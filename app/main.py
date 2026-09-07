"""FastAPI 앱.

BE 가 HTTP 로 부른다 (계약 「공통 규약」). 공개 인터넷에 노출하지 않는다.
"""

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import settings
from app.contract import ErrorCode, ServiceError, err
from app.routers import health_router, router

logger = logging.getLogger("careercompass.ai")

app = FastAPI(
    title="CareerCompass AI",
    version=settings.version,
    description="공고 파싱 · 적합도 산출 · 지원서 초안 생성. 계약은 docs/AI_CONTRACT_v0.1.md.",
)

# 인증이 필요 없는 경로. 헬스체크는 컨테이너 오케스트레이터가 부른다.
_PUBLIC_PATHS = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})


@app.middleware("http")
async def internal_auth(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """공유 시크릿 한 개. 사용자 JWT 는 이 서비스로 넘어오지 않는다."""
    request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())

    if request.url.path not in _PUBLIC_PATHS:
        token = request.headers.get("X-Internal-Token")
        if token != settings.internal_token:
            return JSONResponse(
                status_code=401,
                content=err(ErrorCode.INVALID_INPUT, "내부 호출 토큰이 없거나 올바르지 않습니다"),
                headers={"X-Request-Id": request_id},
            )

    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    # 호출마다 남긴다 — 비용·지연 집계(#28)가 여기에 붙는다.
    logger.info(
        "%s %s %s %dms",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
        extra={"request_id": request_id},
    )
    response.headers["X-Request-Id"] = request_id
    return response


@app.exception_handler(ServiceError)
async def service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=err(exc.code, exc.message, exc.detail),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI 기본 422 대신 계약의 INVALID_INPUT(400) 으로 내보낸다."""
    # ctx 에 예외 객체가 들어 있을 수 있어 JSON 으로 나가지 않는다. 떼고 보낸다.
    errors = [{k: v for k, v in e.items() if k != "ctx"} for e in exc.errors()]
    return JSONResponse(
        status_code=400,
        content=err(
            ErrorCode.INVALID_INPUT,
            "요청 형식이 계약과 다릅니다",
            {"errors": errors},
        ),
    )


app.include_router(health_router)
app.include_router(router)
