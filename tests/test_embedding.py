"""HCX 임베딩 — 요청 모양·예산·캐시만 본다. 실호출 0 (`httpx.MockTransport`)."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from app.budget import Budget, BudgetExceeded
from app.embedding import PRICE_KRW_PER_TOKEN, Embedding, HcxEmbedder, cosine
from app.providers.base import InputError, ProviderTimeout, RateLimited

Handler = Callable[[httpx.Request], httpx.Response]


def _ok(vector: list[float], tokens: int = 7) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": {"code": "20000", "message": "OK"},
            "result": {"embedding": vector, "inputTokens": tokens},
        },
    )


def _embed(handler: Handler, texts: list[str], **kwargs: object) -> list[Embedding]:
    async def run() -> list[Embedding]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            embedder = HcxEmbedder("nv-test", client=client, **kwargs)  # type: ignore[arg-type]
            return [await embedder.embed(t) for t in texts]

    return asyncio.run(run())


def test_request_shape_and_result() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok([0.1, 0.2, 0.3])

    (result,) = _embed(handler, ["  백엔드 개발  "])
    assert seen[0].url.path == "/v1/api-tools/embedding/v2"
    assert seen[0].headers["Authorization"] == "Bearer nv-test"
    assert seen[0].headers["X-NCP-CLOVASTUDIO-REQUEST-ID"]
    assert json.loads(seen[0].content) == {"text": "백엔드 개발"}
    assert result == Embedding([0.1, 0.2, 0.3], 7, cached=False)


def test_records_actual_tokens_to_the_ledger(tmp_path: Path) -> None:
    budget = Budget(tmp_path / "budget.json", daily_krw=10, monthly_krw=100)
    _embed(lambda _: _ok([1.0], tokens=50), ["공고"], budget=budget)
    assert budget.status().today_krw == pytest.approx(50 * PRICE_KRW_PER_TOKEN)


def test_budget_blocks_before_the_call(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _ok([1.0])

    budget = Budget(tmp_path / "budget.json", daily_krw=0.0001, monthly_krw=100)
    with pytest.raises(BudgetExceeded):
        _embed(handler, ["긴 텍스트" * 10], budget=budget)
    assert calls == []


def test_same_text_is_served_from_cache(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _ok([0.5, 0.5])

    first, second = _embed(handler, ["마케팅", "마케팅 "], cache_dir=tmp_path)
    assert len(calls) == 1
    assert second == Embedding([0.5, 0.5], 7, cached=True)
    assert first.vector == second.vector
    # 캐시 파일에 원문을 남기지 않는다 — 나중에 사용자 텍스트를 넣어도 새지 않게
    (cached,) = tmp_path.iterdir()
    assert "마케팅" not in cached.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(429), RateLimited),
        (httpx.Response(200, json={"status": {"code": "42901", "message": "한도"}}), RateLimited),
        (httpx.Response(200, json={"status": {"code": "40001", "message": "x"}}), InputError),
        (httpx.Response(401, text="no"), InputError),
    ],
)
def test_errors_are_classified(response: httpx.Response, error: type[Exception]) -> None:
    with pytest.raises(error):
        _embed(lambda _: response, ["공고"])


def test_timeout_is_not_retried_here() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderTimeout):
        _embed(handler, ["공고"])


def test_empty_text_and_missing_key_are_rejected() -> None:
    with pytest.raises(InputError):
        _embed(lambda _: _ok([1.0]), ["   "])
    with pytest.raises(InputError):
        HcxEmbedder("")


def test_cosine() -> None:
    assert cosine([1.0, 0.0], [2.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 3.0]) == pytest.approx(0.0)
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    with pytest.raises(ValueError, match="차원"):
        cosine([1.0], [1.0, 2.0])
