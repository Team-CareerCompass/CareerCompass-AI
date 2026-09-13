"""평가셋 러너 (#5) — 픽스처에 파이프라인을 돌리고 지표를 낸다.

프롬프트나 규칙을 고쳤을 때 **좋아졌는지 나빠졌는지를 숫자로** 답하기 위한 것이다.
눈으로 두세 개 보고 넘어가면 다른 유형에서 조용히 나빠진다.

지금은 규칙 전용 파이프라인만 돈다. LLM 이 붙으면 `run` 을 갈아 끼워
**규칙 / LLM / 하이브리드**를 같은 픽스처로 비교한다 — BE 의 `HeuristicLlmGateway` 가
그대로 기준선이라, 「규칙 대비 LLM 이 얼마나 나은가」가 정량적으로 나온다.
"""

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app import rules
from app.fixtures import Fixture, load_all
from app.preprocess import preprocess


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
        "likelyPosting": signals.likely_posting,
        "truncated": pre.truncated,
        "maskedContacts": len(pre.masked),
        "imageOnly": fx.image_only,
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
    due_date: Counter[str] = field(default_factory=Counter)
    type: Counter[str] = field(default_factory=Counter)
    form_questions: Counter[str] = field(default_factory=Counter)
    posting: Counter[str] = field(default_factory=Counter)
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

    @property
    def hallucinated(self) -> int:
        """날조한 마감일의 수. **이것은 0 이어야 한다.**"""
        return self.due_date["hallucinated"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "pipeline": self.pipeline,
            "dueDate": dict(self.due_date),
            "type": dict(self.type),
            "formQuestions": dict(self.form_questions),
            "posting": dict(self.posting),
            "dueAccuracy": self.due_accuracy,
            "typeAccuracy": self.type_accuracy,
            "questionAccuracy": self.question_accuracy,
            "postingAccuracy": self.posting_accuracy,
            "rows": self.rows,
        }


def evaluate(fixtures: list[Fixture] | None = None) -> Report:
    report = Report(pipeline="rules-only")

    for fx in fixtures if fixtures is not None else load_all():
        actual = run_rules(fx)
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
        report.rows.append({"id": fx.id, **grades, **actual})

    return report
