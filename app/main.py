"""FastAPI 앱.

BE 의 `HttpLlmGateway` 가 부른다 (계약 v0.2). 공개 인터넷에 노출하지 않는다.
"""

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import settings
from app.contract import ErrorCode, ServiceError, error_body
from app.routers import health_router, router

logger = logging.getLogger("careercompass.ai")

app = FastAPI(
    title="CareerCompass AI",
    version=settings.version,
    description="공고 파싱 · 강점약점 코멘트 · 지원서 초안. 계약은 docs/AI_CONTRACT_v0.2.md.",
)

_PUBLIC_PATHS = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})


@app.middleware("http")
async def observe(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """상관관계 id 와 호출 기록. 인증은 기본적으로 요구하지 않는다 — BE 가 토큰을 보내지 않는다."""
    request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())

    needs_token = settings.require_internal_token and request.url.path not in _PUBLIC_PATHS
    if needs_token and request.headers.get("X-Internal-Token") != settings.internal_token:
        return JSONResponse(
            status_code=401,
            content=error_body(ErrorCode.INVALID_INPUT, "내부 호출 토큰이 올바르지 않습니다"),
            headers={"X-Request-Id": request_id},
        )

    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    # 호출마다 남긴다 — 비용·지연 집계(#28)가 여기에 붙는다.
    logger.info(
        "%s %s %s %dms prompt=%s",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
        request.headers.get("X-Prompt-Version", "-"),
        extra={"request_id": request_id},
    )
    response.headers["X-Request-Id"] = request_id
    return response


@app.exception_handler(ServiceError)
async def service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
    """429·503 만 BE 가 재시도한다. 파싱 실패는 여기로 오지 않는다 — 200 + 플래그다."""
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(exc.code, exc.message),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    """400 이다. BE 는 이것을 LLM_UNAVAILABLE 로 바꾸고 재시도하지 않는다 — 버그라는 뜻이다."""
    logger.warning("계약과 다른 요청: %s", exc.errors())
    return JSONResponse(
        status_code=400,
        content=error_body(ErrorCode.INVALID_INPUT, "요청 형식이 계약과 다릅니다"),
    )


app.include_router(health_router)
app.include_router(router)
