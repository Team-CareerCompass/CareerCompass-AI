"""오류 표현.

계약 v0.2 「실패를 표현하는 법」. **성공 응답에는 봉투가 없다** — 최상위에 필드를 그대로 놓는다.
BE 의 `HttpLlmGateway` 가 `node.path("keywords")` 로 바로 읽기 때문이다.

파싱 실패는 오류가 아니다. `200` + `parsingFailed: true` 로 낸다 — BE 의 `callWithRetry` 가
429·5xx 만 재시도하고 나머지 오류 응답은 전부 `LLM_UNAVAILABLE` 로 바꾸기 때문에,
4xx 로 내면 정상적인 파싱 실패가 서버 장애로 둔갑한다.
"""

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """BE 가 상태 코드만 보므로 본문의 code 는 이쪽 로그용이다."""

    INVALID_INPUT = "INVALID_INPUT"
    RATE_LIMITED = "RATE_LIMITED"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"


HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_INPUT: 400,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.LLM_UNAVAILABLE: 503,
}


class ParseFailReason(StrEnum):
    """계약 §1.3. `reasonCode` — BE 는 무시하고, 이쪽 평가셋과 로그가 쓴다."""

    NO_KEYWORDS = "NO_KEYWORDS"
    IMAGE_ONLY = "IMAGE_ONLY"
    EMPTY = "EMPTY"
    NOT_A_POSTING = "NOT_A_POSTING"
    """지원할 수 있는 공고가 아니다. BE 에 목록 제외 수단이 아직 없다 — 계약 §1.3."""


FAIL_MESSAGES: dict[ParseFailReason, str] = {
    ParseFailReason.NO_KEYWORDS: "핵심 키워드를 3개 이상 뽑지 못했습니다",
    ParseFailReason.IMAGE_ONLY: "본문이 이미지뿐입니다",
    ParseFailReason.EMPTY: "본문이 비어 있습니다",
    ParseFailReason.NOT_A_POSTING: "지원할 수 있는 공고가 아닙니다",
}


class ServiceError(Exception):
    """429·503·400 으로 나가는 예외. 파싱 실패에는 쓰지 않는다."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def status_code(self) -> int:
        return HTTP_STATUS[self.code]


def error_body(code: ErrorCode, message: str) -> dict[str, Any]:
    return {"error": message, "code": str(code)}
