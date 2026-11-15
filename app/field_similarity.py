"""분야 유사도 — 적합도의 분야 유사도 축(40%)을 임베딩으로 (계약 §5, 보고서 4.4.4).

BE `SuitabilityCalculator.fieldSimilarity` 는 공고 키워드와 관심 분야 글자의 부분일치다. 같은
정답으로 재면 임베딩이 관련 공고를 훨씬 잘 올린다 — 실서버 입력 기준 평균 AUC 0.64 → 0.89
(`docs/EVAL.md` 「분야 유사도」). 그 측정과 **같은 텍스트 모양**으로 부른다. 모양을 바꾸면 측정이
무효가 되고 캐시도 안 맞는다.

- 관심 쪽: BE `interestTexts`(직무 코드·라벨·태그)를 「, 」로 잇는다
- 공고 쪽: 「제목\\n키워드, 키워드」

**눈금** — 코사인은 관련·무관이 겹친 좁은 구간에 몰린다(관련 평균 0.45, 무관 평균 0.37). BE 의
0~100 축에 넣으려고 두 점으로 편다: 무관 쌍의 중앙값(0.365) 이하는 0, 관련 쌍 상위 10%(0.509)
이상은 100, 사이는 직선. 순서는 그대로라 AUC 도 0.89 그대로다. 공고 35건에서 정한 값이라 공고가
쌓이면 다시 맞춘다. **값을 바꾸면 `CALIBRATION` 을 올린다** — 응답 `usage.promptVersion` 에 실려
BE 가 캐시를 버릴지 판단할 수 있다.

스텁 모드에서는 BE 와 같은 글자 비교(`keyword_score`)로 답한다 — 모양은 같고 `method` 만 다르다.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Sequence
from typing import Protocol

from app.budget import BudgetExceeded
from app.contract import ErrorCode, ServiceError
from app.embedding import MODEL, PRICE_KRW_PER_TOKEN, Embedding, cosine
from app.providers.base import RateLimited
from app.schemas import (
    FieldSimilarityRequest,
    FieldSimilarityResult,
    FieldSimilarityScore,
    Usage,
)

CALIBRATION = "field-v1"
COSINE_ZERO = 0.365
"""이 코사인 이하는 0점 — 11-15 정답셋에서 무관 쌍 370개의 중앙값."""
COSINE_FULL = 0.509
"""이 코사인 이상은 100점 — 같은 정답셋에서 관련 쌍 50개의 상위 10%."""

MAX_CONCURRENCY = 4
"""임베딩을 동시에 몇 개까지 부르나. 테스트 키 호출 한도를 혼자 다 쓰지 않게 작게 둔다."""


class Embedder(Protocol):
    async def embed(self, text: str) -> Embedding: ...


def profile_text(interests: Sequence[str]) -> str:
    return ", ".join(t.strip() for t in interests if t.strip())


def posting_text(title: str, keywords: Sequence[str]) -> str:
    kw = ", ".join(k.strip() for k in keywords if k.strip())
    return f"{title.strip()}\n{kw}".strip()


def calibrate(similarity: float) -> int:
    ratio = (similarity - COSINE_ZERO) / (COSINE_FULL - COSINE_ZERO)
    return max(0, min(100, math.floor(ratio * 100 + 0.5)))


def _java_round(x: float) -> int:
    """`Math.round(float)` — 반올림이 0.5 에서 위로 간다 (파이썬 `round` 는 짝수 쪽)."""
    return math.floor(x + 0.5)


def keyword_score(keywords: Sequence[str], interests: Sequence[str]) -> tuple[int, list[str]]:
    """BE `SuitabilityCalculator.fieldSimilarity` 이식 (BE #51, 로컬 `pr-51` 에서 읽음).

    키워드가 관심 텍스트 하나와 **어느 쪽으로든 포함 관계**면 맞은 것이다(소문자). 점수는
    맞은 키워드 수 / min(키워드 수, 5) * 100, 상한 100. 뜻이 아니라 글자를 본다 — 관심
    「행정학」은 키워드 「행정학」은 잡고 「기간제 행정원」은 놓친다.
    """
    if not keywords or not interests:
        return 0, []
    lowered = [i.lower() for i in interests]
    matched = [kw for kw in keywords if any(i in kw.lower() or kw.lower() in i for i in lowered)]
    denominator = min(len(keywords), 5)
    return min(100, _java_round(len(matched) * 100 / denominator)), matched


def keyword_result(req: FieldSimilarityRequest) -> FieldSimilarityResult:
    """스텁 응답 — BE 가 지금 내는 점수와 같다."""
    return FieldSimilarityResult(
        scores=[
            FieldSimilarityScore(id=p.id, score=keyword_score(p.keywords, req.interest_texts)[0])
            for p in req.postings
        ],
        method="keyword",
        usage=Usage(provider="stub", model="keyword", prompt_version=CALIBRATION),
    )


def _service_error(exc: BaseException) -> ServiceError:
    if isinstance(exc, RateLimited):
        return ServiceError(ErrorCode.RATE_LIMITED, f"임베딩 호출 한도: {exc}")
    if isinstance(exc, BudgetExceeded):
        return ServiceError(ErrorCode.LLM_UNAVAILABLE, str(exc))
    return ServiceError(ErrorCode.LLM_UNAVAILABLE, f"임베딩 실패: {exc}")


async def score(
    req: FieldSimilarityRequest, embedder: Embedder, *, deadline_s: float
) -> FieldSimilarityResult:
    profile = profile_text(req.interest_texts)
    if not profile:
        raise ServiceError(ErrorCode.INVALID_INPUT, "interestTexts 가 전부 비어 있다")
    texts = [posting_text(p.title, p.keywords) for p in req.postings]
    unique = list(dict.fromkeys([profile, *(t for t in texts if t)]))

    started = time.perf_counter()
    gate = asyncio.Semaphore(MAX_CONCURRENCY)

    async def one(text: str) -> Embedding:
        async with gate:
            return await embedder.embed(text)

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(one(t) for t in unique), return_exceptions=True), deadline_s
        )
    except TimeoutError as exc:
        raise ServiceError(
            ErrorCode.LLM_UNAVAILABLE, f"임베딩이 {deadline_s:g}초를 넘었다"
        ) from exc
    failures = [r for r in results if isinstance(r, BaseException)]
    if failures:
        raise _service_error(failures[0]) from failures[0]
    vectors = {
        text: result
        for text, result in zip(unique, results, strict=True)
        if isinstance(result, Embedding)
    }

    scores: list[FieldSimilarityScore] = []
    for posting, text in zip(req.postings, texts, strict=True):
        if not text:
            scores.append(FieldSimilarityScore(id=posting.id, score=0))
            continue
        similarity = cosine(vectors[profile].vector, vectors[text].vector)
        scores.append(
            FieldSimilarityScore(
                id=posting.id, score=calibrate(similarity), similarity=round(similarity, 4)
            )
        )

    tokens = sum(e.input_tokens for e in vectors.values() if not e.cached)
    return FieldSimilarityResult(
        scores=scores,
        method="embedding",
        usage=Usage(
            provider="hcx",
            model=MODEL,
            prompt_version=CALIBRATION,
            prompt_tokens=tokens,
            total_tokens=tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
            cost_krw=round(tokens * PRICE_KRW_PER_TOKEN, 4),
        ),
    )
