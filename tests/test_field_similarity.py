"""§5 분야 유사도 엔드포인트 — 실호출 0 (임베딩은 대역)."""

import asyncio
import math
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import service
from app.budget import BudgetExceeded
from app.config import settings
from app.embedding import Embedding
from app.field_similarity import (
    CALIBRATION,
    COSINE_FULL,
    COSINE_ZERO,
    calibrate,
    keyword_score,
    posting_text,
    profile_text,
)
from app.main import app
from app.providers.base import ProviderTimeout, RateLimited

INTERESTS = ["backend", "백엔드 개발", "Java", "Spring"]
MID = (COSINE_ZERO + COSINE_FULL) / 2


def _vector_with_cosine(c: float) -> list[float]:
    """프로필 벡터 [1, 0] 과의 코사인이 c 인 단위 벡터."""
    return [c, math.sqrt(1 - c * c)]


class FakeEmbedder:
    """제목(=키워드 없는 공고 텍스트)마다 정해 둔 코사인이 나오게."""

    def __init__(
        self, cosines: dict[str, float], *, error: Exception | None = None, delay_s: float = 0
    ) -> None:
        self.cosines = cosines
        self.error = error
        self.delay_s = delay_s
        self.calls: list[str] = []

    async def embed(self, text: str) -> Embedding:
        self.calls.append(text)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        if text == profile_text(INTERESTS):
            return Embedding([1.0, 0.0], 5, cached=False)
        return Embedding(_vector_with_cosine(self.cosines[text]), 7, cached=text == "캐시됨")


def _body(*postings: dict[str, Any]) -> dict[str, Any]:
    return {"interestTexts": INTERESTS, "postings": list(postings)}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def fake(real_gateway: None, monkeypatch: pytest.MonkeyPatch) -> FakeEmbedder:
    embedder = FakeEmbedder({"딱 맞음": 0.9, "중간": MID, "무관": 0.1, "캐시됨": COSINE_FULL})
    monkeypatch.setattr(service, "embedder", lambda: embedder)
    return embedder


# ---- 텍스트 모양 — 측정(`scripts/compare_field_similarity.py`)과 같아야 숫자가 같다 ----------


def test_text_shape_matches_the_measurement() -> None:
    assert profile_text(["backend", " 백엔드 개발 ", ""]) == "backend, 백엔드 개발"
    assert posting_text("카카오 인턴", ["Spring", " Kotlin ", ""]) == "카카오 인턴\nSpring, Kotlin"
    assert posting_text("카카오 인턴", []) == "카카오 인턴"
    assert posting_text("", []) == ""


def test_calibration_maps_two_points_and_clamps() -> None:
    assert calibrate(COSINE_ZERO) == 0
    assert calibrate(MID) == 50
    assert calibrate(COSINE_FULL) == 100
    assert calibrate(0.0) == 0
    assert calibrate(0.99) == 100


# ---- 스텁 — BE 통합 테스트용. BE 와 같은 글자 비교로 답한다 --------------------------------


def test_stub_answers_with_be_keyword_score(client: TestClient) -> None:
    body = _body(
        {"id": 7, "title": "백엔드 인턴", "keywords": ["Java", "Kotlin"]},
        {"id": "p-2", "title": "디자인", "keywords": ["Figma"]},
    )
    data = client.post("/v1/field-similarity", json=body).json()
    assert data["method"] == "keyword"
    assert [s["id"] for s in data["scores"]] == [7, "p-2"]  # 순서·자료형 그대로
    assert data["scores"][0]["score"] == keyword_score(["Java", "Kotlin"], INTERESTS)[0] == 50
    assert data["scores"][1]["score"] == 0
    assert data["usage"]["promptVersion"] == CALIBRATION


@pytest.mark.parametrize(
    "body",
    [
        {"interestTexts": INTERESTS, "postings": []},
        {"interestTexts": [], "postings": [{"id": 1, "title": "t"}]},
        {"postings": [{"id": 1, "title": "t"}]},
        {"interestTexts": INTERESTS, "postings": [{"id": i, "title": "t"} for i in range(21)]},
    ],
)
def test_bad_requests_are_400(client: TestClient, body: dict[str, Any]) -> None:
    assert client.post("/v1/field-similarity", json=body).status_code == 400


# ---- 실경로 (임베딩 대역) ---------------------------------------------------------------


def test_scores_are_calibrated_cosines_in_request_order(
    client: TestClient, fake: FakeEmbedder
) -> None:
    body = _body(
        {"id": 1, "title": "무관"},
        {"id": 2, "title": "중간"},
        {"id": 3, "title": "딱 맞음"},
    )
    data = client.post("/v1/field-similarity", json=body).json()
    assert data["method"] == "embedding"
    assert [(s["id"], s["score"]) for s in data["scores"]] == [(1, 0), (2, 50), (3, 100)]
    assert data["scores"][2]["similarity"] == pytest.approx(0.9, abs=1e-4)
    assert data["usage"]["model"] == "embedding-v2"
    assert data["usage"]["promptVersion"] == CALIBRATION


def test_same_text_is_embedded_once_and_cache_hits_cost_nothing(
    client: TestClient, fake: FakeEmbedder
) -> None:
    body = _body(
        {"id": 1, "title": "중간"}, {"id": 2, "title": "중간"}, {"id": 3, "title": "캐시됨"}
    )
    data = client.post("/v1/field-similarity", json=body).json()
    assert sorted(fake.calls) == sorted([profile_text(INTERESTS), "중간", "캐시됨"])
    assert data["usage"]["promptTokens"] == 5 + 7  # 프로필 + 「중간」. 캐시 히트는 안 센다
    assert data["scores"][2]["score"] == 100


def test_posting_without_title_or_keywords_scores_zero_without_a_call(
    client: TestClient, fake: FakeEmbedder
) -> None:
    data = client.post("/v1/field-similarity", json=_body({"id": 9, "title": " "})).json()
    assert data["scores"] == [{"id": 9, "score": 0, "similarity": None}]
    assert fake.calls == [profile_text(INTERESTS)]


def test_blank_interests_are_400(client: TestClient, fake: FakeEmbedder) -> None:
    body = {"interestTexts": [" ", ""], "postings": [{"id": 1, "title": "중간"}]}
    assert client.post("/v1/field-similarity", json=body).status_code == 400


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (RateLimited("429"), 429),
        (BudgetExceeded("일일", 1000.0, 1000.0, 0.1), 503),
        (ProviderTimeout("느림"), 503),
    ],
)
def test_failures_map_to_be_retry_codes(
    client: TestClient, fake: FakeEmbedder, error: Exception, status: int
) -> None:
    """BE 는 429·5xx 만 재시도하고, 끝내 실패하면 자기 글자 비교로 대신한다 (계약 §5)."""
    fake.error = error
    res = client.post("/v1/field-similarity", json=_body({"id": 1, "title": "중간"}))
    assert res.status_code == status


def test_whole_request_respects_the_deadline(
    client: TestClient, fake: FakeEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "request_deadline_s", 0.05)
    fake.delay_s = 0.5
    res = client.post("/v1/field-similarity", json=_body({"id": 1, "title": "중간"}))
    assert res.status_code == 503


def test_health_shows_the_calibration(client: TestClient, real_gateway: None) -> None:
    health = client.get("/health").json()
    assert health["fieldSimilarity"] == {"model": "embedding-v2", "calibration": CALIBRATION}
