"""계약 v0.2 §1~§4 의 엔드포인트.

엔드포인트는 셋뿐이다. 적합도 산출·문서 분류·텍스트 추출·유사 공고·추천은 BE #51 이 구현했다.

이 서비스는 상태를 갖지 않는다 — 큐·캐시·재시도·SSE 는 전부 BE 가 한다.
실제 처리는 `app/service.py` → `app/gateway.py`. 여기는 HTTP 모양만 다룬다.
"""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query

from app import service
from app.config import settings
from app.schemas import CommentsRequest, DraftRequest, ParseRequest

router = APIRouter(prefix="/v1")

# BE 가 타임아웃 처리를 검증할 수 있게 지연을 흉내 낸다. 스텁 전용.
Delay = Annotated[int, Query(ge=0, le=60_000, description="응답을 늦출 밀리초 (스텁 전용)")]

# BE 가 보내는 프롬프트 버전. 로그와 usage 에 그대로 남긴다 — BE 가 재파싱 판단에 쓴다.
PromptVersion = Annotated[str, Header(alias="X-Prompt-Version")]


async def _sleep(delay_ms: int) -> None:
    if delay_ms:
        await asyncio.sleep(delay_ms / 1000)


def _dump(model: Any) -> dict[str, Any]:
    """봉투 없이 최상위에 필드를 놓는다 — BE 가 node.path(...) 로 바로 읽는다."""
    return model.model_dump(by_alias=True, mode="json")


@router.post("/parse-posting")
async def parse_posting(
    req: ParseRequest,
    x_prompt_version: PromptVersion = "v1",
    delay: Delay = 0,
) -> dict[str, Any]:
    """§1 공고 구조화 파싱 — 지원서 양식 인식(F4-1)도 여기서 함께 한다.

    파싱 실패도 200 이다. 4xx 로 내면 BE 가 서버 장애로 오인한다.
    """
    await _sleep(delay)
    return _dump(await service.parse_posting(req, x_prompt_version))


@router.post("/comments")
async def comments(
    req: CommentsRequest,
    x_prompt_version: PromptVersion = "v1",
    delay: Delay = 0,
) -> dict[str, Any]:
    """§2 강점·약점 코멘트 — 점수는 BE 가 낸다. 받은 근거 안에서만 쓴다."""
    await _sleep(delay)
    return _dump(await service.comments(req, x_prompt_version))


@router.post("/draft-answer")
async def draft_answer(
    req: DraftRequest,
    x_prompt_version: PromptVersion = "v1",
    delay: Delay = 0,
) -> dict[str, Any]:
    """§3 지원서 초안 — 항목 하나에 호출 하나. 재생성도 같은 경로다."""
    await _sleep(delay)
    return _dump(await service.draft_answer(req, x_prompt_version))


health_router = APIRouter()


@health_router.get("/health")
async def health() -> dict[str, Any]:
    """§4. 프로바이더를 실제로 호출하지는 않는다 (비용). 키 존재만 본다."""
    stub = service.use_stub()
    body: dict[str, Any] = {
        "status": "up",
        "version": settings.version,
        "stubMode": stub,
        "provider": "stub" if stub else f"hcx/{settings.hcx_model}",
        "providerKeyPresent": bool(settings.hcx_api_key),
    }
    if not stub:
        body["budget"] = service.budget().status().as_dict()
    return body
