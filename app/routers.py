"""계약 §1~§6 의 엔드포인트.

이 서비스는 상태를 갖지 않는다 (계약 D1). 큐·재시도·SSE 는 BE 가 한다.
"""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Query, UploadFile

from app import stubs
from app.config import settings
from app.contract import ok
from app.schemas import (
    ClassifyRequest,
    EmbedRequest,
    GenerateRequest,
    ParseRequest,
    ScoreRequest,
)

router = APIRouter(prefix="/v1")

# BE 가 타임아웃 처리를 검증할 수 있게 지연을 흉내 낸다. 스텁 전용.
Delay = Annotated[int, Query(ge=0, le=60_000, description="응답을 늦출 밀리초 (스텁 전용)")]


async def _sleep(delay_ms: int) -> None:
    if delay_ms:
        await asyncio.sleep(delay_ms / 1000)


def _dump(model: Any) -> dict[str, Any]:
    return model.model_dump(by_alias=True, mode="json")


@router.post("/parse-posting")
async def parse_posting(req: ParseRequest, delay: Delay = 0) -> dict[str, Any]:
    """§1 공고 구조화 파싱 — 지원서 양식 인식(F4-1)도 여기서 함께 한다."""
    await _sleep(delay)
    return ok(_dump(stubs.parse(req)))


@router.post("/score")
async def score(req: ScoreRequest, delay: Delay = 0) -> dict[str, Any]:
    """§2 적합도 4축 산출과 강점·약점 코멘트."""
    await _sleep(delay)
    return ok(_dump(stubs.score(req)))


@router.post("/generate-item")
async def generate_item(req: GenerateRequest, delay: Delay = 0) -> dict[str, Any]:
    """§3 지원서 초안 — 항목 1개당 1회 호출. 재생성도 같은 엔드포인트다."""
    await _sleep(delay)
    return ok(_dump(stubs.generate(req)))


@router.post("/classify-document")
async def classify_document(req: ClassifyRequest, delay: Delay = 0) -> dict[str, Any]:
    """§4.2 과거 지원서 항목 분류 6종."""
    await _sleep(delay)
    return ok(_dump(stubs.classify(req)))


@router.post("/extract-text")
async def extract_text(
    past_application_id: Annotated[int, Form(alias="pastApplicationId")],
    file: Annotated[UploadFile, File()],
    delay: Delay = 0,
) -> dict[str, Any]:
    """§4.3 텍스트 추출 — 외부로 나가는 호출이 없다. 전부 로컬에서 한다."""
    await _sleep(delay)
    size = len(await file.read())
    return ok(_dump(stubs.extract_text(past_application_id, file.filename or "", size)))


@router.post("/embed")
async def embed(req: EmbedRequest, delay: Delay = 0) -> dict[str, Any]:
    """§5 임베딩 — D2 에 따라 MVP 에서는 쓰지 않는다. 인터페이스만 고정해 둔다."""
    await _sleep(delay)
    return ok(_dump(stubs.embed(req)))


health_router = APIRouter()


@health_router.get("/health")
async def health() -> dict[str, Any]:
    """§6. 프로바이더를 실제로 호출하지는 않는다 (비용)."""
    return ok(
        {
            "version": settings.version,
            "stubMode": settings.stub_mode,
            "providers": {"stub": "up"},
        }
    )
