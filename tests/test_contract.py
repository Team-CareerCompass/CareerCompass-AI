"""계약(`docs/AI_CONTRACT_v0.1.md`)을 지키는지 본다.

여기가 깨지면 BE·FE 가 깨진다. 스텁을 실제 구현으로 바꿔도 이 테스트는 그대로 통과해야 한다.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

HEADERS = {"X-Internal-Token": settings.internal_token}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _parse_body(posting_id: int, raw: str = "본문" * 200) -> dict[str, Any]:
    return {
        "postingId": posting_id,
        "title": "테스트 공고",
        "rawContent": raw,
        "url": "https://example.com/1",
        "collectedAt": "2026-09-07T09:00:00+09:00",
        "images": [],
    }


# --------------------------------------------------------------------------
# 공통 규약
# --------------------------------------------------------------------------


def test_health_needs_no_token(client: TestClient) -> None:
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["ok"] is True


def test_internal_token_required(client: TestClient) -> None:
    res = client.post("/v1/parse-posting", json=_parse_body(1))
    assert res.status_code == 401
    assert res.json()["ok"] is False


def test_request_id_is_echoed(client: TestClient) -> None:
    res = client.get("/health", headers={"X-Request-Id": "abc-123"})
    assert res.headers["X-Request-Id"] == "abc-123"


def test_malformed_body_is_invalid_input(client: TestClient) -> None:
    res = client.post("/v1/parse-posting", json={"postingId": "숫자아님"}, headers=HEADERS)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "INVALID_INPUT"


# --------------------------------------------------------------------------
# §1 파싱
# --------------------------------------------------------------------------


def test_parse_ok(client: TestClient) -> None:
    res = client.post("/v1/parse-posting", json=_parse_body(101), headers=HEADERS)
    assert res.status_code == 200

    data = res.json()["data"]
    assert data["status"] == "ok"
    assert len(data["keywords"]) >= 3, "키워드 3개 미만이면 status 가 ok 일 수 없다"
    assert data["type"] in {"recruit", "scholarship", "contest", "activity"}
    assert data["meta"]["provider"], "meta 는 비용 집계(#28)의 입력이다"


def test_parse_partial_lists_missing(client: TestClient) -> None:
    res = client.post("/v1/parse-posting", json=_parse_body(999001), headers=HEADERS)
    data = res.json()["data"]

    assert res.status_code == 200, "부분 성공은 에러가 아니다"
    assert data["status"] == "partial"
    assert data["dueDate"] is None, "못 읽은 마감일은 추측하지 않고 null 이다"
    assert set(data["missing"]) == {"dueDate", "formQuestions"}


@pytest.mark.parametrize(
    ("posting_id", "reason"),
    [
        (999002, "NO_KEYWORDS"),
        (999003, "IMAGE_ONLY"),
        (999004, "NOT_A_POSTING"),
        (999005, "EMPTY"),
    ],
)
def test_parse_failure_reasons(client: TestClient, posting_id: int, reason: str) -> None:
    """BE 가 사용자에게 다른 말을 해야 해서 사유를 구분한다 (계약 §1.3)."""
    res = client.post("/v1/parse-posting", json=_parse_body(posting_id), headers=HEADERS)

    assert res.status_code == 422
    body = res.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "PARSING_FAILED"
    assert body["error"]["detail"]["reason"] == reason


def test_parse_marks_truncation(client: TestClient) -> None:
    res = client.post("/v1/parse-posting", json=_parse_body(102, "가" * 40_001), headers=HEADERS)
    assert res.json()["data"]["truncated"] is True


# --------------------------------------------------------------------------
# §2 적합도
# --------------------------------------------------------------------------

_PARSED = {"postingId": 101, "status": "ok", "keywords": ["Spring", "Kotlin", "Redis"]}
_EXPERIENCE = {"id": 5, "type": "project", "title": "CareerCompass", "data": {}}


def test_score_without_profile_is_refused(client: TestClient) -> None:
    body = {
        "postingId": 101,
        "parsed": _PARSED,
        "profile": {"jobInterests": [], "tags": []},
        "experiences": [],
    }
    res = client.post("/v1/score", json=body, headers=HEADERS)

    assert res.status_code == 422
    assert res.json()["error"]["code"] == "PROFILE_INCOMPLETE"


def test_score_without_experience_is_provisional(client: TestClient) -> None:
    """경험 0개면 분야 유사도만으로 잠정 점수를 낸다 (보고서 4.4.4)."""
    body = {
        "postingId": 101,
        "parsed": _PARSED,
        "profile": {"jobInterests": [{"code": "backend", "priority": 1}]},
        "experiences": [],
    }
    res = client.post("/v1/score", json=body, headers=HEADERS)

    assert res.status_code == 200
    assert res.json()["data"]["provisional"] is True


def test_score_cites_only_used_experiences(client: TestClient) -> None:
    """계산에 쓴 근거만 인용한다 (#14·#29)."""
    body = {
        "postingId": 101,
        "parsed": _PARSED,
        "profile": {"jobInterests": [{"code": "backend", "priority": 1}]},
        "experiences": [_EXPERIENCE],
    }
    data = client.post("/v1/score", json=body, headers=HEADERS).json()["data"]

    given = {e["id"] for e in body["experiences"]}
    assert set(data["citedExperienceIds"]) <= given

    axes = {a["axis"] for a in data["breakdown"]}
    assert axes == {"field_similarity", "qualification", "preference", "competition"}


def test_competition_axis_may_be_null(client: TestClient) -> None:
    """근거가 없으면 없다고 한다. 추정하지 않는다 (#12)."""
    body = {
        "postingId": 101,
        "parsed": _PARSED,
        "profile": {"jobInterests": [{"code": "backend", "priority": 1}]},
        "experiences": [_EXPERIENCE],
    }
    data = client.post("/v1/score", json=body, headers=HEADERS).json()["data"]

    competition = next(a for a in data["breakdown"] if a["axis"] == "competition")
    if competition["score"] is None:
        assert data["reweighted"] is True, "빠진 축의 가중치는 나머지에 배분한다"


# --------------------------------------------------------------------------
# §3 초안 생성
# --------------------------------------------------------------------------


@pytest.mark.parametrize("max_chars", [100, 300, 500])
def test_generated_answer_respects_char_limit(client: TestClient, max_chars: int) -> None:
    """프롬프트로 부탁하는 것이 아니라 생성 후 실측해서 지킨다 (#16)."""
    body = {"question": "지원 동기를 작성해 주세요.", "maxChars": max_chars, "tone": "formal"}
    data = client.post("/v1/generate-item", json=body, headers=HEADERS).json()["data"]

    assert data["charCount"] <= max_chars
    assert data["charCount"] == len(data["answer"])


def test_generate_without_limit_uses_default_range(client: TestClient) -> None:
    body = {"question": "자유롭게 작성해 주세요.", "maxChars": None}
    data = client.post("/v1/generate-item", json=body, headers=HEADERS).json()["data"]

    assert 400 <= data["charCount"] <= 600


def test_generate_reports_fact_check(client: TestClient) -> None:
    body = {"question": "지원 동기", "maxChars": 200}
    data = client.post("/v1/generate-item", json=body, headers=HEADERS).json()["data"]

    assert "passed" in data["factCheck"]
    assert isinstance(data["factCheck"]["unverified"], list)


# --------------------------------------------------------------------------
# §4 문서 분류
# --------------------------------------------------------------------------


def test_classify_returns_known_categories(client: TestClient) -> None:
    allowed = {"motivation", "background", "experience", "competency", "aspiration", "other"}
    body = {"pastApplicationId": 7, "text": "첫 문단입니다.\n\n두 번째 문단입니다."}

    data = client.post("/v1/classify-document", json=body, headers=HEADERS).json()["data"]

    assert len(data["items"]) == 2, "문단 단위로 자른다"
    assert {i["category"] for i in data["items"]} <= allowed
    assert all(isinstance(i["confident"], bool) for i in data["items"])


# --------------------------------------------------------------------------
# §5 임베딩
# --------------------------------------------------------------------------


def test_embed_dimensions_are_consistent(client: TestClient) -> None:
    """dim 이 바뀌면 BE 의 pgvector 컬럼을 바꿔야 한다."""
    body = {"texts": ["가", "나", "다"], "purpose": "experience"}
    data = client.post("/v1/embed", json=body, headers=HEADERS).json()["data"]

    assert len(data["vectors"]) == 3
    assert all(len(v) == data["dim"] for v in data["vectors"])
