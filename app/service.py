"""엔드포인트가 부르는 것 — 스텁이냐 게이트웨이냐를 여기서 정한다.

`CC_STUB_MODE=true` 면 `stubs`(모델 안 부름, 제목 표식으로 시나리오 선택),
아니면 `Gateway(HcxProvider)`. 라우터는 이 차이를 모른다.

기능마다 모델을 달리 둘 수 있다(`CC_HCX_MODEL_PARSE` 등). 게이트웨이는 기능당 하나, 캐시와
예산은 셋이 같이 쓴다 — 예산은 **전체** 지출에 걸린다.
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger("careercompass.ai.service")


_stub_warned = False


def use_stub() -> bool:
    """스텁 여부 — 명시적으로 켰거나, 키가 없어 실호출이 불가능하면.

    키 없이 실호출 모드로 뜨면 첫 요청에서 401 이 되어 BE 가 LLM_UNAVAILABLE 을 본다. 그보다는
    스텁으로라도 돌되 **시끄럽게** 알린다 — 로그(한 번)와 `/health.stubMode` 로.
    캐시하지 않는다 — 테스트가 설정을 바꿔 가며 부른다.
    """
    global _stub_warned
    stub = settings.stub_mode or not settings.hcx_api_key
    if stub and not _stub_warned:
        _stub_warned = True
        if settings.stub_mode:
            logger.warning("CC_STUB_MODE=true — 모델을 부르지 않고 더미 응답을 낸다")
        else:
            logger.error(
                "CC_HCX_API_KEY 가 비어 있다 — 스텁으로 내려간다. 실서버라면 설정 누락이다"
            )
    return stub


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
    if use_stub():
        return stubs.parse_posting(req, prompt_version)
    return await gateway("parse").parse_posting(req, prompt_version)


async def comments(req: CommentsRequest, prompt_version: str) -> CommentsResult:
    if use_stub():
        return stubs.comments(req, prompt_version)
    return await gateway("comments").comments(req, prompt_version)


async def draft_answer(req: DraftRequest, prompt_version: str) -> DraftResult:
    if use_stub():
        return stubs.draft_answer(req, prompt_version)
    return await gateway("draft").draft_answer(req, prompt_version)
