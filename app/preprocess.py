"""공고 본문 전처리 — LLM 호출 전에 도는 순수 함수들.

**여기가 파싱 품질과 토큰 비용에 모델 선택보다 크게 영향을 준다.** 게시판 공통 요소가
섞인 채로 프롬프트에 들어가면 엉뚱한 키워드가 뽑히고, 길이를 안 자르면 비용이 새고,
연락처가 그대로 나가면 개인정보가 외부로 나간다.

보고서 4.4 에는 이 단계가 없다. 계약 v0.2 §1.1 이 「HTML→텍스트 변환과 보일러플레이트
제거는 BE 수집 단계의 몫」이라고 정했지만, **BE 가 놓친 것이 넘어와도 여기서 한 번 더
거른다** — 들어오는 텍스트를 이쪽이 통제하지 못하기 때문이다.
"""

import re
from dataclasses import dataclass, field

MAX_CHARS = 40_000
"""계약 §1.1. 초과분은 잘라내고 truncated 로 알린다."""

MIN_MEANINGFUL_CHARS = 200
"""계약 §1.4. 이보다 짧으면 파싱을 시도하지 않는다."""

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile("[ \t\u00a0\u3000]+")  # NBSP·전각 공백 — 웹 복사본에 흔하다
_BLANK_LINES = re.compile(r"\n{3,}")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"\b0\d{1,2}[-.)]\s?\d{3,4}[-.]\d{4}\b")
_MOBILE = re.compile(r"\b01[016-9][-.]?\d{3,4}[-.]?\d{4}\b")

# 게시판 공통 요소. 줄 전체가 이것뿐이면 버린다.
_BOILERPLATE_LINE = re.compile(
    r"^\s*(?:"
    r"이전\s?글|다음\s?글|목록(?:으로)?|글쓰기|수정|삭제|인쇄|스크랩|공유(?:하기)?|"
    r"좋아요|댓글\s?\d*|조회\s?수?\s?\d*|추천\s?\d*|첨부\s?파일|파일\s?다운로드|"
    r"맨\s?위로|top|prev|next|list|share|print"
    r")\s*$",
    re.IGNORECASE,
)

# 게시판 메타줄 — 「작성자 ○○ 조회수 1362 등록일 2026.09.04」. 학교 게시판에 흔하다.
# 등록일이 마감일로 잡히는 것을 막는다 (014 에서 실제로 잡혔다).
_COLON = "[:：]"  # noqa: RUF001 — 반각·전각 콜론 둘 다. 학교 게시판이 전각을 쓴다
_BOARD_META_LINE = re.compile(
    r"^\s*(?:작성자|작성일|등록일|조회\s?수|게시일|첨부)\s*" + _COLON + r"?\s*\S.*"
    r"(?:조회\s?수|등록일|작성일|게시일)\s*" + _COLON + r"?\s*[\d.\-\s]+\s*$"
)

# 링커리어 등 수집처가 본문 끝에 붙이는 자기 홍보.
_TRAILER = re.compile(
    r"\n?\s*대학생\s*대외활동\s*공모전\s*채용\s*사이트.*$|\n?\s*https?://linkareer\.com/?\s*$",
    re.IGNORECASE,
)


@dataclass
class Preprocessed:
    text: str
    truncated: bool = False
    masked: list[str] = field(default_factory=list)
    """마스킹한 원본 값들. 로그에 남기지 않는다 — 개수 확인용이다."""

    @property
    def too_short(self) -> bool:
        return len(self.text.strip()) < MIN_MEANINGFUL_CHARS


def normalize(text: str) -> str:
    """제어문자 제거, 공백·개행 정리. 줄바꿈은 살린다 — 문단 경계의 유일한 단서다."""
    text = _CONTROL.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip()


def strip_boilerplate(text: str) -> str:
    """게시판 공통 요소를 걷어낸다. 본문 안에 섞인 것은 건드리지 않는다."""
    kept = [
        line
        for line in text.split("\n")
        if not _BOILERPLATE_LINE.match(line) and not _BOARD_META_LINE.match(line)
    ]
    return _TRAILER.sub("", "\n".join(kept)).strip()


def mask_contacts(text: str) -> tuple[str, list[str]]:
    """이메일·전화번호를 지운다.

    공고는 공개 게시물이지만 담당자 연락처는 **제3자의 개인정보**다. 공고 파싱은
    프로바이더 제약이 없는 유일한 기능이라 해외 모델로 나갈 수 있는데, 그때 연락처가
    같이 나가면 곤란하다. 파싱 대상 필드도 아니라 지워도 품질에 영향이 없다.
    """
    found: list[str] = []

    def _swap(pattern: re.Pattern[str], placeholder: str, target: str) -> str:
        def repl(m: re.Match[str]) -> str:
            found.append(m.group())
            return placeholder

        return pattern.sub(repl, target)

    text = _swap(_EMAIL, "[이메일]", text)
    text = _swap(_MOBILE, "[연락처]", text)
    text = _swap(_PHONE, "[연락처]", text)
    return text, found


def truncate(text: str, limit: int = MAX_CHARS) -> tuple[str, bool]:
    """길이 상한. 문장 중간에서 자르지 않도록 마지막 개행까지만 남긴다."""
    if len(text) <= limit:
        return text, False
    head = text[:limit]
    cut = head.rfind("\n")
    return (head[:cut] if cut > limit // 2 else head), True


def preprocess(raw: str, *, limit: int = MAX_CHARS) -> Preprocessed:
    """정규화 → 보일러플레이트 제거 → 연락처 마스킹 → 절단."""
    text = strip_boilerplate(normalize(raw))
    text, masked = mask_contacts(text)
    text, truncated = truncate(text, limit)
    return Preprocessed(text=text, truncated=truncated, masked=masked)
