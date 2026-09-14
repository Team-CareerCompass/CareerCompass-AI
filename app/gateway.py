"""게이트웨이 — 엔드포인트 셋의 실제 파이프라인 (#2 #3 #4 #35).

    요청 → 전처리(#39) → 규칙(#8 #10) → [필요하면] LLM → 스키마 검증(#4) → 가드(#29) → 응답

**규칙이 뽑을 수 있는 것은 LLM 에 묻지 않는다.** 마감일·글자수·학점학년·우대 헤딩은 규칙이
100% 를 내고 있고(§7), LLM 은 규칙이 못 하는 것 — 키워드 의미 선별·유형 애매 케이스·뭉친
문항 — 만 맡는다. 결과를 합칠 때도 규칙이 낸 값이 우선이다.

프로바이더 실패는 여기서 `ServiceError` 로 바꾼다 — BE 가 429·503 만 재시도한다.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, TypeVar

from pydantic import BaseModel, Field, ValidationError, field_validator

from app import rules
from app.cache import CacheMiss, ReplayCache
from app.config import settings
from app.contract import FAIL_MESSAGES, ErrorCode, ParseFailReason, ServiceError
from app.guard import extract_json, fact_check, trim_to_limit
from app.preprocess import preprocess
from app.prompts import Prompt, load_prompt
from app.providers.base import (
    Completion,
    InputError,
    Provider,
    RateLimited,
    SchemaViolation,
    TransientError,
    complete_with_retry,
    cost_krw,
)
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

logger = logging.getLogger("careercompass.ai.gateway")

MAX_KEYWORDS = 10
MIN_KEYWORDS = 3
MAX_QUESTIONS = 10  # FE #413
LLM_BODY_CHARS = 16_000
"""LLM 에 넣는 본문 상한. 계약의 40,000 은 받는 상한이고, 이건 비용 상한이다."""
DEFAULT_DRAFT_CHARS = 600

_T = TypeVar("_T", bound=BaseModel)


# --------------------------------------------------------------------------
# LLM 출력 스키마 (#4) — 계약 스키마와 별개다. 모델이 지켜야 할 최소 모양.
# --------------------------------------------------------------------------


class _LlmQuestion(BaseModel):
    order: int = 1
    question: str = Field(min_length=4)
    maxChars: int | None = None  # 프롬프트가 camelCase 로 요구한다


class _LlmParse(BaseModel):
    """목록 길이는 여기서 막지 않는다 — 모델이 44개를 내면 위반이 아니라 앞 10개를 쓴다.

    첫 v2 실측에서 두산(직무 44개)이 `max_length=30` 에 걸려 재요청까지 실패했고, 규칙이 뽑은
    마감일·문항까지 같이 버려졌다. 너무 많이 낸 것은 자르면 되지만, 실패시키면 전부 잃는다.
    """

    type: str | None = None
    keywords: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    formQuestions: list[_LlmQuestion] = Field(default_factory=list)

    @field_validator("keywords", "preferences", "formQuestions", mode="before")
    @classmethod
    def _null_is_empty(cls, v: Any) -> Any:
        """모델이 「없음」을 `null` 로 내는 것은 위반이 아니다 — 빈 배열로 읽는다 (v5 실측)."""
        return [] if v is None else v


class _LlmComments(BaseModel):
    strength: str | None = None
    weakness: str | None = None


class _LlmDraft(BaseModel):
    answer: str = Field(min_length=1)
    usedIndexes: list[int] = Field(default_factory=list)


# --------------------------------------------------------------------------


class Gateway:
    def __init__(self, provider: Provider, cache: ReplayCache | None = None) -> None:
        self.provider = provider
        self.cache = cache or ReplayCache()

    # ---- 공통 ------------------------------------------------------------

    async def _complete(
        self, prompt: Prompt, user: str, *, max_tokens: int, temperature: float
    ) -> Completion:
        key = ReplayCache.key(
            self.provider.name,
            self.provider.model,
            prompt.version,
            prompt.system,
            user,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        try:
            hit = self.cache.get(key)
        except CacheMiss as exc:
            raise ServiceError(
                ErrorCode.LLM_UNAVAILABLE, f"리플레이 캐시에 없는 요청 ({prompt.name})"
            ) from exc
        if hit is not None:
            return hit
        try:
            completion = await complete_with_retry(
                self.provider, prompt.system, user, max_tokens=max_tokens, temperature=temperature
            )
        except RateLimited as exc:
            raise ServiceError(ErrorCode.RATE_LIMITED, str(exc)) from exc
        except (TransientError, InputError) as exc:
            logger.warning("프로바이더 실패 (%s): %s", prompt.name, exc)
            raise ServiceError(ErrorCode.LLM_UNAVAILABLE, str(exc)) from exc
        self.cache.put(key, completion, note=f"{prompt.name}.{prompt.version}")
        return completion

    async def _complete_json(
        self,
        prompt: Prompt,
        user: str,
        model: type[_T],
        *,
        max_tokens: int,
        temperature: float,
    ) -> tuple[_T, list[Completion]]:
        """스키마를 어기면 **1회** 재요청. 그래도 어기면 `SchemaViolation` (#4)."""
        completions: list[Completion] = []
        attempt_user = user
        last_error = ""
        for attempt in range(2):
            completion = await self._complete(
                prompt, attempt_user, max_tokens=max_tokens, temperature=temperature
            )
            completions.append(completion)
            try:
                return model.model_validate(extract_json(completion.text)), completions
            except (SchemaViolation, ValidationError) as exc:
                last_error = str(exc).splitlines()[0]
                logger.warning("스키마 위반 (%s, %d회): %s", prompt.name, attempt + 1, last_error)
                attempt_user = (
                    f"{user}\n\n(직전 응답이 형식을 어겼다: {last_error}. "
                    "설명 없이 JSON 객체 하나만 출력한다.)"
                )
        raise SchemaViolation(last_error)

    def _usage(self, prompt_version: str, completions: list[Completion]) -> Usage:
        prompt_tokens = sum(c.prompt_tokens for c in completions)
        completion_tokens = sum(c.completion_tokens for c in completions)
        return Usage(
            provider=self.provider.name,
            model=self.provider.model,
            prompt_version=prompt_version,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            latency_ms=sum(c.latency_ms for c in completions),
            cost_krw=round(sum(cost_krw(self.provider, c) for c in completions), 4),
        )

    # ---- §1 파싱 ----------------------------------------------------------

    async def parse_posting(
        self, req: ParseRequest, prompt_version: str = "v1"
    ) -> ParseResult | ParseFailure:
        pre = preprocess(req.raw_content)
        empty_usage = Usage(
            provider=self.provider.name, model=self.provider.model, prompt_version=prompt_version
        )

        def fail(reason: ParseFailReason) -> ParseFailure:
            return ParseFailure(
                reason=FAIL_MESSAGES[reason], reason_code=str(reason), usage=empty_usage
            )

        # 모델을 부르기 전에 규칙이 거를 수 있는 것은 거른다 — 비용이 0 이다.
        if pre.too_short:
            return fail(ParseFailReason.IMAGE_ONLY if req.images else ParseFailReason.EMPTY)

        collected = date.fromisoformat(req.collected_at) if req.collected_at else None
        due = rules.extract_due_date(pre.text, collected)
        if not rules.posting_signals(pre.text, due, req.title).likely_posting:
            return fail(ParseFailReason.NOT_A_POSTING)

        rule_type = rules.guess_type(req.title, pre.text)
        rule_questions = rules.extract_form_questions(pre.text)
        quals = rules.extract_qualifications(pre.text)
        rule_prefs = rules.extract_preferences(pre.text)

        prompt = load_prompt("parse_posting", settings.parse_prompt_version)
        user = prompt.render(title=req.title, body=pre.text[:LLM_BODY_CHARS])
        try:
            llm, completions = await self._complete_json(
                prompt, user, _LlmParse, max_tokens=800, temperature=0.1
            )
        except SchemaViolation as exc:
            logger.warning("파싱 스키마 최종 실패: %s", exc)
            return fail(ParseFailReason.NO_KEYWORDS)

        keywords = _dedupe(k.strip() for k in llm.keywords if k and k.strip())[:MAX_KEYWORDS]
        if len(keywords) < MIN_KEYWORDS:
            failure = fail(ParseFailReason.NO_KEYWORDS)
            failure.usage = self._usage(prompt_version, completions)
            return failure

        # 합치기 — 규칙이 낸 값이 우선이다.
        questions = rule_questions or [
            rules.FormQuestion(order=i + 1, question=q.question.strip(), max_chars=q.maxChars)
            for i, q in enumerate(llm.formQuestions)
        ]
        return ParseResult(
            type=_posting_type(rule_type) or _posting_type(llm.type),
            keywords=keywords,
            qualification_year=quals.year,
            qualification_gpa=quals.gpa,
            qualification_major=quals.major,
            preferences=(rule_prefs or _dedupe(p.strip() for p in llm.preferences if p.strip()))[
                :MAX_KEYWORDS
            ],
            due_date=due.iso,
            due_date_raw=due.raw,
            form_questions=[
                FormQuestion(order=q.order, question=q.question, max_chars=q.max_chars)
                for q in questions[:MAX_QUESTIONS]
            ],
            truncated=pre.truncated,
            usage=self._usage(prompt_version, completions),
        )

    # ---- §2 코멘트 --------------------------------------------------------

    async def comments(self, req: CommentsRequest, prompt_version: str = "v1") -> CommentsResult:
        grounds = [*req.matched_keywords, *req.matched_preferences]
        if req.top_experience_title:
            grounds.append(req.top_experience_title)
        if not grounds and not req.missing_qualifications:
            # 근거가 비면 빈말을 만들지 않는다 — 모델도 부르지 않는다.
            return CommentsResult(
                usage=Usage(
                    provider=self.provider.name,
                    model=self.provider.model,
                    prompt_version=prompt_version,
                )
            )

        prompt = load_prompt("comments")
        user = prompt.render(
            matched_keywords=_jlist(req.matched_keywords),
            matched_preferences=_jlist(req.matched_preferences),
            top_experience_title=json.dumps(req.top_experience_title, ensure_ascii=False),
            missing_qualifications=_jlist(req.missing_qualifications),
        )
        try:
            llm, completions = await self._complete_json(
                prompt, user, _LlmComments, max_tokens=300, temperature=0.3
            )
        except SchemaViolation:
            # 코멘트는 없어도 점수는 나간다. 이상한 문장보다 null 이 낫다.
            return CommentsResult(usage=self._usage(prompt_version, []))

        # #14 방어 — 근거를 하나도 지목하지 않은 문장은 버린다. BE 의 우대 매칭이 과대매칭이라
        # 근거 자체가 의심스러운데, 근거조차 안 나오는 문장은 확실히 빈말이다.
        strength = _keep_if_cites(llm.strength, grounds)
        weakness = _keep_if_cites(llm.weakness, req.missing_qualifications)
        return CommentsResult(
            strength=strength, weakness=weakness, usage=self._usage(prompt_version, completions)
        )

    # ---- §3 초안 ----------------------------------------------------------

    async def draft_answer(self, req: DraftRequest, prompt_version: str = "v1") -> DraftResult:
        limit = req.max_chars if req.max_chars > 0 else 0
        length_rule = f"{limit}자 이내" if limit else "400~600자"
        tone_rule = (
            "친근한 문체. 「~해요」체. 딱딱한 격식 표현을 피한다"
            if req.tone == "casual"
            else "격식 있는 문체. 「~합니다」체. 지원서에 바로 쓸 수 있는 정중한 어조"
        )
        experiences = "\n".join(f"[{i}] {s}" for i, s in enumerate(req.experience_summaries))
        if not experiences:
            experiences = (
                "(없음 — 경험 요약이 비어 있다. "
                "질문에 대한 일반적 태도만 쓰고 사실은 만들지 않는다)"
            )

        prompt = load_prompt("draft_answer")
        user = prompt.render(
            posting_title=req.posting_title,
            keywords=", ".join(req.keywords),
            question=req.question,
            experiences=experiences,
            length_rule=length_rule,
            tone_rule=tone_rule,
        )
        # HCX 토큰 ≈ 한국어 1자 안팎. 상한의 두 배를 준다 — 문장 중간에서 끊기지 않게.
        max_tokens = max(400, (limit or DEFAULT_DRAFT_CHARS) * 2)
        try:
            llm, completions = await self._complete_json(
                prompt, user, _LlmDraft, max_tokens=max_tokens, temperature=0.7
            )
        except SchemaViolation as exc:
            raise ServiceError(ErrorCode.LLM_UNAVAILABLE, f"초안 형식 오류: {exc}") from exc

        answer = llm.answer.strip()
        if limit and len(answer) > limit:
            # 프롬프트로 부탁한 것을 실측으로 강제한다 (#16).
            # 1회 단축 재요청 → 그래도 넘으면 문장 경계에서 자른다.
            shorten_user = (
                f"{user}\n\n(직전 초안이 {len(answer)}자다. {limit}자 이내로 줄여 다시 쓴다.)"
            )
            try:
                shorter, more = await self._complete_json(
                    prompt, shorten_user, _LlmDraft, max_tokens=max_tokens, temperature=0.5
                )
                completions.extend(more)
                if len(shorter.answer.strip()) <= len(answer):
                    answer, llm = shorter.answer.strip(), shorter
            except SchemaViolation:
                pass
            answer = trim_to_limit(answer, limit)

        if not answer:
            raise ServiceError(ErrorCode.LLM_UNAVAILABLE, "초안이 비었다")

        n = len(req.experience_summaries)
        used = sorted({i for i in llm.usedIndexes if 0 <= i < n})
        unverified = fact_check(
            answer, [*req.experience_summaries, *req.keywords, req.posting_title, req.question]
        )
        return DraftResult(
            answer=answer,
            char_count=len(answer),
            used_indexes=used,
            fact_check=FactCheck(passed=not unverified, unverified=unverified),
            usage=self._usage(prompt_version, completions),
        )


# --------------------------------------------------------------------------


def _dedupe(items: Any) -> list[str]:
    return list(dict.fromkeys(items))


def _jlist(items: list[str]) -> str:
    return json.dumps(items, ensure_ascii=False)


def _posting_type(value: str | None) -> PostingType | None:
    if value is None:
        return None
    try:
        return PostingType(value)
    except ValueError:
        return None


def _keep_if_cites(sentence: str | None, grounds: list[str]) -> str | None:
    """문장이 근거 중 하나라도 **글자 그대로** 담고 있어야 살린다. 최대 3줄."""
    if not sentence or not sentence.strip():
        return None
    text = "\n".join(sentence.strip().splitlines()[:3])
    compact = text.replace(" ", "").lower()
    if any(g and g.replace(" ", "").lower() in compact for g in grounds):
        return text
    return None
