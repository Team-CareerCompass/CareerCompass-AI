"""계약 v0.2 대로의 더미 응답.

모델은 아직 부르지 않는다. 여기 있는 것은 전부 고정값이다.

**시나리오 스위치** — 고정 응답만 내려주면 BE 가 실패 경로를 짤 수 없다. BE 는 `postingId` 를
보내지 않으므로 **제목에 표식을 넣어** 고른다.

    {"title": "[stub:NO_KEYWORDS] 아무거나", "rawContent": "..."}

| 표식 | 결과 |
| --- | --- |
| `[stub:NO_KEYWORDS]` | 파싱 실패 — 키워드 부족 |
| `[stub:IMAGE_ONLY]` | 파싱 실패 — 본문이 이미지뿐 |
| `[stub:EMPTY]` | 파싱 실패 — 본문이 비어 있음 |
| `[stub:NOT_A_POSTING]` | 파싱 실패 — 공고가 아님 |
| `[stub:PARTIAL]` | 성공하되 마감일·양식이 없음 |
| 표식 없음 | 정상 |
"""

import re

from app.contract import FAIL_MESSAGES, ParseFailReason
from app.schemas import (
    CommentsRequest,
    CommentsResult,
    DraftRequest,
    DraftResult,
    FactCheck,
    FormQuestion,
    ParseFailure,
    ParseRequest,
    ParseResult,
    PostingType,
    Usage,
)

MAX_RAW_CONTENT_CHARS = 40_000
DEFAULT_MIN_CHARS = 400
DEFAULT_MAX_CHARS = 600

_MARKER = re.compile(r"\[stub:([A-Z_]+)\]")


def _usage(model: str = "stub", prompt_version: str = "v1") -> Usage:
    return Usage(provider="stub", model=model, prompt_version=prompt_version)


def _marker(title: str) -> str | None:
    match = _MARKER.search(title)
    return match.group(1) if match else None


# --------------------------------------------------------------------------
# §1 파싱
# --------------------------------------------------------------------------


def parse_posting(req: ParseRequest, prompt_version: str = "v1") -> ParseResult | ParseFailure:
    marker = _marker(req.title)

    if marker is not None and marker in ParseFailReason.__members__:
        reason = ParseFailReason[marker]
        return ParseFailure(
            reason=FAIL_MESSAGES[reason],
            reason_code=str(reason),
            usage=_usage(prompt_version=prompt_version),
        )

    truncated = len(req.raw_content) > MAX_RAW_CONTENT_CHARS

    if marker == "PARTIAL":
        return ParseResult(
            type=PostingType.SCHOLARSHIP,
            keywords=["장학금", "성적우수", "재학생"],
            qualification_year="3학년 이상",
            qualification_gpa="3.5 이상",
            preferences=[],
            due_date=None,
            due_date_raw=None,
            form_questions=[],
            truncated=truncated,
            usage=_usage(prompt_version=prompt_version),
        )

    return ParseResult(
        type=PostingType.RECRUIT,
        keywords=["Spring", "Kotlin", "Redis", "백엔드"],
        qualification_year="2학년 이상",
        qualification_gpa=None,
        qualification_major=None,
        preferences=["Java/Kotlin 백엔드 경험", "RDB 1년+"],
        due_date="2026-05-25",
        due_date_raw="5월 25일(월) 23:59까지",
        form_questions=[
            FormQuestion(order=1, question="지원 동기를 작성해 주세요.", max_chars=500),
            FormQuestion(order=2, question="본인의 강점과 약점을 서술해 주세요.", max_chars=400),
        ],
        truncated=truncated,
        usage=_usage(prompt_version=prompt_version),
    )


# --------------------------------------------------------------------------
# §2 코멘트
# --------------------------------------------------------------------------


def comments(req: CommentsRequest, prompt_version: str = "v1") -> CommentsResult:
    has_ground = bool(req.matched_keywords or req.matched_preferences)

    if not has_ground and not req.missing_qualifications:
        # 근거가 비면 빈말을 만들지 않는다. BE 는 코멘트 null 을 허용한다.
        return CommentsResult(usage=_usage(prompt_version=prompt_version))

    strength = (
        f"(더미) {', '.join(req.matched_keywords[:2])} 경험이 이 공고의 조건과 맞습니다."
        if has_ground
        else None
    )
    weakness = (
        f"(더미) {req.missing_qualifications[0]} 을(를) 확인할 수 있는 경험이 없습니다."
        if req.missing_qualifications
        else None
    )
    return CommentsResult(
        strength=strength, weakness=weakness, usage=_usage(prompt_version=prompt_version)
    )


# --------------------------------------------------------------------------
# §3 초안
# --------------------------------------------------------------------------

_SENTENCE = "(더미 응답) 지원 분야와 맞닿은 경험을 바탕으로 기여하고자 합니다. "


def draft_answer(req: DraftRequest, prompt_version: str = "v1") -> DraftResult:
    # maxChars 가 0 이면 제한 없음이다 — BE 가 null 을 0 으로 바꿔 보낸다.
    limit = req.max_chars if req.max_chars > 0 else DEFAULT_MAX_CHARS

    body = _SENTENCE * (limit // len(_SENTENCE) + 1)
    answer = body[:limit].rstrip()

    used = list(range(min(2, len(req.experience_summaries))))

    return DraftResult(
        answer=answer,
        char_count=len(answer),
        used_indexes=used,
        fact_check=FactCheck(passed=True, unverified=[]),
        usage=_usage(prompt_version=prompt_version),
    )
