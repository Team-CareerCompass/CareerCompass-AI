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
import re
from datetime import date
from typing import Any, TypeVar

from pydantic import BaseModel, Field, ValidationError, field_validator

from app import rules
from app.budget import Budget, BudgetExceeded
from app.cache import CacheMiss, ReplayCache
from app.config import settings
from app.contract import FAIL_MESSAGES, ErrorCode, ParseFailReason, ServiceError
from app.guard import (
    drop_sentences_with,
    ending_ratio,
    extract_json,
    fact_check,
    safe_draft,
    scrub_pii,
    sentences,
    trim_to_limit,
)
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
"""`maxChars: 0`(제한 없음)일 때의 상한. 하한은 `FILL_MIN` 배 — 400자."""
FILL_MIN = 2 / 3
"""상한 대비 하한 — 프롬프트에 적는 **부탁**이다. 상한과 달리 실측 강제하지 않는다.
v1 실측(09-16): 「500자 이내」라고만 하면 355자, 「300자 이내」는 100자. v2 에서 하한을 적고
미달이면 재요청까지 해 봤지만 평균 48%→48% 로 효과가 없어 재요청은 뺐다(비용만 2배).
HCX-005 는 채우지만 지어내서 채운다. 문장 수 지시가 그나마 듣는다(48→54%). 표는 prompts/README."""
CHARS_PER_SENTENCE = 55
"""글자 수 하한을 문장 수로 바꿀 때의 환산. v1·v2 실측 답의 문장 평균이 50~60자."""
TONE_MIN = 0.8
"""문장 어미 일관성 하한. 미달이면 1회 재요청 — 글자 수와 같은 패턴이다 (#18)."""

PAST_EXCERPT_PREFIX = "과거 자소서 발췌:"
"""BE 가 experienceSummaries 끝에 붙이는 항목의 접두어 (`DraftContextBuilder.pastExcerpt`)."""

TONE_RULES = {
    "formal": "격식체. 모든 문장을 「~습니다」「~합니다」「~입니다」로 끝낸다. "
    "지원서에 바로 쓸 수 있는 정중한 어조",
    # v1 「친근한 문체. ~해요체」는 14건 중 12건이 합니다체로 나왔다 — 어미를 못박고 금지를 적는다
    "casual": "친근한 구어체. **모든 문장**을 「~해요」「~어요」「~였어요」「~죠」로 끝낸다. "
    "「~합니다」「~습니다」「~입니다」는 한 문장도 쓰지 않는다. "
    "예: 「백엔드를 맡았어요.」「그때 협업을 배웠죠.」",
    # BE `ApplicationService.TONES` 는 셋이다 — FE 는 둘만 보내지만 API 로는 올 수 있다
    "confident": "자신감 있는 문체. 모든 문장을 「~니다」로 끝내되 단정적으로 — 「~할 수 있습니다」"
    "「~해냈습니다」. 겸손한 완곡 표현(「부족하지만」「감히」)은 쓰지 않는다. "
    "사실은 경험 요약 그대로",
}
TONE_LABEL = {"formal": "「~습니다」체", "casual": "「~해요」체", "confident": "「~니다」체"}

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


TOKENS_PER_CHAR = 1.0
"""비용 추정용. HCX 실측은 한국어 1자당 0.6~0.8 토큰이라 이 값은 넉넉하다 — 상한은 보수적으로."""


class Gateway:
    def __init__(
        self,
        provider: Provider,
        cache: ReplayCache | None = None,
        budget: Budget | None = None,
    ) -> None:
        self.provider = provider
        self.cache = cache or ReplayCache()
        self.budget = budget

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
        if self.budget is not None:
            # 호출 전에 막는다 — 이 호출이 최대로 쓸 수 있는 돈으로 (#31)
            estimate = (
                (len(prompt.system) + len(user)) * TOKENS_PER_CHAR * self.provider.price_in_krw
                + max_tokens * self.provider.price_out_krw
            )
            try:
                self.budget.check(estimate)
            except BudgetExceeded as exc:
                logger.error("예산 차단 (%s): %s", prompt.name, exc)
                raise ServiceError(ErrorCode.LLM_UNAVAILABLE, str(exc)) from exc
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
        if self.budget is not None:
            self.budget.record(cost_krw(self.provider, completion))
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

        if pre.injections:
            logger.warning(
                "지시문 의심 문단 %d개 제거 (postingId=%s)", pre.injections, req.posting_id
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

        # 40자 넘는 「키워드」는 문장이다 — 주입된 지시문이 키워드 자리로 새는 길을 막는다
        keywords = _dedupe(
            k.strip() for k in llm.keywords if k and k.strip() and len(k.strip()) <= 40
        )[:MAX_KEYWORDS]
        if len(keywords) < MIN_KEYWORDS:
            failure = fail(ParseFailReason.NO_KEYWORDS)
            failure.usage = self._usage(prompt_version, completions)
            return failure

        # 합치기 — 규칙이 낸 값이 우선이다. LLM 문항은 민감정보 요구를 거른다 (#29).
        questions = rule_questions or [
            rules.FormQuestion(order=i + 1, question=q.question.strip(), max_chars=q.maxChars)
            for i, q in enumerate(
                q for q in llm.formQuestions if not rules.SENSITIVE_QUESTION.search(q.question)
            )
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
        strength = scrub_pii(strength)[0] if strength else None
        weakness = scrub_pii(weakness)[0] if weakness else None
        return CommentsResult(
            strength=strength, weakness=weakness, usage=self._usage(prompt_version, completions)
        )

    # ---- §3 초안 ----------------------------------------------------------

    async def draft_answer(self, req: DraftRequest, prompt_version: str = "v1") -> DraftResult:
        limit = req.max_chars if req.max_chars > 0 else 0
        # 상한만 말하면 짧게 끝낸다 — 하한도 준다. 상한만 아래서 실측으로 강제한다.
        ceiling = limit or DEFAULT_DRAFT_CHARS
        floor = int(ceiling * FILL_MIN)
        # 「N자 이상」은 DASH-002 가 못 따른다(v2 실측 평균 48%). 문장 수로도 말한다 —
        # 소형 모델은 구조 지시를 더 잘 따른다. 자소서 문장 하나 ≈ 55자.
        min_sentences = max(2, round(floor / CHARS_PER_SENTENCE))
        length_rule = (
            f"{floor}자 이상 {ceiling}자 이하 (공백 포함) — 문장 {min_sentences}개 이상. "
            "경험마다 상황·행동·결과를 각각 한 문장 이상으로 쓴다"
        )
        tone = req.tone if req.tone in TONE_RULES else "formal"
        tone_rule = TONE_RULES[tone]
        # BE `DraftContextBuilder.summariesFor` 는 경험 카드 최대 3개 뒤에 「과거 자소서 발췌: …」를
        # 붙인다 (BE #51). 경험이 아니라 **문체 참고**다 — 사실 근거로도, 안전 초안의 「경험」으로도
        # 쓰지 않는다. 인덱스는 원래 배열 기준으로 유지한다 (BE 가 usedIndexes 를 그 배열로 읽는다).
        cards = [
            (i, x)
            for i, x in enumerate(req.experience_summaries)
            if not x.startswith(PAST_EXCERPT_PREFIX)
        ]
        excerpts = [x for x in req.experience_summaries if x.startswith(PAST_EXCERPT_PREFIX)]
        if not cards:
            # 근거가 0 이면 검증할 것도 0 이다. v2 실측(003)에서 모델은 「컴퓨터공학 전공」
            # 「환경 보호 연구」「[지원자 이름]」을 만들었다 — 전부 날조, 문자열 대조로 못 잡는다.
            logger.info("경험 요약 0개 → 모델을 부르지 않고 안전 초안")
            answer = safe_draft(req.question, req.posting_title, [], tone=tone, limit=limit)
            return DraftResult(
                answer=answer,
                char_count=len(answer),
                fact_check=FactCheck(passed=True, unverified=[], fallback=True),
                usage=Usage(
                    provider=self.provider.name,
                    model=self.provider.model,
                    prompt_version=prompt_version,
                ),
            )
        experiences = "\n".join(f"[{i}] {x}" for i, x in cards)
        if excerpts:
            experiences += (
                "\n\n(아래는 지원자가 예전에 쓴 자소서 일부다. 문체만 참고하고, "
                "거기 적힌 사실을 이 답에 옮기지 않는다)\n"
                + "\n".join(x.removeprefix(PAST_EXCERPT_PREFIX).strip() for x in excerpts)
            )

        prompt = load_prompt("draft_answer", settings.draft_prompt_version)
        user = prompt.render(
            posting_title=req.posting_title,
            keywords=", ".join(req.keywords),
            question=req.question,
            experiences=experiences,
            length_rule=length_rule,
            tone_rule=tone_rule,
        )
        # HCX 토큰 ≈ 한국어 1자 안팎. 상한의 두 배를 준다 — 문장 중간에서 끊기지 않게.
        max_tokens = max(400, ceiling * 2)
        sources = [x for _, x in cards] + [req.posting_title, req.question]
        fallback = False
        try:
            llm, completions = await self._complete_json(
                prompt, user, _LlmDraft, max_tokens=max_tokens, temperature=0.7
            )
            answer = llm.answer.strip()
            used_raw = llm.usedIndexes
        except SchemaViolation as exc:
            # 모델이 두 번 다 형식을 어겼다. 503 으로 BE 재시도를 부르는 대신 안전 초안을 낸다 —
            # 재시도해도 같은 모델이 같은 짓을 한다.
            logger.warning("초안 형식 최종 실패 → 안전 초안: %s", exc)
            answer, used_raw, completions, fallback = "", [], [], True

        ratio = ending_ratio(answer, tone) if answer else None
        if ratio is not None and ratio < TONE_MIN:
            # 프롬프트로 부탁한 문체를 실측으로 강제한다 (#18). v1 실측에서 casual 은 부탁만으로는
            # 14건 중 2건만 지켰다. 1회 재요청 — 어미 비율이 오르면 받는다.
            tone_user = (
                f"{user}\n\n(직전 초안이 문체를 어겼다: 문장 {len(sentences(answer))}개 중 "
                f"{int(ratio * len(sentences(answer)))}개만 {TONE_LABEL[tone]}였다. "
                f"**모든 문장**을 {TONE_LABEL[tone]}로 끝내서 다시 쓴다. 사실은 그대로.)"
            )
            try:
                retoned, more = await self._complete_json(
                    prompt, tone_user, _LlmDraft, max_tokens=max_tokens, temperature=0.5
                )
                completions.extend(more)
                candidate = retoned.answer.strip()
                if candidate and (ending_ratio(candidate, tone) or 0.0) > ratio:
                    answer, used_raw = candidate, retoned.usedIndexes
            except SchemaViolation:
                pass

        if answer and limit and len(answer) > limit:
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
                    answer, used_raw = shorter.answer.strip(), shorter.usedIndexes
            except SchemaViolation:
                pass
            answer = trim_to_limit(answer, limit)

        # 근거는 **사용자의 경험**이다. 공고 키워드는 근거가 아니다 — 첫 실측에서 keywords 의
        # Redis 를 「사용해 본 경험」으로 쓴 초안이 통과했다(#29). 공고 제목·질문은 회사명·주제가
        # 답에 나오는 것이 당연하므로 남긴다.
        unverified = fact_check(answer, sources) if answer else []
        if unverified:
            # ① 1회 재요청 — 무엇이 근거에 없는지 짚어서
            retry_user = (
                f"{user}\n\n(직전 초안에 근거에 없는 표현이 있었다: {', '.join(unverified)}. "
                "이것들을 빼고, 경험 요약에 있는 사실만으로 다시 쓴다.)"
            )
            try:
                redo, more = await self._complete_json(
                    prompt, retry_user, _LlmDraft, max_tokens=max_tokens, temperature=0.5
                )
                completions.extend(more)
                candidate = redo.answer.strip()
                if limit:
                    candidate = trim_to_limit(candidate, limit)
                if len(fact_check(candidate, sources)) < len(unverified):
                    answer, used_raw = candidate, redo.usedIndexes
                    unverified = fact_check(answer, sources)
            except SchemaViolation:
                pass
        if unverified:
            # ② 규칙으로 해당 문장을 통째로 뺀다. 표현만 지우면 문장이 거짓말로 남는다
            stripped = drop_sentences_with(answer, unverified)
            if len(stripped) >= max(40, len(answer) // 3):
                answer = stripped
                unverified = fact_check(answer, sources)
        if unverified or not answer:
            # ③ 그래도 남으면 모델 출력을 버리고 입력 문자열만으로 만든 초안을 낸다.
            #    빤하지만 거짓이 없다. 사용자가 고쳐 쓰는 출발점.
            logger.warning("초안 사실검증 최종 실패 → 안전 초안: %s", unverified)
            answer = safe_draft(
                req.question,
                req.posting_title,
                [x for _, x in cards],
                tone=tone,
                limit=limit,
            )
            used_raw = [i for i, _ in cards[:3]]
            unverified, fallback = [], True

        answer, pii_hits = scrub_pii(answer)
        if pii_hits:
            logger.warning("초안 출력에서 개인정보 모양 %d건 제거", pii_hits)

        # 발췌 항목의 인덱스는 「인용한 경험」이 아니다
        valid = {i for i, _ in cards}
        used = sorted(set(used_raw) & valid)
        return DraftResult(
            answer=answer,
            char_count=len(answer),
            used_indexes=used,
            fact_check=FactCheck(passed=not unverified, unverified=unverified, fallback=fallback),
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


_TOKEN = re.compile(r"[A-Za-z0-9+#.]{2,}|[가-힣]{2,}")


def _cites(text: str, ground: str) -> bool:
    """근거 하나를 문장이 지목했는가.

    글자 그대로 포함이면 물론이고, 근거의 **토큰 절반 이상**이 나와도 지목으로 본다 —
    「RDB 1년 이상」을 모델이 「RDB 경력 1년」이라고 바꿔 쓴 것까지 버리면 weakness 가
    거의 항상 null 이 된다(첫 실측 #14). 두 글자 미만 토큰은 세지 않는다.
    """
    compact, g = text.replace(" ", "").lower(), ground.replace(" ", "").lower()
    if g and g in compact:
        return True
    tokens = [t.lower() for t in _TOKEN.findall(ground)]
    if not tokens:
        return False
    hits = sum(t in compact for t in tokens)
    return hits * 2 >= len(tokens)


def _keep_if_cites(sentence: str | None, grounds: list[str]) -> str | None:
    """문장이 근거 중 하나라도 지목해야 살린다. 최대 3줄."""
    if not sentence or not sentence.strip():
        return None
    text = "\n".join(sentence.strip().splitlines()[:3])
    if any(_cites(text, g) for g in grounds if g):
        return text
    return None
