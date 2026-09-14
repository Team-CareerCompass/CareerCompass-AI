"""엔드포인트가 부르는 것 — 스텁이냐 게이트웨이냐를 여기서 정한다.

`CC_STUB_MODE=true` 면 `stubs`(모델 안 부름, 제목 표식으로 시나리오 선택),
아니면 `Gateway(HcxProvider)`. 라우터는 이 차이를 모른다.

기능마다 모델을 달리 둘 수 있다(`CC_HCX_MODEL_PARSE` 등). 게이트웨이는 기능당 하나, 캐시와
예산은 셋이 같이 쓴다 — 예산은 **전체** 지출에 걸린다.
"""

from __future__ import annotations

from functools import cache

from app import stubs
from app.budget import Budget
from app.cache import ReplayCache
from app.config import settings
from app.gateway import Gateway
from app.providers.hcx import HcxProvider
from app.schemas import (
    CommentsRequest,
    CommentsResult,
    DraftRequest,
    DraftResult,
    ParseFailure,
    ParseRequest,
    ParseResult,
)


@cache
def budget() -> Budget:
    return Budget(
        settings.budget_ledger,
        daily_krw=settings.budget_daily_krw,
        monthly_krw=settings.budget_monthly_krw,
    )


@cache
def _replay_cache() -> ReplayCache:
    return ReplayCache(settings.llm_cache_dir, settings.llm_cache)


@cache
def gateway(function: str = "parse") -> Gateway:
    model = {
        "parse": settings.hcx_model_parse,
        "comments": settings.hcx_model_comments,
        "draft": settings.hcx_model_draft,
    }.get(function) or settings.hcx_model
    provider = HcxProvider(settings.hcx_api_key, model, timeout_s=settings.llm_timeout_s)
    return Gateway(provider, _replay_cache(), budget())


async def parse_posting(req: ParseRequest, prompt_version: str) -> ParseResult | ParseFailure:
    if settings.stub_mode:
        return stubs.parse_posting(req, prompt_version)
    return await gateway("parse").parse_posting(req, prompt_version)


async def comments(req: CommentsRequest, prompt_version: str) -> CommentsResult:
    if settings.stub_mode:
        return stubs.comments(req, prompt_version)
    return await gateway("comments").comments(req, prompt_version)


async def draft_answer(req: DraftRequest, prompt_version: str) -> DraftResult:
    if settings.stub_mode:
        return stubs.draft_answer(req, prompt_version)
    return await gateway("draft").draft_answer(req, prompt_version)
