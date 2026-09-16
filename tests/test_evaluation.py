"""평가셋 회귀 방지 (#5).

**여기가 회귀를 잡는 자리다.** 규칙이나 프롬프트를 고쳤을 때 조용히 나빠지는 것을 막는다.
임계값은 지금 실측치보다 낮게 잡되, 날조만은 0 을 요구한다.
"""

from app.evaluation import evaluate
from app.fixtures import load_all

# 현재 실측 100%. 표본이 늘면 떨어질 수 있어 여유를 두되, 크게 무너지면 잡는다.
MIN_DUE_ACCURACY = 0.70
MIN_TYPE_ACCURACY = 0.70


def test_fixtures_exist() -> None:
    """평가셋이 비면 그 뒤 테스트가 전부 조용히 통과한다."""
    assert len(load_all()) >= 30


def test_never_hallucinates_a_deadline() -> None:
    """**이것만은 0 이어야 한다.**

    정답이 「마감일 없음」인데 날짜를 만들어 내면, 사용자가 D-1 알림을 받고
    지원했는데 이미 끝나 있다. 못 읽는 것보다 나쁘다 (#8).
    """
    assert evaluate().hallucinated == 0


def test_due_date_accuracy_does_not_regress() -> None:
    report = evaluate()
    assert report.due_accuracy is not None
    assert report.due_accuracy >= MIN_DUE_ACCURACY, (
        f"마감일 정확도 {report.due_accuracy:.0%} — 규칙을 고치기 전에 왜 떨어졌는지 본다"
    )


def test_type_accuracy_does_not_regress() -> None:
    report = evaluate()
    assert report.type_accuracy is not None
    assert report.type_accuracy >= MIN_TYPE_ACCURACY


def test_contacts_are_masked_before_leaving() -> None:
    """공고 파싱은 프로바이더 제약이 없는 유일한 기능이다 — 연락처가 같이 나가면 안 된다."""
    for row in evaluate().rows:
        assert row["maskedContacts"] >= 0
    assert any(row["maskedContacts"] > 0 for row in evaluate().rows), (
        "픽스처에 연락처가 든 공고가 있어야 마스킹이 도는지 확인된다"
    )


# --------------------------------------------------------------------------
# 키워드·우대 채점 (#7) — 순서 없는 목록의 겹침
# --------------------------------------------------------------------------


def test_fixtures_have_keyword_labels() -> None:
    """정답 라벨이 없으면 키워드 정확도가 「측정 안 됨」으로 조용히 넘어간다."""
    labeled = [fx for fx in load_all() if "keywords" in fx.expected]
    assert len(labeled) >= 25


def test_grade_list_counts_overlap_not_exact_match() -> None:
    from app.evaluation import grade_list

    g = grade_list(
        gold=["SW 개발", "두산에너빌리티", "원자력"],
        predicted=["SW개발", "두산에너빌리티 플랜트", "채용", "원자력/SMR 설계"],
        forbidden=["채용", "신입"],
    )
    assert g == {"gold": 3, "hit": 3, "pred": 4, "predHit": 3, "forbidden": 1}


def test_grade_list_short_tokens_need_exact_match() -> None:
    """「AI」가 「AI 반도체」에 포함된다고 맞힌 것으로 치면 두 글자 토큰은 다 맞는다."""
    from app.evaluation import grade_list

    assert grade_list(["X"], ["X, 블로그"], [])["hit"] == 0
    assert grade_list(["AI"], ["AI 반도체"], [])["hit"] == 1  # 두 글자부터는 포함 허용


def test_rules_pipeline_extracts_no_keywords() -> None:
    """규칙은 키워드를 못 낸다 — LLM 비교의 0 기준선이다.

    규칙이 키워드를 내기 시작하면 비교표를 다시 그린다.
    """
    report = evaluate()
    assert report.keyword_recall == 0.0
    assert report.keyword_forbidden == 0


# --------------------------------------------------------------------------
# BE 모양 — Jsoup body().text() 처럼 줄바꿈이 없는 입력에서도 무너지지 않는가
# --------------------------------------------------------------------------


def test_flat_shape_due_date_does_not_regress() -> None:
    """실서버 입력은 한 줄이다. 픽스처(줄 있음)만 재면 실서버에서만 깨지는 것을 놓친다."""
    report = evaluate(shape="flat")
    assert report.hallucinated == 0
    assert report.due_accuracy is not None and report.due_accuracy >= 0.9
    assert report.posting_accuracy is not None and report.posting_accuracy >= 0.9


def test_flat_shape_is_resegmented() -> None:
    from app.evaluation import flatten
    from app.preprocess import preprocess

    for fx in load_all():
        if len(fx.body) < 600:
            continue
        pre = preprocess(flatten(fx).body)
        assert pre.resegmented, fx.id
        # 사람인처럼 라벨에 콜론이 없는 페이지는 줄이 적게 선다 — 그래도 0 은 아니어야 한다
        assert pre.text.count("\n") >= 1, fx.id
