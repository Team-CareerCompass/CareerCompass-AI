"""응답 봉투와 에러 코드.

계약 「공통 규약」. BE 가 그대로 감싸 내보낼 수 있게 API_SPEC §9 의 코드를 쓴다.
"""

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """이 서비스가 내는 것은 이 다섯 개뿐이다."""

    INVALID_INPUT = "INVALID_INPUT"
    PROFILE_INCOMPLETE = "PROFILE_INCOMPLETE"
    PARSING_FAILED = "PARSING_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"


HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_INPUT: 400,
    ErrorCode.PROFILE_INCOMPLETE: 422,
    ErrorCode.PARSING_FAILED: 422,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.LLM_UNAVAILABLE: 503,
}


class FailReason(StrEnum):
    """계약 §1.3. `failed` 의 사유 — BE 가 사용자에게 다른 말을 해야 해서 구분한다."""

    NOT_A_POSTING = "NOT_A_POSTING"
    """지원할 수 있는 공고가 아니다. BE 는 이것만 목록에서 제외한다."""

    NO_KEYWORDS = "NO_KEYWORDS"
    IMAGE_ONLY = "IMAGE_ONLY"
    EMPTY = "EMPTY"


class ServiceError(Exception):
    """계약대로의 에러 응답으로 변환되는 예외."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    @property
    def status_code(self) -> int:
        return HTTP_STATUS[self.code]


def ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def err(code: ErrorCode, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": str(code), "message": message}
    if detail:
        body["detail"] = detail
    return {"ok": False, "error": body}
