"""계약 v0.2 를 지키는지 본다.

정본 근거는 BE 의 `HttpLlmGateway` 다. 여기가 깨지면 BE 가 깨진다.
스텁을 실제 구현으로 바꿔도 이 테스트는 그대로 통과해야 한다.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app

HEADERS = {"X-Prompt-Version": "v1"}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _parse_body(title: str = "테스트 공고", raw: str = "본문" * 200) -> dict[str, Any]:
    """BE 는 title 과 rawContent 만 보낸다."""
    return {"title": title, "rawContent": raw}


# --------------------------------------------------------------------------
# 공통 규약
# --------------------------------------------------------------------------


def test_health_is_open(client: TestClient) -> None:
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "up"


def test_no_auth_header_required(client: TestClient) -> None:
    """BE 는 인증 헤더를 보내지 않는다. 요구하면 전부 401 이 된다."""
    res = client.post("/v1/parse-posting", json=_parse_body())
    assert res.status_code == 200


def test_response_has_no_envelope(client: TestClient) -> None:
    """BE 는 node.path("keywords") 로 최상위에서 읽는다. 감싸면 못 찾는다."""
    body = client.post("/v1/parse-posting", json=_parse_body(), headers=HEADERS).json()

    assert "ok" not in body, "봉투를 씌우면 BE 가 LLM_UNAVAILABLE 을 던진다"
    assert "data" not in body
    assert isinstance(body["keywords"], list)


def test_malformed_body_is_400(client: TestClient) -> None:
    res = client.post("/v1/parse-posting", json={"title": "제목만"}, headers=HEADERS)
    assert res.status_code == 400


def test_request_id_is_echoed(client: TestClient) -> None:
    res = client.get("/health", headers={"X-Request-Id": "abc-123"})
    assert res.headers["X-Request-Id"] == "abc-123"


# --------------------------------------------------------------------------
# §1 파싱
# --------------------------------------------------------------------------


def test_parse_matches_be_record_shape(client: TestClient) -> None:
    """BE 의 ParsedPosting 레코드와 필드가 1:1 이어야 한다."""
    data = client.post("/v1/parse-posting", json=_parse_body(), headers=HEADERS).json()

    assert data["parsingFailed"] is False
    assert len(data["keywords"]) >= 3, "3개 미만이면 BE 가 다시 실패로 판정한다"
    assert data["type"] in {"recruit", "scholarship", "contest", "activity", "other", None}

    # 자격 조건은 평평하다 — 중첩 객체가 아니다
    assert "qualifications" not in data
    for field in ("qualificationYear", "qualificationGpa", "qualificationMajor"):
        assert field in data

    for question in data["formQuestions"]:
        assert set(question) >= {"order", "question", "maxChars"}

    assert data["usage"]["totalTokens"] is not None, "BE 가 usage.totalTokens 를 로깅한다"


def test_prompt_version_is_echoed_in_usage(client: TestClient) -> None:
    """BE 가 재파싱 판단에 쓰는 값이다."""
    res = client.post(
        "/v1/parse-posting", json=_parse_body(), headers={"X-Prompt-Version": "parse@7"}
    )
    assert res.json()["usage"]["promptVersion"] == "parse@7"


@pytest.mark.parametrize("reason_code", ["NO_KEYWORDS", "IMAGE_ONLY", "EMPTY", "NOT_A_POSTING"])
def test_parse_failure_is_200_with_flag(client: TestClient, reason_code: str) -> None:
    """4xx 로 내면 BE 의 callWithRetry 가 LLM_UNAVAILABLE 로 바꿔 서버 장애로 둔갑한다."""
    res = client.post(
        "/v1/parse-posting", json=_parse_body(f"[stub:{reason_code}] 공고"), headers=HEADERS
    )

    assert res.status_code == 200, "파싱 실패는 오류 응답이 아니다"
    body = res.json()
    assert body["parsingFailed"] is True
    assert body["reason"], "BE 가 ParsingFailedException 메시지로 쓴다"
    assert body["reasonCode"] == reason_code


def test_parse_due_date_may_be_null(client: TestClient) -> None:
    """못 읽으면 null 이다. 그럴듯한 날짜를 지어내지 않는다 (#8)."""
    data = client.post(
        "/v1/parse-posting", json=_parse_body("[stub:PARTIAL] 장학 공지"), headers=HEADERS
    ).json()

    assert data["parsingFailed"] is False
    assert data["dueDate"] is None
    assert data["formQuestions"] == [], "양식이 없는 공고는 정상이다"


def test_parse_marks_truncation(client: TestClient) -> None:
    data = client.post(
        "/v1/parse-posting", json=_parse_body(raw="가" * 40_001), headers=HEADERS
    ).json()
    assert data["truncated"] is True


# --------------------------------------------------------------------------
# §2 코멘트
# --------------------------------------------------------------------------


def test_comments_return_two_strings(client: TestClient) -> None:
    body = {
        "matchedKeywords": ["Spring", "Kotlin"],
        "missingQualifications": ["RDB 1년 이상"],
        "matchedPreferences": ["Java/Kotlin 백엔드 경험"],
        "topExperienceTitle": "CareerCompass",
    }
    data = client.post("/v1/comments", json=body, headers=HEADERS).json()

    assert isinstance(data["strength"], str)
    assert isinstance(data["weakness"], str)


def test_comments_are_null_without_grounds(client: TestClient) -> None:
    """근거가 비면 빈말을 만들지 않는다. BE 는 코멘트 null 을 허용하고 점수는 정상으로 낸다."""
    body = {
        "matchedKeywords": [],
        "missingQualifications": [],
        "matchedPreferences": [],
        "topExperienceTitle": "",
    }
    data = client.post("/v1/comments", json=body, headers=HEADERS).json()

    assert data["strength"] is None
    assert data["weakness"] is None


def test_comments_cite_only_given_grounds(client: TestClient) -> None:
    """계산에 쓴 근거만 인용한다 (#14). 입력에 없는 사실이 나오면 점수와 설명이 따로 논다."""
    body = {
        "matchedKeywords": ["Spring"],
        "missingQualifications": [],
        "matchedPreferences": [],
        "topExperienceTitle": "",
    }
    data = client.post("/v1/comments", json=body, headers=HEADERS).json()

    assert "Spring" in data["strength"]
    assert "Kotlin" not in data["strength"], "주지 않은 근거가 등장하면 안 된다"


# --------------------------------------------------------------------------
# §3 초안
# --------------------------------------------------------------------------


def _draft_body(max_chars: int) -> dict[str, Any]:
    return {
        "question": "지원 동기를 작성해 주세요",
        "maxChars": max_chars,
        "tone": "formal",
        "postingTitle": "2026 카카오 SW 인턴십",
        "keywords": ["Spring", "Kotlin"],
        "experienceSummaries": ["CareerCompass — 백엔드", "동아리 프로젝트 — 안드로이드"],
    }


@pytest.mark.parametrize("max_chars", [100, 300, 500])
def test_draft_respects_char_limit(client: TestClient, max_chars: int) -> None:
    """프롬프트로 부탁하는 것이 아니라 생성 후 실측해서 지킨다 (#16)."""
    data = client.post("/v1/draft-answer", json=_draft_body(max_chars), headers=HEADERS).json()

    assert data["charCount"] <= max_chars
    assert data["charCount"] == len(data["answer"])


def test_draft_zero_max_chars_means_no_limit(client: TestClient) -> None:
    """BE 는 null 을 0 으로 바꿔 보낸다. 0 을 글자 수 상한으로 읽으면 빈 답이 나간다."""
    data = client.post("/v1/draft-answer", json=_draft_body(0), headers=HEADERS).json()

    assert 400 <= data["charCount"] <= 600
    assert data["answer"], "BE 는 빈 answer 를 LLM_UNAVAILABLE 로 본다"


def test_draft_reports_used_indexes(client: TestClient) -> None:
    """BE 가 경험 카드 id 를 안 보내므로 인덱스로 답한다."""
    body = _draft_body(300)
    data = client.post("/v1/draft-answer", json=body, headers=HEADERS).json()

    assert data["usedIndexes"]
    assert all(0 <= i < len(body["experienceSummaries"]) for i in data["usedIndexes"])


def test_draft_reports_fact_check(client: TestClient) -> None:
    data = client.post("/v1/draft-answer", json=_draft_body(200), headers=HEADERS).json()

    assert "passed" in data["factCheck"]
    assert isinstance(data["factCheck"]["unverified"], list)


# --------------------------------------------------------------------------
# 없어진 엔드포인트 — BE #51 이 구현했다
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/v1/score", "/v1/generate-item", "/v1/classify-document", "/v1/extract-text", "/v1/embed"],
)
def test_removed_endpoints_are_gone(client: TestClient, path: str) -> None:
    """v0.1 의 경로들이다. 적합도·분류·추출·임베딩은 BE 가 한다 (계약 v0.2 §0)."""
    assert client.post(path, json={}, headers=HEADERS).status_code == 404
