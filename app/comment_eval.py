"""코멘트 평가 (#14) — 초안(`app/draft_eval.py`)과 같은 틀. 정답이 없으니 어긴 것을 센다.

코멘트의 실패는 초안과 다르다. **날조가 조용하다** — 「RDB 1년 이상」이라는 근거로
「데이터베이스 관련 **자격증**이 부족합니다」라고 쓰면 문장은 그럴듯하고 근거도 반쯤 지목했지만
사용자는 없는 자격증 얘기를 읽는다(09-14 실측). 그래서 재는 것은 넷이다.

| 지표 | 무엇 | 왜 |
| --- | --- | --- |
| 기대 충족 | 근거가 있으면 쓰고, 없으면 `null` | 빈말 방지와 「근거 있는데 침묵」 양쪽 |
| 금지 문자열 | 근거에 없는 한국어 명사 (`forbidden`) | 문자열 대조가 못 잡는 날조 |
| 미검증 토큰 | 수치·영문이 근거에 있나 (`guard.fact_check`) | 잡을 수 있는 것은 확실히 |
| 형식 | 1~3줄·항목당 2문장·PII 없음 | 계약 §2.2 |

**LLM 으로 LLM 을 채점하지 않는다.** 전부 문자열 규칙이다.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any

from app.draft_eval import forbidden_hits
from app.gateway import _cites
from app.guard import _PII_OUT, fact_check, sentences
from app.schemas import CommentsRequest

COMMENT_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "comments"

MAX_LINES = 3
"""계약 §2.2 — 명세서 F3-3 의 상한."""
MAX_SENTENCES = 2
"""프롬프트가 항목마다 요구하는 것."""


@dataclass
class CommentCase:
    id: str
    note: str
    request: CommentsRequest
    forbidden: list[str]
    expect: dict[str, Any]
    path: Path

    @property
    def grounds(self) -> list[str]:
        """모델이 인용해도 되는 것 전부 — 이 밖의 사실은 전부 날조다."""
        r = self.request
        out = [*r.matched_keywords, *r.matched_preferences, *r.missing_qualifications]
        if r.top_experience_title:
            out.append(r.top_experience_title)
        return out


def load(path: Path) -> CommentCase:
    data = json.loads(path.read_text(encoding="utf-8"))
    return CommentCase(
        id=str(data.get("id", path.stem)),
        note=str(data.get("note", "")),
        request=CommentsRequest.model_validate(data["request"]),
        forbidden=list(data.get("forbidden", [])),
        expect=dict(data.get("expect", {})),
        path=path,
    )


def load_all(root: Path = COMMENT_ROOT) -> list[CommentCase]:
    return [load(p) for p in sorted(root.glob("*.json"))]


# --------------------------------------------------------------------------
# 규칙 지표
# --------------------------------------------------------------------------


def line_count(text: str | None) -> int:
    return len([ln for ln in (text or "").splitlines() if ln.strip()])


def grounded(text: str | None, grounds: list[str]) -> bool | None:
    """문장 **전부**가 근거를 하나 이상 지목하는가. 게이트웨이 가드가 통과시킨 것을 다시 본다.

    가드는 항목 단위로 본다(`_keep_if_cites`) — 두 문장 중 하나만 근거를 지목해도 항목이 산다.
    여기서는 문장 단위로 다시 재서 그 틈을 드러낸다.
    """
    if not text:
        return None
    return all(any(_cites(s, g) for g in grounds if g) for s in sentences(text))


def grade_expected(row: dict[str, Any], expect: dict[str, Any]) -> list[str]:
    """기대와 다른 것의 목록. 비어 있으면 통과."""
    misses = []
    for key in ("strength", "weakness"):
        if key not in expect:
            continue
        want, got = bool(expect[key]), bool(row[key])
        if want != got:
            misses.append(f"{key}={'있어야' if want else 'null 이어야'} 하는데 반대")
    if expect.get("noCall") and row["calls"]:
        misses.append("모델을 부르지 않아야 하는데 불렀다")
    return misses


def run_case(case: CommentCase, gateway: Any) -> dict[str, Any]:
    res = asyncio.run(gateway.comments(case.request))
    text = "\n".join(x for x in (res.strength, res.weakness) if x)
    grounds = case.grounds
    row: dict[str, Any] = {
        "id": case.id,
        "note": case.note,
        "strength": res.strength,
        "weakness": res.weakness,
        "lines": line_count(res.strength) + line_count(res.weakness),
        "sentencesOver": [
            key
            for key, val in (("strength", res.strength), ("weakness", res.weakness))
            if val and len(sentences(val)) > MAX_SENTENCES
        ],
        "forbiddenHits": forbidden_hits(text, case.forbidden),
        "unverified": fact_check(text, grounds) if text else [],
        "grounded": grounded(text, grounds),
        "pii": bool(_PII_OUT.search(text)),
        "calls": res.usage.total_tokens if res.usage else 0,
        "usage": res.usage.model_dump(by_alias=True) if res.usage else None,
    }
    row["expectMisses"] = grade_expected(row, case.expect)
    return row


@dataclass
class CommentReport:
    prompt_version: str | None = None
    model: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def expect_ok(self) -> tuple[int, int]:
        graded = [r for r in self.rows if r["expectMisses"] is not None]
        return sum(not r["expectMisses"] for r in graded), len(graded)

    @property
    def forbidden_total(self) -> int:
        return sum(len(r["forbiddenHits"]) for r in self.rows)

    @property
    def unverified_total(self) -> int:
        return sum(len(r["unverified"]) for r in self.rows)

    @property
    def grounded_rate(self) -> float | None:
        vals = [r["grounded"] for r in self.rows if r["grounded"] is not None]
        return mean(vals) if vals else None

    @property
    def format_violations(self) -> int:
        return sum(r["lines"] > MAX_LINES or bool(r["sentencesOver"]) for r in self.rows)

    @property
    def pii_total(self) -> int:
        return sum(r["pii"] for r in self.rows)

    @property
    def silent_rate(self) -> float | None:
        """근거가 있는데 둘 다 `null` 인 비율. 가드가 너무 세면 코멘트가 사라진다 (#44)."""
        rows = [r for r in self.rows if r["usage"] and r["calls"]]
        return mean(not (r["strength"] or r["weakness"]) for r in rows) if rows else None

    @property
    def cost_krw(self) -> float:
        return round(sum(r["usage"]["costKrw"] for r in self.rows if r["usage"]), 4)

    @property
    def total_tokens(self) -> int:
        return sum(r["usage"]["totalTokens"] for r in self.rows if r["usage"])

    def as_dict(self) -> dict[str, Any]:
        ok, total = self.expect_ok
        return {
            "promptVersion": self.prompt_version,
            "model": self.model,
            "expectOk": ok,
            "expectOf": total,
            "forbiddenTotal": self.forbidden_total,
            "unverifiedTotal": self.unverified_total,
            "groundedRate": self.grounded_rate,
            "formatViolations": self.format_violations,
            "piiTotal": self.pii_total,
            "silentRate": self.silent_rate,
            "totalTokens": self.total_tokens,
            "costKrw": self.cost_krw,
            "rows": self.rows,
        }


def evaluate_comments(
    cases: list[CommentCase] | None = None, *, gateway: Any = None
) -> CommentReport:
    if gateway is None:
        from app.service import gateway as service_gateway

        gateway = service_gateway("comments")

    from app.config import settings
    from app.prompts import load_prompt

    report = CommentReport(
        prompt_version=load_prompt("comments", settings.comments_prompt_version).version,
        model=getattr(gateway.provider, "model", None) or settings.hcx_model,
    )
    for case in cases if cases is not None else load_all():
        report.rows.append(run_case(case, gateway))
    return report
