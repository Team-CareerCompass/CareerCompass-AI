"""분야 유사도 비교 — BE 부분일치 이식본과 순위 지표."""

import json
from pathlib import Path

import pytest

from app.fixtures import load_all
from app.similarity_eval import (
    auc,
    be_field_similarity,
    bootstrap_macro_auc_diff,
    interest_texts,
    macro_auc,
    r_precision,
)

PROFILES = Path(__file__).resolve().parent.parent / "fixtures" / "similarity" / "profiles.json"
LABELS = {"backend": "백엔드 개발", "public": "공공·행정"}


def test_interest_texts_follow_be_order() -> None:
    """BE `AnalysisService.interestTexts` — 직무마다 코드·라벨, 그다음 태그."""
    texts = interest_texts(["backend", "public"], ["Java"], LABELS)
    assert texts == ["backend", "백엔드 개발", "public", "공공·행정", "Java"]


def test_be_field_similarity_matches_either_direction_case_insensitive() -> None:
    interests = ["backend", "백엔드 개발", "Java", "서버"]
    # 키워드 ⊂ 관심(「java」 ⊂ 「java」), 관심 ⊂ 키워드(「서버」 ⊂ 「서버 개발자」), 대소문자 무시
    score, matched = be_field_similarity(["JAVA", "서버 개발자", "마케팅"], interests)
    assert matched == ["JAVA", "서버 개발자"]
    assert score == 67  # 2/3 → 66.67 → Math.round 67


def test_be_field_similarity_reads_letters_not_meaning() -> None:
    """원본의 한계 그대로 — 「행정학」 관심은 「기간제 행정원」을 못 잡는다."""
    score, matched = be_field_similarity(
        ["기간제 행정원", "세종"], ["public", "공공·행정", "행정학"]
    )
    assert (score, matched) == (0, [])


def test_be_field_similarity_denominator_is_capped_at_five() -> None:
    keywords = ["Java", "a1", "a2", "a3", "a4", "a5", "a6"]
    assert be_field_similarity(keywords, ["java"])[0] == 20  # 1 / min(7, 5)
    assert be_field_similarity(["Java", "Spring"], ["java", "spring"])[0] == 100
    assert be_field_similarity([], ["java"]) == (0, [])
    assert be_field_similarity(["Java"], []) == (0, [])


def test_auc_counts_ties_as_half() -> None:
    assert auc([3, 2, 1], [True, False, False]) == 1.0
    assert auc([1, 2, 3], [True, False, False]) == 0.0
    # 부분일치가 전부 0 이면 동전 던지기
    assert auc([0, 0, 0, 0], [True, True, False, False]) == 0.5
    assert auc([1, 2], [True, True]) is None


def test_r_precision_breaks_ties_by_expectation() -> None:
    assert r_precision([0.9, 0.8, 0.1], [True, False, True]) == 0.5
    # 관련 2 / 4, 전부 동점 → 상위 2 자리에 관련이 평균 1 개 → 0.5
    assert r_precision([0, 0, 0, 0], [True, True, False, False]) == 0.5
    # 1 등이 관련, 나머지 셋 동점 중 관련 하나 → 1 + 1/3
    assert r_precision([5, 0, 0, 0], [True, True, False, False]) == pytest.approx((1 + 1 / 3) / 2)
    assert r_precision([1, 2], [False, False]) is None


def test_bootstrap_interval_contains_a_clear_win() -> None:
    labels = {"a": [True, True, False, False, False, False]}
    base = {"a": [0.0] * 6}
    better = {"a": [0.9, 0.8, 0.1, 0.2, 0.3, 0.1]}
    assert macro_auc(better, labels) - macro_auc(base, labels) == 0.5
    lo, hi = bootstrap_macro_auc_diff(base, better, labels, n=300)
    assert 0 < lo <= hi <= 0.5


def test_profiles_point_at_real_postings() -> None:
    """정답 파일이 없는 공고 번호를 가리키면 그 정답은 조용히 0 으로 빠진다."""
    spec = json.loads(PROFILES.read_text(encoding="utf-8"))
    ids = {fx.id for fx in load_all() if fx.expected.get("keywords")} - set(spec["excluded"])
    assert len(ids) == 35
    for profile in spec["profiles"]:
        assert set(profile["relevant"]) <= ids, profile["id"]
        assert set(profile["relevant"].values()) <= {1, 2}, profile["id"]
        assert all(code in spec["jobLabels"] for code in profile["jobCodes"]), profile["id"]
