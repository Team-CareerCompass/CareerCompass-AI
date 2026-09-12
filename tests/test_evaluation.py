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
    assert len(load_all()) >= 8


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
