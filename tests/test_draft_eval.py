"""초안 평가 (#16 #18 #19) — 채점기가 규칙대로 매기는지. 모델은 부르지 않는다.

채점기가 틀리면 실측 숫자가 전부 거짓이다. 그래서 채점기부터 테스트한다.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.cache import ReplayCache
from app.draft_eval import (
    HEDGES,
    ending_ratio,
    evaluate_drafts,
    fact_jaccard,
    forbidden_hits,
    length_grade,
    load_all,
    ngram_jaccard,
    sentence_overlap,
    sentences,
)
from app.gateway import Gateway
from app.providers.base import Completion

# --------------------------------------------------------------------------
# 픽스처
# --------------------------------------------------------------------------


def test_fixtures_load_and_have_the_shapes_we_measure() -> None:
    cases = load_all()
    assert len(cases) >= 12
    ids = [c.id for c in cases]
    assert ids == sorted(ids)
    # 측정 대상 모양이 하나씩은 있어야 지표가 「해당 없음」으로 조용히 넘어가지 않는다
    assert any(not c.request.experience_summaries for c in cases), "경험 없는 사용자 (#16)"
    assert any(c.request.max_chars == 0 for c in cases), "제한 없음 = 0"
    assert any(
        any(x.startswith("과거 자소서 발췌:") for x in c.request.experience_summaries)
        for c in cases
    ), "BE 의 pastExcerpt 모양"
    assert all(c.forbidden for c in cases), "금지 문자열이 없으면 한국어 날조를 못 잰다"
    assert all(c.note for c in cases), "왜 넣었는지 없는 픽스처는 나중에 못 지운다"


def test_fixtures_are_synthetic_no_pii() -> None:
    """실물 자소서·연락처가 들어오면 저장소에 개인정보가 박힌다 (`docs/PII.md`)."""
    pii = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|01[016-9][-.]?\d{3,4}[-.]?\d{4}")
    for case in load_all():
        blob = json.dumps(case.request.model_dump(), ensure_ascii=False)
        assert not pii.search(blob), case.id


# --------------------------------------------------------------------------
# 톤 — 어미 규칙
# --------------------------------------------------------------------------

FORMAL = "백엔드를 맡았습니다. 32개의 API 를 구현했습니다. 이 경험으로 기여하겠습니다."
CASUAL = "백엔드를 맡았어요. API 를 32개 구현했죠. 이 경험으로 기여하고 싶어요!"
MIXED = "백엔드를 맡았어요. 32개의 API 를 구현했습니다. 이 경험으로 기여하겠습니다."


def test_sentences_split_on_terminal_punctuation_and_newlines() -> None:
    assert len(sentences(FORMAL)) == 3
    assert len(sentences("첫 문장입니다\n둘째 문장이에요.")) == 2
    assert sentences("   ") == []


def test_ending_ratio_formal_vs_casual() -> None:
    assert ending_ratio(FORMAL, "formal") == 1.0
    assert ending_ratio(FORMAL, "casual") == 0.0
    assert ending_ratio(CASUAL, "casual") == 1.0
    assert ending_ratio(CASUAL, "formal") == 0.0


def test_ending_ratio_catches_the_09_14_symptom_first_sentence_only() -> None:
    """09-14 실측: casual 이 첫 문장만 「~해요」였다. 그건 1/3 이지 합격이 아니다."""
    assert ending_ratio(MIXED, "casual") == 1 / 3
    assert ending_ratio(MIXED, "formal") == 2 / 3


def test_ending_ratio_ignores_trailing_quotes_and_is_none_when_empty() -> None:
    assert ending_ratio("「해냈습니다」.", "formal") == 1.0
    assert ending_ratio("", "formal") is None
    assert ending_ratio(FORMAL, "confident") == 1.0  # confident 도 「~니다」 계열


def test_plain_written_style_is_neither_tone() -> None:
    """「~였다」체는 formal 도 casual 도 아니다 — 자소서에서 그건 톤이 아니라 실수다."""
    assert ending_ratio("백엔드를 맡았다. API 를 구현했다.", "formal") == 0.0
    assert ending_ratio("백엔드를 맡았다. API 를 구현했다.", "casual") == 0.0


def test_hedges_are_listed_for_confident_tone() -> None:
    assert "부족하지만" in HEDGES


# --------------------------------------------------------------------------
# 재생성 — n-gram · 문장 겹침
# --------------------------------------------------------------------------


def test_ngram_jaccard_identity_and_disjoint() -> None:
    assert ngram_jaccard(FORMAL, FORMAL) == 1.0
    assert ngram_jaccard("가나다라마", "바사아자차") == 0.0
    assert ngram_jaccard("", "") == 1.0


def test_ngram_jaccard_ignores_whitespace_differences() -> None:
    assert ngram_jaccard("백엔드를 맡았습니다", "백엔드를맡았습니다") == 1.0


def test_sentence_overlap_counts_copied_sentences_only() -> None:
    regen = "32개의 API 를 구현했습니다. 스터디를 운영했습니다."
    assert sentence_overlap(FORMAL, regen) == 0.5  # 앞 문장만 복사
    assert sentence_overlap(FORMAL, FORMAL) == 1.0
    assert sentence_overlap(FORMAL, "") == 0.0


# --------------------------------------------------------------------------
# 사실 — 톤 간 동일성 · 금지 문자열 · 길이
# --------------------------------------------------------------------------


def test_fact_jaccard_same_facts_in_different_tone_is_one() -> None:
    assert fact_jaccard(FORMAL, CASUAL) == 1.0  # 32, API 둘 다 같다


def test_fact_jaccard_drops_when_a_tone_adds_a_number() -> None:
    added = CASUAL + " 응답 속도를 40% 줄였어요."
    assert fact_jaccard(FORMAL, added) < 1.0


def test_fact_jaccard_no_facts_on_either_side_is_one() -> None:
    assert fact_jaccard("열심히 하겠습니다.", "열심히 할게요.") == 1.0


def test_forbidden_hits_ignore_case_and_spaces() -> None:
    answer = "Redis 를 써 봤고 토익 905 점을 받았습니다."
    assert forbidden_hits(answer, ["redis", "905", "편의점"]) == ["redis", "905"]
    assert forbidden_hits(answer, []) == []


def test_length_grade() -> None:
    assert length_grade(500, 500) == "ok"
    assert length_grade(501, 500) == "over"
    assert length_grade(450, 0) == "ok"
    assert length_grade(120, 0) == "short"
    assert length_grade(900, 0) == "long"


# --------------------------------------------------------------------------
# 러너 — 가짜 프로바이더로 끝까지. 모델 호출 0.
# --------------------------------------------------------------------------


class ScriptedProvider:
    """톤 지시를 읽어 그 어미로 답한다. 재생성이면 문장 순서를 바꿔 「다른 글」 흉내를 낸다."""

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
        casual = "~해요" in user
        s1 = (
            "교내 프로젝트에서 백엔드를 맡았어요."
            if casual
            else "프로젝트에서 백엔드를 맡았습니다."
        )
        s2 = "협업을 배웠어요." if casual else "협업을 배웠습니다."
        answer = f"{s1} {s2}" if self.calls % 2 else f"{s2} {s1}"
        body: dict[str, Any] = {"answer": answer, "usedIndexes": [0]}
        return Completion(
            text=json.dumps(body, ensure_ascii=False), prompt_tokens=100, completion_tokens=30
        )


def test_evaluate_drafts_runs_end_to_end_without_a_model() -> None:
    provider = ScriptedProvider()
    gw = Gateway(provider, ReplayCache())
    # 경험이 없는 항목(003)은 모델을 안 부른다 — 호출 수를 세는 테스트라 경험 있는 것만
    cases = [c for c in load_all() if c.request.experience_summaries][:3]
    report = evaluate_drafts(cases, gateway=gw, regen_gateway=gw)

    assert report.calls == 3 * 3  # formal + casual + 재생성
    assert provider.calls == 9
    assert report.length_compliance == 1.0
    assert report.forbidden_total == 0
    assert report.fallback_rate == 0.0
    assert report.tone_consistency("formal") == 1.0
    assert report.tone_consistency("casual") == 1.0
    assert report.distinguishable == (3, 3)
    assert report.fact_jaccard_mean == 1.0
    # 재생성이 문장 순서만 바꿨다 — 3-gram 은 거의 같고 문장은 전부 겹친다
    assert report.regen_sentence_mean == 1.0
    assert report.regen_ngram_mean is not None and report.regen_ngram_mean > 0.8
    d = report.as_dict()
    assert d["calls"] == 9 and len(d["rows"]) == 3
    assert d["rows"][0]["tones"]["formal"]["answer"]


def test_evaluate_drafts_no_regen_skips_the_third_call() -> None:
    provider = ScriptedProvider()
    cases = [c for c in load_all() if c.request.experience_summaries][:2]
    report = evaluate_drafts(cases, gateway=Gateway(provider), regen=False)
    assert provider.calls == 4
    assert report.regen_ngram_mean is None
