"""전처리 테스트.

보고서 4.4 에는 이 단계가 없다. 파싱 품질과 토큰 비용에 모델 선택보다 크게 영향을 준다.
"""

from app import preprocess as pp


def test_normalize_keeps_line_breaks() -> None:
    """줄바꿈은 문단 경계의 유일한 단서다. 뭉개면 항목을 못 찾는다."""
    out = pp.normalize("첫 줄\r\n\r\n\r\n둘째  줄\t\t끝")

    assert out == "첫 줄\n\n둘째 줄 끝"


def test_normalize_strips_control_characters() -> None:
    assert "\x07" not in pp.normalize("공고\x07본문")


def test_strip_boilerplate_removes_board_chrome() -> None:
    text = "목록\n이전글\n■ 모집 대상\n- 재학생\n다음글\n공유하기"

    assert pp.strip_boilerplate(text) == "■ 모집 대상\n- 재학생"


def test_strip_boilerplate_keeps_body_words() -> None:
    """본문 안에 섞인 같은 단어는 건드리지 않는다."""
    text = "- 제출 서류 목록을 확인하세요"

    assert pp.strip_boilerplate(text) == text


def test_strip_collector_trailer() -> None:
    """링커리어가 본문 끝에 붙이는 자기 홍보 — 008 에서 실제로 나왔다."""
    text = "활동기간: 10주\n\n대학생 대외활동 공모전 채용 사이트 링커리어 https://linkareer.com/"

    assert "링커리어" not in pp.strip_boilerplate(text)


def test_mask_contacts() -> None:
    """공고는 공개 게시물이지만 담당자 연락처는 제3자의 개인정보다.

    공고 파싱은 프로바이더 제약이 없는 유일한 기능이라 해외 모델로 나갈 수 있다.
    """
    text = "문의처 : (재)용복장학회 사무국 02-437-2219 / leeyj@hanmail.net"
    out, found = pp.mask_contacts(text)

    assert "02-437-2219" not in out
    assert "leeyj@hanmail.net" not in out
    assert "[연락처]" in out and "[이메일]" in out
    assert len(found) == 2


def test_mask_contacts_handles_mobile() -> None:
    out, found = pp.mask_contacts("담당자 010-1234-5678")

    assert "010-1234-5678" not in out
    assert len(found) == 1


def test_truncate_marks_and_cuts_at_line() -> None:
    text = "\n".join(f"{i}번째 줄입니다" for i in range(500))
    out, truncated = pp.truncate(text, limit=200)

    assert truncated is True
    assert len(out) <= 200
    assert not out.endswith("번")  # 줄 중간에서 자르지 않는다


def test_truncate_leaves_short_text_alone() -> None:
    out, truncated = pp.truncate("짧은 본문", limit=200)

    assert (out, truncated) == ("짧은 본문", False)


def test_preprocess_flags_too_short() -> None:
    """본문이 사실상 비어 있으면 파싱을 시도하지 않는다 (계약 §1.4 EMPTY)."""
    assert pp.preprocess("자세한 내용은 붙임 파일 참조").too_short is True
    assert pp.preprocess("가" * 500).too_short is False


def test_preprocess_pipeline() -> None:
    raw = "목록\n■ 모집\n- 재학생\n문의 010-1234-5678\n다음글"
    out = pp.preprocess(raw)

    assert "목록" not in out.text
    assert "010-1234-5678" not in out.text
    assert len(out.masked) == 1
    assert out.truncated is False


def test_strip_board_meta_line() -> None:
    """「작성자 ○○ 조회수 1362 등록일 2026.09.04」 — 학교 게시판 메타줄.

    014 에서 등록일 2026.07.13 이 마감일로 잡혔다. 마감일 라벨 「신청」이 제목에 있어서
    윈도우가 다음 줄의 등록일까지 삼켰기 때문이다.
    """
    text = (
        "2026학년도 2학기 수강바구니(수강신청) 일정 안내\n"
        "작성자 문과대학 행정실 조회수 13063 등록일 2026.07.13\n"
        "일정을 다음과 같이 안내드립니다."
    )
    out = pp.strip_boilerplate(text)

    assert "등록일" not in out
    assert "2026.07.13" not in out
    assert "일정을 다음과 같이" in out
