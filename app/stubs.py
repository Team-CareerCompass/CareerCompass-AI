"""계약대로의 더미 응답.

#1 의 완료 조건은 「BE 가 이 서비스를 호출해 더미 응답을 받을 수 있다」이다.
모델은 아직 부르지 않는다. 여기 있는 것은 전부 고정값이다.

**시나리오 스위치** — 고정 응답만 내려주면 BE 가 실패 화면을 짤 수 없다.
`postingId` 로 응답을 골라 낼 수 있게 해 둔다.

| postingId | 결과 |
| --- | --- |
| 999001 | `status: partial` (마감일·양식 없음) |
| 999002 | `PARSING_FAILED` / `NO_KEYWORDS` |
| 999003 | `PARSING_FAILED` / `IMAGE_ONLY` |
| 999004 | `PARSING_FAILED` / `NOT_A_POSTING` |
| 999005 | `PARSING_FAILED` / `EMPTY` |
| 그 밖 | `status: ok` |
"""

import pathlib

from app.contract import ErrorCode, FailReason, ServiceError
from app.schemas import (
    Axis,
    ClassifiedItem,
    ClassifyRequest,
    ClassifyResult,
    DocFormat,
    EmbedRequest,
    EmbedResult,
    ExtractTextResult,
    FactCheck,
    FormQuestion,
    GenerateRequest,
    GenerateResult,
    ItemCategory,
    Meta,
    ParseRequest,
    ParseResult,
    ParseStatus,
    PostingType,
    Qualifications,
    ScoreRequest,
    ScoreResult,
)

FAIL_SCENARIOS: dict[int, FailReason] = {
    999002: FailReason.NO_KEYWORDS,
    999003: FailReason.IMAGE_ONLY,
    999004: FailReason.NOT_A_POSTING,
    999005: FailReason.EMPTY,
}

PARTIAL_SCENARIO = 999001

FAIL_MESSAGES: dict[FailReason, str] = {
    FailReason.NO_KEYWORDS: "핵심 키워드를 3개 이상 뽑지 못했습니다",
    FailReason.IMAGE_ONLY: "본문이 이미지뿐입니다",
    FailReason.NOT_A_POSTING: "지원할 수 있는 공고가 아닙니다",
    FailReason.EMPTY: "본문이 비어 있습니다",
    FailReason.SCANNED_PDF: "텍스트 레이어가 없어 내용을 읽지 못했습니다",
}


def _meta(model: str = "stub", prompt_version: str = "stub@0", latency_ms: int = 0) -> Meta:
    return Meta(
        provider="stub",
        model=model,
        prompt_version=prompt_version,
        latency_ms=latency_ms,
        cost_krw=0.0,
    )


def _fail(reason: FailReason) -> ServiceError:
    return ServiceError(
        ErrorCode.PARSING_FAILED,
        FAIL_MESSAGES[reason],
        {"reason": str(reason)},
    )


# --------------------------------------------------------------------------
# §1 파싱
# --------------------------------------------------------------------------


def parse(req: ParseRequest) -> ParseResult:
    if (reason := FAIL_SCENARIOS.get(req.posting_id)) is not None:
        raise _fail(reason)

    truncated = len(req.raw_content) > 40_000

    if req.posting_id == PARTIAL_SCENARIO:
        return ParseResult(
            posting_id=req.posting_id,
            status=ParseStatus.PARTIAL,
            truncated=truncated,
            missing=["dueDate", "formQuestions"],
            type=PostingType.SCHOLARSHIP,
            organization="건국대학교",
            due_date=None,
            due_date_raw=None,
            keywords=["장학금", "성적우수", "재학생"],
            qualifications=Qualifications(year="3학년 이상", gpa="3.5 이상", major=None),
            preferences=[],
            work_type=None,
            form_questions=[],
            meta=_meta(),
        )

    return ParseResult(
        posting_id=req.posting_id,
        status=ParseStatus.OK,
        truncated=truncated,
        type=PostingType.RECRUIT,
        organization="OO기업",
        due_date="2026-05-25",
        due_date_raw="5월 25일(월) 23:59까지",
        keywords=["Spring", "Kotlin", "Redis", "백엔드"],
        qualifications=Qualifications(year="2학년 이상", gpa=None, major=None),
        preferences=["Java/Kotlin 백엔드 경험", "RDB 1년+"],
        work_type="인턴",
        form_questions=[
            FormQuestion(order=1, question="지원 동기를 작성해 주세요.", max_chars=500),
            FormQuestion(order=2, question="본인의 강점과 약점을 서술해 주세요.", max_chars=400),
        ],
        meta=_meta(),
    )


# --------------------------------------------------------------------------
# §2 적합도
# --------------------------------------------------------------------------


def score(req: ScoreRequest) -> ScoreResult:
    has_interests = bool(req.profile.job_interests or req.profile.tags)
    has_experiences = bool(req.experiences)

    if not has_interests and not has_experiences:
        raise ServiceError(
            ErrorCode.PROFILE_INCOMPLETE,
            "관심 분야 또는 경험 카드가 최소 1개 필요합니다",
            {"missing": ["jobInterests", "experiences"]},
        )

    cited = [req.experiences[0].id] if has_experiences else []

    # 경쟁 강도는 근거가 없으면 null 이고, 그만큼 나머지 축에 가중치를 배분한다 (#12).
    breakdown = [
        Axis(
            axis="field_similarity",
            score=95,
            weight=44,
            basis="관심분야 ↔ 공고 키워드 매칭 (더미)",
        ),
        Axis(axis="qualification", score=88, weight=33, basis="자격 조건 충족 (더미)"),
        Axis(
            axis="preference",
            score=78 if has_experiences else 0,
            weight=23,
            basis=f"우대 조건 일치 (경험 {cited})" if has_experiences else "경험 카드 없음",
        ),
        Axis(axis="competition", score=None, weight=10, basis=None),
    ]

    return ScoreResult(
        score=88 if has_experiences else 62,
        label="very_suitable" if has_experiences else "suitable",
        provisional=not has_experiences,
        reweighted=True,
        breakdown=breakdown,
        strength_comment="(더미) 보유 경험이 공고 우대 조건과 맞습니다.",
        weakness_comment="(더미) 일부 우대 조건을 확인할 경험이 없습니다.",
        cited_experience_ids=cited,
        meta=_meta(),
    )


# --------------------------------------------------------------------------
# §3 초안 생성
# --------------------------------------------------------------------------

_SENTENCE = "(더미 응답) 지원 분야와 맞닿은 경험을 바탕으로 기여하고자 합니다. "


def generate(req: GenerateRequest) -> GenerateResult:
    limit = req.max_chars if req.max_chars is not None else 500

    body = _SENTENCE * (limit // len(_SENTENCE) + 1)
    answer = body[:limit].rstrip()

    used = req.emphasize_experience_ids or [e.id for e in req.experiences[:1]]

    return GenerateResult(
        answer=answer,
        char_count=len(answer),
        used_experience_ids=used,
        fact_check=FactCheck(passed=True, unverified=[]),
        meta=_meta(),
    )


# --------------------------------------------------------------------------
# §4 문서 분류
# --------------------------------------------------------------------------


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
"""명세서 F1-4. 10MB 이하."""

SUPPORTED_FORMATS: dict[str, DocFormat] = {
    ".pdf": DocFormat.PDF,
    ".docx": DocFormat.DOCX,
    ".txt": DocFormat.TXT,
}

EXTRACT_FAIL_SCENARIOS: dict[int, FailReason] = {
    999002: FailReason.SCANNED_PDF,
    999003: FailReason.EMPTY,
}

_STUB_DOCUMENT = (
    "저는 사용자의 불편을 빠르게 확인하고 도구로 만들어 검증하는 것을 좋아합니다.\n\n"
    "학부 3학년 때 팀 프로젝트에서 백엔드를 맡아 API 설계와 배포를 담당했습니다.\n\n"
    "입사 후에는 데이터 파이프라인 영역에서 기여하고 싶습니다."
)


def extract_text(past_application_id: int, filename: str, size: int) -> ExtractTextResult:
    """§4.3. 형식·크기 위반은 INVALID_INPUT, 읽기 실패는 PARSING_FAILED 다."""
    suffix = pathlib.Path(filename).suffix.lower()

    if (fmt := SUPPORTED_FORMATS.get(suffix)) is None:
        raise ServiceError(
            ErrorCode.INVALID_INPUT,
            "PDF·DOCX·TXT 만 지원합니다",
            {"filename": filename},
        )

    if size > MAX_UPLOAD_BYTES:
        raise ServiceError(
            ErrorCode.INVALID_INPUT,
            "10MB 이하만 올릴 수 있습니다",
            {"size": size, "limit": MAX_UPLOAD_BYTES},
        )

    if (reason := EXTRACT_FAIL_SCENARIOS.get(past_application_id)) is not None:
        raise _fail(reason)

    return ExtractTextResult(
        past_application_id=past_application_id,
        format=fmt,
        text=_STUB_DOCUMENT,
        char_count=len(_STUB_DOCUMENT),
        ocr_used=False,
        meta=_meta(model="local", prompt_version="-"),
    )


def classify(req: ClassifyRequest) -> ClassifyResult:
    paragraphs = [p.strip() for p in req.text.split("\n\n") if p.strip()] or [req.text]

    items = [
        ClassifiedItem(
            order=i + 1,
            category=ItemCategory.MOTIVATION if i == 0 else ItemCategory.OTHER,
            content=p,
            confident=i == 0,
        )
        for i, p in enumerate(paragraphs)
    ]
    return ClassifyResult(items=items, meta=_meta())


# --------------------------------------------------------------------------
# §5 임베딩
# --------------------------------------------------------------------------

_DIM = 1024


def embed(req: EmbedRequest) -> EmbedResult:
    vectors = [[0.0] * _DIM for _ in req.texts]
    return EmbedResult(vectors=vectors, dim=_DIM, model="stub")
