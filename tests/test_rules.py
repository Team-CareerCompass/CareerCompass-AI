"""규칙 기반 추출 테스트.

날짜 표현은 **실제 픽스처에서 가져온 것**만 쓴다. 지어낸 예제로 시험하면 실전에서 무너진다.
"""

from datetime import date

import pytest

from app import rules

COLLECTED = date(2026, 9, 7)


# --------------------------------------------------------------------------
# 마감일 — 여섯 가지 실제 표현 (#8)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 001 한글 연월일 범위
        ("다. 접수기한 : 2026년 9월 1일 (화) ~ 2026년 9월 15일(화)", "2026-09-15"),
        # 002 점 구분 범위
        ("6.신청기간 및 방법\n- 기간 : 2026. 9. 15. (화) ~ 2026. 9. 21. (월)", "2026-09-21"),
        # 003 시각 포함
        ("2) 신청기간: 2026. 9. 7.(월) 18:00 ~ 2026. 9. 18.(금) 09:00", "2026-09-18"),
        # 004 두 자리 연도 + 뒤쪽 연도 없음
        ("[모집기간]26.09.01(화) 9시 ~ 09.14(월) 17시까지 (KST)", "2026-09-14"),
        # 006 뒤쪽 연도 없음
        ("▶ 접수기간\n- 2026. 8. 24(월) 12시~ 9. 21(월) 18시까지", "2026-09-21"),
        # 008 연도가 아예 없음
        ("📅 모집기간: 8/24(월) ~ 9/11(금)", "2026-09-11"),
    ],
)
def test_due_date_from_real_expressions(text: str, expected: str) -> None:
    """**범위 표현에서는 뒤쪽이 마감일이다.** 앞 날짜를 집으면 틀린다."""
    assert rules.extract_due_date(text, COLLECTED).iso == expected


def test_due_date_ignores_result_announcement() -> None:
    """002 의 함정 — 마감일 바로 다음 줄에 결과 발표일이 있다."""
    text = (
        "6.신청기간 및 방법\n"
        "- 기간 : 2026. 9. 15. (화) ~ 2026. 9. 21. (월)\n"
        "7.결과 발표일 : 2026. 9. 30 (수)"
    )
    assert rules.extract_due_date(text, COLLECTED).iso == "2026-09-21"


def test_due_date_ignores_schedule_table() -> None:
    """006 의 함정 — 아래 운영일정에 10.6·11.9 가 있다."""
    text = (
        "▶ 접수기간\n- 2026. 8. 24(월) 12시~ 9. 21(월) 18시까지\n\n"
        "▶ 운영일정\n예선 결과 발표\n10.6(화)\n대회 본선\n11.9(월)"
    )
    assert rules.extract_due_date(text, COLLECTED).iso == "2026-09-21"


def test_due_date_ignores_support_period() -> None:
    """003 의 함정 — 「지원기간: 선발시부터 6개학기」는 장학금 지급 기간이다."""
    text = (
        "5) 지원기간: 선발시부터 6개학기만 지원\n"
        "2) 신청기간: 2026. 9. 7.(월) 18:00 ~ 2026. 9. 18.(금) 09:00"
    )
    assert rules.extract_due_date(text, COLLECTED).iso == "2026-09-18"


def test_year_inference_is_flagged() -> None:
    """연도를 추정했으면 그 사실을 남긴다. 신뢰도가 낮다는 신호다."""
    inferred = rules.extract_due_date("모집기간: 8/24(월) ~ 9/11(금)", COLLECTED)
    assert inferred.iso == "2026-09-11"
    assert inferred.year_inferred is True

    explicit = rules.extract_due_date("접수기한 : 2026년 9월 15일", COLLECTED)
    assert explicit.year_inferred is False


def test_time_is_detected() -> None:
    due = rules.extract_due_date("신청기간: 2026. 9. 7. 18:00 ~ 2026. 9. 18. 09:00", COLLECTED)
    assert due.has_time is True


def test_no_deadline_differs_from_not_found() -> None:
    """「상시 모집」과 「마감일이 있는데 못 읽음」은 다르다 (#8)."""
    always = rules.extract_due_date("상시 모집합니다. 관심 있는 분은 연락 주세요.", COLLECTED)
    assert always.iso is None
    assert always.reason == "no_deadline"

    unknown = rules.extract_due_date("자세한 내용은 붙임 파일을 참조하시기 바랍니다.", COLLECTED)
    assert unknown.iso is None
    assert unknown.reason == "not_found"


def test_impossible_dates_are_rejected() -> None:
    """URL 의 숫자가 날짜로 잡히면 안 된다 — event-us.kr/m/132963/59419."""
    text = "접수기간\n참가 신청 (링크 : https://event-us.kr/m/132963/59419)"
    assert rules.extract_due_date(text, COLLECTED).iso is None


# --------------------------------------------------------------------------
# 지원서 양식 (#10)
# --------------------------------------------------------------------------


def test_form_questions_with_char_limits() -> None:
    text = (
        "2. 자기소개서\n"
        "   - 지원 동기를 작성해 주세요. (500자 이내)\n"
        "   - 본인의 강점과 약점을 사례를 들어 서술해 주세요. (400자 이내)\n"
        "   - 입사 후 이루고 싶은 목표를 작성해 주세요. (300자 이내)\n"
    )
    questions = rules.extract_form_questions(text)

    assert [q.max_chars for q in questions] == [500, 400, 300]
    assert questions[0].order == 1


@pytest.mark.parametrize(
    "line",
    [
        "- 궁금한 사항을 모두 정리하여 하나의 메일로 문의해 주세요",
        "  3. 작성한 서류를 구글폼에 업로드하여 제출",
        "- 자세한 내용은 홈페이지를 참고하시기 바랍니다",
    ],
)
def test_form_questions_reject_guidance_text(line: str) -> None:
    """문의처·제출 방법 안내문이 흔한 오탐이다 (#10). 실제 픽스처에서 나왔다."""
    assert rules.extract_form_questions(line) == []


def test_no_form_questions_is_normal() -> None:
    """서류 없이 지원하는 장학금이 정상적으로 존재한다. 「없음」과 「못 찾음」은 다르다."""
    assert rules.extract_form_questions("가. 접수 방법: 등기우편 (마감일 소인분 유효)") == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [("500자 이내", 500), ("띄어쓰기 포함 800자", 800), ("1000자 내외", 1000), ("제한 없음", None)],
)
def test_max_chars(text: str, expected: int | None) -> None:
    assert rules.extract_max_chars(text) == expected


# --------------------------------------------------------------------------
# 자격·우대·유형
# --------------------------------------------------------------------------


def test_qualifications() -> None:
    text = "■ 자격 요건\n- 4년제 대학 재학생 (2학년 이상)\n- 직전 학기 학점 3.7/4.5 이상"
    quals = rules.extract_qualifications(text)

    assert quals.year == "2학년 이상"
    assert quals.gpa is not None and "3.7" in quals.gpa


def test_preferences_under_heading() -> None:
    text = (
        "■ 우대 사항\n"
        "- Java / Kotlin 기반 서버 개발 경험\n"
        "- RDB 설계 및 운영 경험 1년 이상\n"
        "\n"
        "■ 근무 조건\n- 인턴 6개월"
    )
    assert rules.extract_preferences(text) == [
        "Java / Kotlin 기반 서버 개발 경험",
        "RDB 설계 및 운영 경험 1년 이상",
    ]


def test_preferences_empty_without_heading() -> None:
    assert rules.extract_preferences("■ 자격 요건\n- 2학년 이상") == []


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("2026학년도 용복장학회 장학생 모집 안내", "scholarship"),
        ("[현대자동차] 2026 하반기 9월 신입 채용", "recruit"),
        ("2026 K-조선 해커톤", "contest"),
        ("[서포터즈] LG 마이컵 대학생 서포터즈 1기 모집", "activity"),
    ],
)
def test_guess_type(title: str, expected: str) -> None:
    assert rules.guess_type(title, "") == expected


def test_guess_type_returns_none_without_signal() -> None:
    assert rules.guess_type("2026-1학기 수강신청 안내", "수강신청 일정을 안내합니다") is None


# --------------------------------------------------------------------------
# 공고인가 (NOT_A_POSTING 1차 판정)
# --------------------------------------------------------------------------


def test_academic_notice_is_not_a_posting() -> None:
    """학사공지를 통째로 등록하면 함께 수집된다 (F2-1). 목록에 넣으면 안 된다."""
    text = "2026-1학기 수강신청 안내\n수강신청 일정은 학사일정을 확인하시기 바랍니다."
    due = rules.extract_due_date(text)

    assert rules.posting_signals(text, due).likely_posting is False


def test_real_posting_passes() -> None:
    text = "■ 모집 대상\n- 4년제 대학 재학생 (2학년 이상)\n■ 접수기한\n- 2026년 9월 15일까지"
    due = rules.extract_due_date(text, COLLECTED)

    assert rules.posting_signals(text, due).likely_posting is True
