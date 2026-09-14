"""평가셋 러너 (#5) — 픽스처에 파이프라인을 돌리고 지표를 낸다.

프롬프트나 규칙을 고쳤을 때 **좋아졌는지 나빠졌는지를 숫자로** 답하기 위한 것이다.
눈으로 두세 개 보고 넘어가면 다른 유형에서 조용히 나빠진다.

파이프라인은 둘이다 — `rules`(규칙 전용, 기준선) 와 `llm`(게이트웨이 = 규칙 + LLM 병합).
같은 픽스처·같은 채점기라 「규칙 대비 LLM 이 얼마나 나은가」가 숫자로 나온다.
BE 의 `HeuristicLlmGateway` 가 그 아래 기준선이다.

`llm` 은 실제로 모델을 부른다 — `CC_LLM_CACHE=record` 로 한 번 녹화해 두면 그 뒤는 공짜다.
"""

import asyncio
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app import rules
from app.fixtures import Fixture, load_all
from app.preprocess import preprocess
from app.schemas import Image, ParseFailure, ParseRequest


def run_rules(fx: Fixture) -> dict[str, Any]:
    """규칙 전용 파이프라인. LLM 이 붙기 전의 기준선이다."""
    pre = preprocess(fx.body)
    due = rules.extract_due_date(pre.text, fx.collected_at)
    signals = rules.posting_signals(pre.text, due, fx.title)

    return {
        "dueDate": due.iso,
        "dueDateRaw": due.raw,
        "yearInferred": due.year_inferred,
        "dueReason": due.reason,
        "type": rules.guess_type(fx.title, pre.text),
        "formQuestions": [
            {"order": q.order, "question": q.question, "maxChars": q.max_chars}
            for q in rules.extract_form_questions(pre.text)
        ],
        "qualifications": vars(rules.extract_qualifications(pre.text)),
        "preferences": rules.extract_preferences(pre.text),
        "keywords": [],  # 규칙은 키워드를 못 낸다 — 이것이 LLM 비교의 0 기준선이다
        "likelyPosting": signals.likely_posting,
        "truncated": pre.truncated,
        "maskedContacts": len(pre.masked),
        "injectionsStripped": pre.injections,
        "imageOnly": fx.image_only,
    }


def run_llm(fx: Fixture, gateway: Any) -> dict[str, Any]:
    """게이트웨이 파이프라인 — 규칙이 뽑은 것 위에 LLM 이 키워드·유형·문항을 보탠다.

    `run_rules` 와 같은 키를 낸다. 채점기가 둘을 구분하지 않는다.
    """
    req = ParseRequest(
        title=fx.title,
        raw_content=fx.body,
        collected_at=fx.collected_at.isoformat() if fx.collected_at else None,
        images=[Image(url=name, order=i + 1) for i, name in enumerate(fx.images)],
    )
    res = asyncio.run(gateway.parse_posting(req))
    pre = preprocess(fx.body)
    due = rules.extract_due_date(pre.text, fx.collected_at)
    base = {
        "dueDateRaw": due.raw,
        "yearInferred": due.year_inferred,
        "dueReason": due.reason,
        "qualifications": vars(rules.extract_qualifications(pre.text)),
        "truncated": pre.truncated,
        "maskedContacts": len(pre.masked),
        "injectionsStripped": pre.injections,
        "imageOnly": fx.image_only,
    }
    if isinstance(res, ParseFailure):
        return {
            **base,
            "dueDate": None,
            "type": None,
            "formQuestions": [],
            "preferences": [],
            "keywords": [],
            "likelyPosting": res.reason_code != "NOT_A_POSTING",
            "failReason": res.reason_code,
            "usage": res.usage.model_dump(by_alias=True) if res.usage else None,
        }
    return {
        **base,
        "dueDate": res.due_date,
        "type": str(res.type) if res.type else None,
        "formQuestions": [q.model_dump(by_alias=True) for q in res.form_questions],
        "preferences": res.preferences,
        "keywords": res.keywords,
        "likelyPosting": True,
        "failReason": None,
        "usage": res.usage.model_dump(by_alias=True) if res.usage else None,
    }


def grade_due_date(expected: dict[str, Any], actual: dict[str, Any]) -> str:
    """**틀린 날짜가 null 보다 나쁘다** — 없는 마감일을 만들면 사용자가 기회를 놓친다 (#8)."""
    if expected.get("status") == "failed":
        return "skip"
    want, got = expected.get("dueDate", "__missing__"), actual["dueDate"]
    if want == "__missing__":
        return "skip"
    if want is None:
        return "correct_null" if got is None else "hallucinated"
    if got is None:
        return "missed"
    return "correct" if got == want else "wrong"


def grade_type(expected: dict[str, Any], actual: dict[str, Any]) -> str:
    if expected.get("status") == "failed" or expected.get("type") is None:
        return "skip"
    return "correct" if actual["type"] == expected["type"] else "wrong"


def grade_posting(expected: dict[str, Any], actual: dict[str, Any]) -> str:
    """「애초에 공고가 아닌 글」을 거르는가 (계약 §1.3 NOT_A_POSTING).

    학사공지를 통째로 등록하면 수강신청 안내가 함께 수집된다. 공고로 오인하는 것(false_positive)과
    진짜 공고를 걸러 버리는 것(false_negative)은 비용이 다르다 — 후자가 더 나쁘다. 안 들어온 것은
    존재를 모른다.
    """
    if expected.get("reason") == "NOT_A_POSTING":
        return "correct" if not actual["likelyPosting"] else "false_positive"
    if expected.get("status") == "failed" or expected.get("type") is None:
        return "skip"
    return "correct" if actual["likelyPosting"] else "false_negative"


def _norm(s: str) -> str:
    return "".join(s.split()).lower()


def _matches(a: str, b: str) -> bool:
    """공백·대소문자 무시, **어느 쪽이든 포함**이면 같은 것으로 본다.

    「SW개발」과 「SW 개발」, 「숏폼 채널」과 「숏폼 채널 활발한 운영」이 같은 항목이다.
    두 글자 미만은 포함 판정이 헐거워 정확 일치만 인정한다.
    """
    x, y = _norm(a), _norm(b)
    if not x or not y:
        return False
    if len(x) < 2 or len(y) < 2:
        return x == y
    return x in y or y in x


def grade_list(gold: list[str], predicted: list[str], forbidden: list[str]) -> dict[str, int]:
    """키워드·우대 조건처럼 **순서 없는 목록**의 채점. 정확 일치가 아니라 겹침이다.

    - `hit`      정답 중 예측에 잡힌 것 → 재현율의 분자
    - `predHit`  예측 중 정답에 닿는 것 → 정밀도의 분자
    - `forbidden` 뽑으면 안 되는 것(일반어·다른 공고·혜택)이 예측에 든 수. **0 이어야 한다**
    """
    hit = sum(any(_matches(g, p) for p in predicted) for g in gold)
    pred_hit = sum(any(_matches(p, g) for g in gold) for p in predicted)
    bad = sum(any(_matches(p, f) for f in forbidden) for p in predicted)
    return {
        "gold": len(gold),
        "hit": hit,
        "pred": len(predicted),
        "predHit": pred_hit,
        "forbidden": bad,
    }


def grade_keywords(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, int] | None:
    if expected.get("status") == "failed" or "keywords" not in expected:
        return None
    return grade_list(expected["keywords"], actual["keywords"], expected.get("keywordsNot", []))


def grade_preferences(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, int] | None:
    if expected.get("status") == "failed" or "preferences" not in expected:
        return None
    return grade_list(expected["preferences"], actual["preferences"], [])


def grade_questions(expected: dict[str, Any], actual: dict[str, Any]) -> str:
    """개수만 본다. 문항 내용 일치는 표본이 쌓인 뒤에 본다."""
    if expected.get("status") == "failed" or "formQuestions" not in expected:
        return "skip"
    want, got = len(expected["formQuestions"]), len(actual["formQuestions"])
    if want == got:
        return "correct"
    return "false_positive" if got > want else "missed"


@dataclass
class Report:
    pipeline: str
    prompt_version: str | None = None
    """`llm` 파이프라인이 쓴 `parse_posting` 프롬프트 버전. 기록에 무엇으로 돌렸는지 남긴다."""
    model: str | None = None
    """`llm` 파이프라인이 쓴 모델. 같은 프롬프트라도 모델이 다르면 다른 실험이다."""
    due_date: Counter[str] = field(default_factory=Counter)
    type: Counter[str] = field(default_factory=Counter)
    form_questions: Counter[str] = field(default_factory=Counter)
    posting: Counter[str] = field(default_factory=Counter)
    keywords: Counter[str] = field(default_factory=Counter)
    preferences: Counter[str] = field(default_factory=Counter)
    rows: list[dict[str, Any]] = field(default_factory=list)

    @staticmethod
    def _rate(counter: Counter[str], *hits: str) -> float | None:
        total = sum(v for k, v in counter.items() if k != "skip")
        return sum(counter[h] for h in hits) / total if total else None

    @property
    def due_accuracy(self) -> float | None:
        return self._rate(self.due_date, "correct", "correct_null")

    @property
    def type_accuracy(self) -> float | None:
        return self._rate(self.type, "correct")

    @property
    def question_accuracy(self) -> float | None:
        return self._rate(self.form_questions, "correct")

    @property
    def posting_accuracy(self) -> float | None:
        return self._rate(self.posting, "correct")

    @staticmethod
    def _ratio(counter: Counter[str], num: str, den: str) -> float | None:
        return counter[num] / counter[den] if counter[den] else None

    @property
    def keyword_recall(self) -> float | None:
        """정답 키워드 중 뽑힌 비율. 규칙 전용은 0 — LLM 이 얼마나 보태는지의 기준선."""
        return self._ratio(self.keywords, "hit", "gold")

    @property
    def keyword_precision(self) -> float | None:
        """뽑은 키워드 중 정답에 닿는 비율. 낮으면 일반어·잡음이 섞인 것이다."""
        return self._ratio(self.keywords, "predHit", "pred")

    @property
    def keyword_forbidden(self) -> int:
        """뽑으면 안 되는 것(`keywordsNot`)이 나온 수. **0 이어야 한다.**"""
        return self.keywords["forbidden"]

    @property
    def preference_recall(self) -> float | None:
        return self._ratio(self.preferences, "hit", "gold")

    @property
    def preference_precision(self) -> float | None:
        return self._ratio(self.preferences, "predHit", "pred")

    @property
    def hallucinated(self) -> int:
        """날조한 마감일의 수. **이것은 0 이어야 한다.**"""
        return self.due_date["hallucinated"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "pipeline": self.pipeline,
            "promptVersion": self.prompt_version,
            "model": self.model,
            "dueDate": dict(self.due_date),
            "type": dict(self.type),
            "formQuestions": dict(self.form_questions),
            "posting": dict(self.posting),
            "dueAccuracy": self.due_accuracy,
            "typeAccuracy": self.type_accuracy,
            "questionAccuracy": self.question_accuracy,
            "postingAccuracy": self.posting_accuracy,
            "keywords": dict(self.keywords),
            "keywordRecall": self.keyword_recall,
            "keywordPrecision": self.keyword_precision,
            "keywordForbidden": self.keyword_forbidden,
            "preferences": dict(self.preferences),
            "preferenceRecall": self.preference_recall,
            "preferencePrecision": self.preference_precision,
            "rows": self.rows,
        }


def evaluate(
    fixtures: list[Fixture] | None = None,
    *,
    pipeline: str = "rules",
    run: Callable[[Fixture], dict[str, Any]] | None = None,
) -> Report:
    """`run` 을 주면 그것으로, 아니면 `pipeline` 이름으로 고른다 (`rules` | `llm`)."""
    if run is None:
        if pipeline == "llm":
            from app.service import gateway  # 지연 임포트 — 키 없는 환경에서 rules 만 돌리려고

            gw = gateway()

            def run(fx: Fixture) -> dict[str, Any]:
                return run_llm(fx, gw)

        else:
            run = run_rules
    report = Report(pipeline=pipeline)
    if pipeline == "llm":
        from app.config import settings
        from app.prompts import load_prompt

        report.prompt_version = load_prompt("parse_posting", settings.parse_prompt_version).version
        report.model = settings.hcx_model

    for fx in fixtures if fixtures is not None else load_all():
        actual = run(fx)
        grades = {
            "dueGrade": grade_due_date(fx.expected, actual),
            "typeGrade": grade_type(fx.expected, actual),
            "questionGrade": grade_questions(fx.expected, actual),
            "postingGrade": grade_posting(fx.expected, actual),
        }
        report.due_date[grades["dueGrade"]] += 1
        report.type[grades["typeGrade"]] += 1
        report.form_questions[grades["questionGrade"]] += 1
        report.posting[grades["postingGrade"]] += 1
        kw = grade_keywords(fx.expected, actual)
        pf = grade_preferences(fx.expected, actual)
        if kw is not None:
            report.keywords.update(kw)
        if pf is not None:
            report.preferences.update(pf)
        report.rows.append(
            {"id": fx.id, **grades, "keywordGrade": kw, "preferenceGrade": pf, **actual}
        )

    return report
