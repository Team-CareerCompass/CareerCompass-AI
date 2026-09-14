"""엔드포인트가 부르는 것 — 스텁이냐 게이트웨이냐를 여기서 정한다.

`CC_STUB_MODE=true` 면 `stubs`(모델 안 부름, 제목 표식으로 시나리오 선택),
아니면 `Gateway(HcxProvider)`. 라우터는 이 차이를 모른다.
"""

from __future__ import annotations

from functools import lru_cache

from app import stubs
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


@lru_cache(maxsize=1)
def gateway() -> Gateway:
    provider = HcxProvider(
        settings.hcx_api_key, settings.hcx_model, timeout_s=settings.llm_timeout_s
    )
    cache = ReplayCache(settings.llm_cache_dir, settings.llm_cache)
    return Gateway(provider, cache)


async def parse_posting(req: ParseRequest, prompt_version: str) -> ParseResult | ParseFailure:
    if settings.stub_mode:
        return stubs.parse_posting(req, prompt_version)
    return await gateway().parse_posting(req, prompt_version)


async def comments(req: CommentsRequest, prompt_version: str) -> CommentsResult:
    if settings.stub_mode:
        return stubs.comments(req, prompt_version)
    return await gateway().comments(req, prompt_version)


async def draft_answer(req: DraftRequest, prompt_version: str) -> DraftResult:
    if settings.stub_mode:
        return stubs.draft_answer(req, prompt_version)
    return await gateway().draft_answer(req, prompt_version)
