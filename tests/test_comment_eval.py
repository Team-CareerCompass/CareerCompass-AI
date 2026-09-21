"""코멘트 평가 (#14) — 채점기가 규칙대로 매기는지. 모델은 부르지 않는다."""

from __future__ import annotations

import json
import re
from typing import Any

from app.comment_eval import (
    evaluate_comments,
    grade_expected,
    grounded,
    line_count,
    load_all,
)
from app.draft_eval import forbidden_hits
from app.gateway import Gateway
from app.providers.base import Completion

GROUNDS = ["Spring", "Kotlin", "RDB 1년 이상", "중고거래 플랫폼 백엔드"]


# --------------------------------------------------------------------------
# 픽스처
# --------------------------------------------------------------------------


def test_fixtures_load_and_cover_the_shapes_we_measure() -> None:
    cases = load_all()
    assert len(cases) >= 10
    assert [c.id for c in cases] == sorted(c.id for c in cases)
    assert all(c.note for c in cases)
    assert any(not c.grounds for c in cases), "근거가 전부 빈 것 — null 을 내야 한다 (계약 §2.2)"
    assert any(
        not c.request.matched_keywords and c.request.missing_qualifications for c in cases
    ), "약점만 있는 것 — strength 를 지어내는지"
    assert any(
        c.request.matched_keywords and not c.request.missing_qualifications for c in cases
    ), "강점만 있는 것 — weakness 를 지어내는지"
    assert any("무시하고" in " ".join(c.grounds) for c in cases), "주입 (#29)"
    assert any(c.expect.get("noCall") for c in cases), "모델을 안 부르는 경로"


def test_fixtures_are_synthetic_except_the_planted_pii() -> None:
    """실물 개인정보는 없다. 010 의 전화번호는 **일부러 심은 것** — 출력 스크럽이 잡아야 한다."""
    phone = re.compile(r"01[016-9][-.]?\d{3,4}[-.]?\d{4}")
    planted = [c.id for c in load_all() if phone.search(json.dumps(c.request.model_dump()))]
    assert planted == ["010"], planted
    assert "010-1234-5678" in load_all()[9].forbidden  # 잡혔는지 채점되는지


# --------------------------------------------------------------------------
# 지표
# --------------------------------------------------------------------------


def test_line_count_ignores_blank_lines() -> None:
    assert line_count("한 줄입니다.") == 1
    assert line_count("첫 줄입니다.\n\n둘째 줄입니다.") == 2
    assert line_count(None) == 0


def test_grounded_is_per_sentence_not_per_item() -> None:
    """가드는 항목 단위라 두 문장 중 하나만 근거를 지목해도 통과한다 — 여기서 그 틈을 드러낸다."""
    both = "Spring 경험이 맞습니다. Kotlin 경험도 맞습니다."
    half = "Spring 경험이 맞습니다. 앞으로 크게 성장하실 분입니다."
    assert grounded(both, GROUNDS) is True
    assert grounded(half, GROUNDS) is False
    assert grounded(None, GROUNDS) is None


def test_grade_expected_reports_both_directions() -> None:
    row: dict[str, Any] = {"strength": "있음", "weakness": None, "calls": 100}
    assert grade_expected(row, {"strength": True, "weakness": False}) == []
    assert grade_expected(row, {"weakness": True}) == ["weakness=있어야 하는데 반대"]
    assert grade_expected(row, {"strength": False}) == ["strength=null 이어야 하는데 반대"]
    assert grade_expected(row, {"noCall": True}) == ["모델을 부르지 않아야 하는데 불렀다"]
    assert grade_expected({**row, "calls": 0}, {"noCall": True}) == []


# --------------------------------------------------------------------------
# 러너 — 가짜 프로바이더. 모델 호출 0.
# --------------------------------------------------------------------------


class ScriptedProvider:
    """근거를 그대로 지목하는 모범 답안을 낸다 — 채점기가 통과를 통과로 매기는지 본다."""

    name = "fake"
    model = "fake-1"
    price_in_krw = 0.001
    price_out_krw = 0.002

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self, system: str, user: str, *, max_tokens: int, temperature: float
    ) -> Completion:
        self.calls += 1
        kw = re.findall(r'"([^"]+)"', user.split("matchedKeywords:")[1].split("\n")[0])
        miss = re.findall(r'"([^"]+)"', user.split("missingQualifications:")[1].split("\n")[0])
        out = {
            "strength": f"{kw[0]} 경험이 이 공고와 맞습니다." if kw else None,
            "weakness": f"{miss[0]}을(를) 확인할 경험이 프로필에 없습니다." if miss else None,
        }
        return Completion(
            text=json.dumps(out, ensure_ascii=False), prompt_tokens=80, completion_tokens=25
        )


def test_evaluate_comments_runs_end_to_end_without_a_model() -> None:
    provider = ScriptedProvider()
    report = evaluate_comments(load_all(), gateway=Gateway(provider))

    ok, total = report.expect_ok
    assert total >= 8
    assert report.forbidden_total == 0
    assert report.unverified_total == 0
    assert report.pii_total == 0
    assert report.format_violations == 0
    assert report.grounded_rate == 1.0
    assert ok == total, [r["expectMisses"] for r in report.rows if r["expectMisses"]]
    # 근거가 전부 빈 항목은 모델을 부르지 않는다
    assert provider.calls == len([c for c in load_all() if c.grounds])
    d = report.as_dict()
    assert len(d["rows"]) == len(load_all())
    assert d["costKrw"] > 0


def test_gateway_now_drops_the_fabricated_credential_sentence() -> None:
    """09-14·09-21 실측 사례 — 「RDB 1년 이상」 근거에서 「자격증」을 만들었다.

    가드(`unsupported_claims`)가 그 문장을 빼므로 평가셋에는 날조가 **도달하지 않는다.**
    채점기(`forbidden_hits`)는 가드를 뚫고 나온 것을 잡는 마지막 그물이라 따로 테스트한다.
    """

    class Fabricating(ScriptedProvider):
        async def complete(
            self, system: str, user: str, *, max_tokens: int, temperature: float
        ) -> Completion:
            self.calls += 1
            out = {
                "strength": "Spring 경험이 맞습니다.",
                "weakness": "RDB 관련 자격증이 부족합니다.",
            }
            return Completion(
                text=json.dumps(out, ensure_ascii=False), prompt_tokens=80, completion_tokens=25
            )

    case = next(c for c in load_all() if c.id == "001")
    report = evaluate_comments([case], gateway=Gateway(Fabricating()))
    assert report.forbidden_total == 0
    assert report.rows[0]["weakness"] is None  # 문장이 하나뿐이라 항목째 사라진다
    assert report.rows[0]["strength"] == "Spring 경험이 맞습니다."

    # 채점기 자체는 여전히 잡는다 — 가드를 뚫고 나오면 이것이 마지막 그물이다
    assert forbidden_hits("RDB 관련 자격증이 부족합니다.", case.forbidden) == ["자격증"]
