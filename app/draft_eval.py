"""초안 평가 (#16 #18 #19) — 정답이 없는 기능의 회귀를 숫자로 잡는다.

파싱(`app/evaluation.py`)은 정답 라벨과 대조하면 되지만 초안은 정답이 없다. 대신 **어겨서는
안 되는 것**이 있고 그것은 전부 규칙으로 잴 수 있다:

| 지표 | 무엇 | 이슈 |
| --- | --- | --- |
| 글자 수 준수 | `charCount ≤ maxChars` (0 이면 400~600) | #16 |
| 금지 문자열 | 발췌에만 있는 사실·경험에 없는 키워드·부풀린 수치가 답에 나왔나 | #16 #29 |
| 톤 일관성 | 문장 어미 — formal 「~니다」 casual 「~요」 비율 | #18 |
| 톤 간 사실 동일 | 두 톤 답의 수치·영문 토큰 집합이 같은가 | #18 |
| 재생성 겹침 | 같은 요청 두 답의 문자 3-gram Jaccard·문장 겹침 | #19 |

전부 문자열 규칙이다. **LLM 으로 LLM 을 채점하지 않는다** — 채점기가 흔들리면 회귀를 못 잡는다.
픽스처는 `fixtures/drafts/*.json`, 전부 합성(실물 자소서 없음).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any

from app.guard import _LATIN, _NUMBER, _TRAILING, _normalize, ending_ratio, sentences
from app.schemas import DraftRequest

DRAFT_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "drafts"

DEFAULT_RANGE = (400, 600)
"""`maxChars: 0` 일 때 프롬프트가 요구하는 길이. 계약 위반은 아니라 등급만 다르게 매긴다."""

TONE_TARGET = 0.8
"""톤 일관성 합격선. 09-14 실측의 casual 은 첫 문장만 「~해요」였다 — 그건 0.2 쯤이다."""


@dataclass
class DraftCase:
    id: str
    note: str
    request: DraftRequest
    forbidden: list[str]
    path: Path


def load(path: Path) -> DraftCase:
    data = json.loads(path.read_text(encoding="utf-8"))
    return DraftCase(
        id=str(data.get("id", path.stem)),
        note=str(data.get("note", "")),
        request=DraftRequest.model_validate(data["request"]),
        forbidden=list(data.get("forbidden", [])),
        path=path,
    )


def load_all(root: Path = DRAFT_ROOT) -> list[DraftCase]:
    return [load(p) for p in sorted(root.glob("*.json"))]


# --------------------------------------------------------------------------
# 규칙 지표 — 전부 순수 함수. 테스트는 `tests/test_draft_eval.py`.
# --------------------------------------------------------------------------

__all__ = ["ending_ratio", "sentences"]  # guard 에 있다 — 채점기와 게이트웨이가 같은 규칙을 쓴다

FILL_TARGET = 2 / 3
"""상한이 있을 때 이만큼은 채워야 「답」이다 (`gateway.FILL_MIN` 과 같다).
v1 실측: 500자 항목에 355자, 300자 항목에 100자."""

REGEN_SIMILAR = 0.5
"""재생성 3-gram Jaccard 가 이 이상이면 「거의 같은 글」로 센다. 다른 항목끼리는 0.03 이다."""

HEDGES = ("부족하지만", "부족하나", "감히", "미숙하지만", "미흡하지만", "나름대로", "조금이나마")
"""confident 톤이 쓰지 말아야 할 완곡 표현 (`gateway.TONE_RULES`)."""

CLICHES = ("라고 생각합니다", "하였습니다", "생각됩니다", "것 같습니다")
"""#18 「상투」 — 격식이 아니라 남용이다. 세기만 한다."""


def count_phrases(text: str, phrases: tuple[str, ...]) -> int:
    compact = text.replace(" ", "")
    return sum(compact.count(p.replace(" ", "")) for p in phrases)


def ngrams(text: str, n: int = 3) -> set[str]:
    """공백을 뺀 문자 n-gram. 한국어는 띄어쓰기가 흔들려 단어 단위보다 문자 단위가 안정적이다."""
    compact = "".join(text.split())
    return {compact[i : i + n] for i in range(max(0, len(compact) - n + 1))}


def ngram_jaccard(a: str, b: str, n: int = 3) -> float:
    """두 글의 문자 n-gram Jaccard. 1.0 이면 같은 글, 0.0 이면 겹치는 세 글자가 하나도 없다.

    같은 경험 요약을 인용하면 어느 정도는 겹친다 — 그래서 절대값보다 **재생성 vs 다른 항목**
    비교가 뜻이 있다. 다른 항목끼리의 값이 바닥선이다.
    """
    x, y = ngrams(a, n), ngrams(b, n)
    if not x and not y:
        return 1.0
    return len(x & y) / len(x | y)


def sentence_overlap(a: str, b: str) -> float:
    """b 의 문장 중 a 에 그대로(공백·문장부호 무시) 있는 비율. 재생성이 문장을 복사했는지."""
    pool = {_normalize(_TRAILING.sub("", s)) for s in sentences(a)}
    sents = [_normalize(_TRAILING.sub("", s)) for s in sentences(b)]
    if not sents:
        return 0.0
    return sum(s in pool for s in sents) / len(sents)


def fact_tokens(text: str) -> set[str]:
    """수치·영문 토큰 — `guard.fact_check` 가 대조하는 것과 같은 것. 한 자리 수는 서술어라 뺀다."""
    out: set[str] = set()
    for m in _NUMBER.finditer(text):
        token = m.group(0).strip()
        if len(re.sub(r"\D", "", token)) >= 2:
            out.add(_normalize(token))
    for m in _LATIN.finditer(text):
        out.add(_normalize(m.group(0)))
    return out


def fact_jaccard(a: str, b: str) -> float:
    """두 답이 인용한 사실 토큰의 겹침. 둘 다 사실 토큰이 없으면 1.0 — 「없는 사실이 같다」."""
    x, y = fact_tokens(a), fact_tokens(b)
    if not x and not y:
        return 1.0
    return len(x & y) / len(x | y)


def forbidden_hits(answer: str, forbidden: list[str]) -> list[str]:
    compact = _normalize(answer)
    return [f for f in forbidden if f and _normalize(f) in compact]


def length_grade(char_count: int, limit: int) -> str:
    """`ok` · `over`(상한 초과 — 계약 위반) · `under`(상한의 70% 미만 — 위반은 아니지만 답이 아니다)
    · `short`/`long`(제한 없을 때 400~600 밖).
    """
    if limit > 0:
        if char_count > limit:
            return "over"
        return "under" if char_count < limit * FILL_TARGET else "ok"
    lo, hi = DEFAULT_RANGE
    if char_count < lo:
        return "short"
    if char_count > hi:
        return "long"
    return "ok"


# --------------------------------------------------------------------------
# 러너
# --------------------------------------------------------------------------


def run_case(case: DraftCase, gateway: Any, tone: str) -> dict[str, Any]:
    """게이트웨이를 한 번 불러 규칙 지표를 매긴 행. `answer` 도 남긴다 — 사람이 읽어야 한다."""
    req = case.request.model_copy(update={"tone": tone})
    res = asyncio.run(gateway.draft_answer(req))
    fc = res.fact_check
    return {
        "tone": tone,
        "answer": res.answer,
        "charCount": res.char_count,
        "limit": req.max_chars,
        "lengthGrade": length_grade(res.char_count, req.max_chars),
        "fillRatio": round(res.char_count / req.max_chars, 2) if req.max_chars else None,
        "usedIndexes": res.used_indexes,
        "unverified": fc.unverified if fc else [],
        "fallback": bool(fc and fc.fallback),
        "forbiddenHits": forbidden_hits(res.answer, case.forbidden),
        "endingRatio": ending_ratio(res.answer, tone),
        "hedges": count_phrases(res.answer, HEDGES),
        "cliches": count_phrases(res.answer, CLICHES),
        "usage": res.usage.model_dump(by_alias=True) if res.usage else None,
    }


@dataclass
class DraftReport:
    prompt_version: str | None = None
    model: str | None = None
    tones: tuple[str, ...] = ("formal", "casual")
    regen: bool = True
    rows: list[dict[str, Any]] = field(default_factory=list)

    # ---- 집계 ------------------------------------------------------------

    def _tone_rows(self, tone: str | None = None) -> list[dict[str, Any]]:
        """톤을 주면 그 톤의 행만 — 재생성 행도 자기 톤으로 걸러진다 (첫 실측의 집계 버그)."""
        out = []
        for row in self.rows:
            for t, r in row["tones"].items():
                if tone is None or t == tone:
                    out.append(r)
            regen = row.get("regen")
            if regen and (tone is None or regen["tone"] == tone):
                out.append(regen)
        return out

    @property
    def calls(self) -> int:
        return len(self._tone_rows())

    @property
    def length_compliance(self) -> float | None:
        """상한이 있는 답 중 넘지 않은 비율. `over` 가 하나라도 있으면 게이트웨이 버그다."""
        rows = [r for r in self._tone_rows() if r["limit"] > 0]
        return mean(r["lengthGrade"] != "over" for r in rows) if rows else None

    @property
    def fill_compliance(self) -> float | None:
        """상한이 있는 답 중 70% 이상 채운 비율. 500자 항목에 100자를 내면 사용자는 다시 누른다."""
        rows = [r for r in self._tone_rows() if r["limit"] > 0]
        return mean(r["lengthGrade"] == "ok" for r in rows) if rows else None

    @property
    def fill_ratio_mean(self) -> float | None:
        vals = [r["fillRatio"] for r in self._tone_rows() if r.get("fillRatio") is not None]
        return mean(vals) if vals else None

    @property
    def default_range_compliance(self) -> float | None:
        rows = [r for r in self._tone_rows() if r["limit"] == 0]
        return mean(r["lengthGrade"] == "ok" for r in rows) if rows else None

    @property
    def fallback_rate(self) -> float | None:
        rows = self._tone_rows()
        return mean(r["fallback"] for r in rows) if rows else None

    @property
    def forbidden_total(self) -> int:
        return sum(len(r["forbiddenHits"]) for r in self._tone_rows())

    @property
    def unverified_total(self) -> int:
        return sum(len(r["unverified"]) for r in self._tone_rows())

    def tone_consistency(self, tone: str) -> float | None:
        vals = [r["endingRatio"] for r in self._tone_rows(tone) if r["endingRatio"] is not None]
        return mean(vals) if vals else None

    @property
    def distinguishable(self) -> tuple[int, int]:
        """(둘 다 어미 일관성 ≥ 80% 인 건수, 두 톤을 다 돌린 건수)."""
        pairs = [row for row in self.rows if len(row["tones"]) >= 2]
        ok = sum(bool(row["distinguishable"]) for row in pairs)
        return ok, len(pairs)

    @property
    def fact_jaccard_mean(self) -> float | None:
        vals = [row["factJaccard"] for row in self.rows if row.get("factJaccard") is not None]
        return mean(vals) if vals else None

    def _regen_rows(self) -> list[dict[str, Any]]:
        """안전 초안(fallback)은 규칙이 만든 결정적 문장이라 재생성 비교에서 뺀다."""
        return [
            row["regen"]
            for row in self.rows
            if row.get("regen")
            and not row["regen"]["fallback"]
            and not row["tones"][self.tones[0]]["fallback"]
        ]

    @property
    def regen_ngram_mean(self) -> float | None:
        vals = [r["ngramJaccard"] for r in self._regen_rows()]
        return mean(vals) if vals else None

    @property
    def regen_similar(self) -> tuple[int, int]:
        """(3-gram Jaccard ≥ 0.5 인 재생성 수, 재생성 수) — 「다시 써줘」가 같은 글을 준 횟수."""
        vals = [r["ngramJaccard"] for r in self._regen_rows()]
        return sum(v >= REGEN_SIMILAR for v in vals), len(vals)

    @property
    def regen_sentence_mean(self) -> float | None:
        vals = [r["sentenceOverlap"] for r in self._regen_rows()]
        return mean(vals) if vals else None

    @property
    def cross_case_ngram_mean(self) -> float | None:
        """**바닥선** — 서로 다른 항목의 formal 답끼리의 3-gram Jaccard. 재생성 겹침이 이 값에
        가까우면 「다른 글」이고, 1.0 에 가까우면 같은 글이다."""
        answers = [row["tones"][self.tones[0]]["answer"] for row in self.rows if row["tones"]]
        pairs = [
            ngram_jaccard(answers[i], answers[j])
            for i in range(len(answers))
            for j in range(i + 1, len(answers))
        ]
        return mean(pairs) if pairs else None

    @property
    def cost_krw(self) -> float:
        return round(sum(r["usage"]["costKrw"] for r in self._tone_rows() if r.get("usage")), 4)

    @property
    def total_tokens(self) -> int:
        return sum(r["usage"]["totalTokens"] for r in self._tone_rows() if r.get("usage"))

    def as_dict(self) -> dict[str, Any]:
        ok, total = self.distinguishable
        return {
            "promptVersion": self.prompt_version,
            "model": self.model,
            "tones": list(self.tones),
            "regen": self.regen,
            "calls": self.calls,
            "lengthCompliance": self.length_compliance,
            "fillCompliance": self.fill_compliance,
            "fillRatioMean": self.fill_ratio_mean,
            "defaultRangeCompliance": self.default_range_compliance,
            "fallbackRate": self.fallback_rate,
            "forbiddenTotal": self.forbidden_total,
            "unverifiedTotal": self.unverified_total,
            "toneConsistency": {t: self.tone_consistency(t) for t in self.tones},
            "distinguishable": ok,
            "distinguishableOf": total,
            "factJaccardMean": self.fact_jaccard_mean,
            "regenNgramMean": self.regen_ngram_mean,
            "regenSimilar": self.regen_similar[0],
            "regenSentenceMean": self.regen_sentence_mean,
            "crossCaseNgramMean": self.cross_case_ngram_mean,
            "totalTokens": self.total_tokens,
            "costKrw": self.cost_krw,
            "rows": self.rows,
        }


def evaluate_drafts(
    cases: list[DraftCase] | None = None,
    *,
    gateway: Any = None,
    regen_gateway: Any = None,
    tones: tuple[str, ...] = ("formal", "casual"),
    regen: bool = True,
) -> DraftReport:
    """항목마다 톤별로 한 번, 첫 톤으로 한 번 더(재생성). 지표는 행에, 집계는 `DraftReport` 에.

    재생성 호출은 **캐시를 우회한다** — 같은 요청을 두 번 보내 다른 답이 오는지가 측정 대상이라
    리플레이 캐시가 있으면 항상 같은 답이 나와 겹침률 1.0 이 된다.
    """
    if gateway is None:
        from app.service import gateway as service_gateway

        gateway = service_gateway("draft")
    if regen and regen_gateway is None:
        from app.cache import ReplayCache
        from app.gateway import Gateway

        regen_gateway = Gateway(gateway.provider, ReplayCache(), gateway.budget)

    from app.config import settings
    from app.prompts import load_prompt

    report = DraftReport(
        prompt_version=load_prompt("draft_answer", settings.draft_prompt_version).version,
        model=getattr(gateway.provider, "model", None) or settings.hcx_model,
        tones=tones,
        regen=regen,
    )
    for case in cases if cases is not None else load_all():
        tone_rows = {tone: run_case(case, gateway, tone) for tone in tones}
        row: dict[str, Any] = {
            "id": case.id,
            "note": case.note,
            "limit": case.request.max_chars,
            "experiences": len(case.request.experience_summaries),
            "tones": tone_rows,
            "factJaccard": None,
            "distinguishable": None,
            "regen": None,
        }
        if len(tones) >= 2:
            a, b = tone_rows[tones[0]], tone_rows[tones[1]]
            row["factJaccard"] = fact_jaccard(a["answer"], b["answer"])
            row["distinguishable"] = all((r["endingRatio"] or 0.0) >= TONE_TARGET for r in (a, b))
        if regen:
            first = tone_rows[tones[0]]
            again = run_case(case, regen_gateway, tones[0])
            again["ngramJaccard"] = ngram_jaccard(first["answer"], again["answer"])
            again["sentenceOverlap"] = sentence_overlap(first["answer"], again["answer"])
            row["regen"] = again
        report.rows.append(row)
    return report
