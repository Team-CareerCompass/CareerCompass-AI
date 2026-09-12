"""규칙 기반 추출 — LLM 없이 뽑을 수 있는 것들.

**순서가 중요하다.** 보고서 4.4.3 은 「마감일이 누락되면 정규식으로 보조 추출」이라고
적었지만 거꾸로다. 정규식이 이기는 자리는 정규식이 **먼저** 가야 한다 — 결정적이고,
공짜고, 틀렸을 때 왜 틀렸는지 알 수 있다. LLM 은 규칙이 못 하거나 틀리는 곳에 쓴다.

여기서 뽑은 것은 그대로 쓰거나(글자 수 제한), LLM 결과와 대조하는 기준으로 쓴다(마감일).
"""

import re
from dataclasses import dataclass, field
from datetime import date

# --------------------------------------------------------------------------
# 마감일
# --------------------------------------------------------------------------

_DATE = re.compile(
    r"(?:(?P<year>\d{2,4})\s*[.\-/년]\s*)?"
    r"(?P<month>\d{1,2})\s*[.\-/월]\s*"
    r"(?P<day>\d{1,2})\s*일?"
)
_TIME = re.compile(r"(?P<hour>\d{1,2})\s*(?::\s*(?P<minute>\d{2})|시)")

# 우선순위 순. 앞의 것이 잡히면 뒤는 보지 않는다.
_DEADLINE_LABELS: tuple[tuple[str, ...], ...] = (
    ("접수기한", "접수 기한", "제출기한", "제출 기한", "마감일시", "마감일"),
    ("접수기간", "접수 기간", "신청기간", "신청 기간", "모집기간", "모집 기간"),
    ("지원기간", "지원 기간", "접수", "신청", "모집"),
)

# 윈도우를 여기서 끊는다. 마감일이 아닌 날짜가 바로 뒤에 오는 자리들이다.
_WINDOW_END = re.compile(r"결과|발표|통보|지급|증명|발급|활동\s?기간|교육\s?기간|오리엔테이션")

_NO_DEADLINE = re.compile(r"상시\s?모집|선착순|예산\s?소진|수시\s?모집|충원\s?시")

_WINDOW_CHARS = 180


@dataclass
class DueDate:
    iso: str | None = None
    raw: str | None = None
    year_inferred: bool = False
    """연도가 본문에 없어 수집일 기준으로 추정했다. 신뢰도가 낮다는 신호다."""

    has_time: bool = False
    reason: str | None = None
    """`no_deadline`(상시·선착순) / `not_found`(못 읽음). 둘은 다르다 (#8)."""


def _to_iso(year: int | None, month: int, day: int, fallback_year: int) -> str | None:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    resolved = fallback_year if year is None else (2000 + year if year < 100 else year)
    try:
        return date(resolved, month, day).isoformat()
    except ValueError:
        return None


def _window(text: str, start: int) -> str:
    """라벨 뒤 한 조각. 문단이 끝나거나 마감일이 아닌 날짜가 나오면 거기서 끊는다."""
    chunk = text[start : start + _WINDOW_CHARS]
    if (para := chunk.find("\n\n")) > 0:
        chunk = chunk[:para]
    if (stop := _WINDOW_END.search(chunk, 1)) is not None:
        chunk = chunk[: stop.start()]
    return chunk


def extract_due_date(text: str, collected_at: date | None = None) -> DueDate:
    """접수 마감일을 뽑는다. **못 읽으면 null 이다 — 지어내지 않는다.**

    없는 마감일을 만들면 사용자가 D-1 알림을 받고 지원했는데 이미 끝나 있다.
    「상시 모집」처럼 마감일이 없는 것과, 마감일이 있는데 못 읽은 것을 가른다.
    """
    fallback_year = (collected_at or date.today()).year

    for group in _DEADLINE_LABELS:
        for label in group:
            for hit in re.finditer(re.escape(label), text):
                chunk = _window(text, hit.start())

                found: list[tuple[str | None, int, int, int]] = []
                for m in _DATE.finditer(chunk):
                    month, day = int(m.group("month")), int(m.group("day"))
                    if not (1 <= month <= 12 and 1 <= day <= 31):
                        continue
                    found.append((m.group("year"), month, day, m.end()))

                if not found:
                    continue

                # 범위 표현이면 뒤쪽이 마감일이다. 앞쪽을 집으면 틀린다.
                year_raw, month, day, end = found[-1]

                # 연도가 없으면 같은 구간의 앞 날짜에서 빌린다. 그것도 없으면 수집일 기준.
                inferred = year_raw is None
                if inferred:
                    for prev_year, *_ in found[:-1]:
                        if prev_year is not None:
                            year_raw = prev_year
                            inferred = False
                            break

                year = int(year_raw) if year_raw is not None else None
                iso = _to_iso(year, month, day, fallback_year)
                if iso is None:
                    continue

                return DueDate(
                    iso=iso,
                    raw=chunk.strip()[:80],
                    year_inferred=inferred,
                    has_time=_TIME.search(chunk[end : end + 12]) is not None,
                )

    if _NO_DEADLINE.search(text):
        return DueDate(reason="no_deadline")
    return DueDate(reason="not_found")


# --------------------------------------------------------------------------
# 지원서 양식
# --------------------------------------------------------------------------

_MAX_CHARS = re.compile(
    r"(?:띄어쓰기\s?포함\s?)?(\d{2,5})\s*자\s*(?:이내|이하|내외|정도|미만)?", re.IGNORECASE
)
_QUESTION_LINE = re.compile(
    r"^\s*(?:[-•*]|\d+[.)]|[①-⑩])\s*(?P<q>.{6,120}?)\s*(?:\((?P<limit>[^)]*자[^)]*)\))?\s*$",
    re.MULTILINE,
)
_QUESTION_HINT = re.compile(r"(?:해\s?주세요|하시오|서술|작성|기술하|기재|적어|설명해)")

# 흔한 오탐 — 문의처·제출 방법 안내문이 질문형 어미를 쓴다 (#10).
#   「궁금한 사항을 메일로 문의해 주세요」 · 「작성한 서류를 구글폼에 업로드하여 제출」
_QUESTION_EXCLUDE = re.compile(
    r"문의|메일|이메일|연락|전화|홈페이지|링크|다운로드|업로드|첨부|"
    r"제출\s?(?:방법|서류|처|기한)|접수\s?(?:방법|처|기간)|방문|등기|우편|참고하|확인하"
)


@dataclass
class FormQuestion:
    order: int
    question: str
    max_chars: int | None = None


def extract_max_chars(text: str) -> int | None:
    """「500자 이내」·「띄어쓰기 포함 800자」. 정규식이 거의 100% 맞는 자리다."""
    m = _MAX_CHARS.search(text)
    return int(m.group(1)) if m else None


def extract_form_questions(text: str) -> list[FormQuestion]:
    """자소서 문항을 뽑는다.

    **없음과 못 찾음을 가른다** — 서류 없이 지원하는 장학금이 정상적으로 존재한다(#10).
    문의처·제출 방법 안내문이 흔한 오탐이라 질문형 어미를 요구한다.
    """
    questions: list[FormQuestion] = []
    for m in _QUESTION_LINE.finditer(text):
        body = m.group("q").strip()
        if not _QUESTION_HINT.search(body) or _QUESTION_EXCLUDE.search(body):
            continue
        limit = extract_max_chars(m.group("limit") or "") or extract_max_chars(body)
        questions.append(FormQuestion(order=len(questions) + 1, question=body, max_chars=limit))
    return questions


# --------------------------------------------------------------------------
# 자격·우대
# --------------------------------------------------------------------------

_YEAR_REQ = re.compile(r"(\d)\s*학년\s*(?:이상|이하|재학|만)")
_GPA_REQ = re.compile(r"(?:학점|평점|성적)\s*(?:[^\n]{0,12}?)(\d\.\d+)\s*(?:이상|/|만점)")
_MAJOR_REQ = re.compile(r"([가-힣]{2,12}(?:학과|학부|전공|계열))")

_PREFERENCE_HEADING = re.compile(
    r"^[^\n]{0,6}(?:우대\s?(?:사항|조건|요건)|가점|우대)[^\n]{0,10}$", re.MULTILINE
)
_BULLET = re.compile(r"^\s*(?:[-•*▶·]|\d+[.)]|[가-하][.)])\s*(?P<item>.{4,80})\s*$")


@dataclass
class Qualifications:
    year: str | None = None
    gpa: str | None = None
    major: str | None = None


def extract_qualifications(text: str) -> Qualifications:
    year = _YEAR_REQ.search(text)
    gpa = _GPA_REQ.search(text)
    major = _MAJOR_REQ.search(text)
    return Qualifications(
        year=year.group().strip() if year else None,
        gpa=gpa.group().strip() if gpa else None,
        major=major.group(1) if major else None,
    )


def extract_preferences(text: str) -> list[str]:
    """「우대 사항」 헤딩 아래의 불릿을 모은다. 헤딩이 없으면 빈 목록이다."""
    heading = _PREFERENCE_HEADING.search(text)
    if heading is None:
        return []

    items: list[str] = []
    for line in text[heading.end() :].split("\n")[1:]:
        if not line.strip():
            if items:
                break
            continue
        m = _BULLET.match(line)
        if m is None:
            break
        items.append(m.group("item").strip())
        if len(items) >= 10:
            break
    return items


# --------------------------------------------------------------------------
# 유형
# --------------------------------------------------------------------------

_TYPE_HINTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("scholarship", re.compile(r"장학(?:금|생|재단|회)|등록금\s?지원|생활비\s?지원")),
    ("contest", re.compile(r"공모전|경진\s?대회|해커톤|아이디어\s?공모|콘테스트")),
    ("activity", re.compile(r"서포터즈|기자단|대외\s?활동|앰배서더|체험단|봉사단")),
    ("recruit", re.compile(r"채용|신입\s?사원|인턴|모집\s?부문|경력\s?사원|입사")),
)


def guess_type(title: str, text: str) -> str | None:
    """제목에 가중치를 준다. 본문에는 다른 공고 얘기가 섞여 있을 수 있다."""
    scores: dict[str, int] = {}
    for name, pattern in _TYPE_HINTS:
        scores[name] = len(pattern.findall(title)) * 5 + len(pattern.findall(text))

    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else None


# --------------------------------------------------------------------------
# 공고인가
# --------------------------------------------------------------------------

_APPLICABLE = re.compile(r"모집|선발|접수|신청|지원\s?(?:자격|대상|방법)|응모|참가\s?신청")
_ELIGIBILITY = re.compile(r"자격|대상|요건|조건")


@dataclass
class PostingSignals:
    applicable: bool = False
    has_eligibility: bool = False
    has_deadline: bool = False
    hits: list[str] = field(default_factory=list)

    @property
    def score(self) -> int:
        return sum((self.applicable, self.has_eligibility, self.has_deadline))

    @property
    def likely_posting(self) -> bool:
        """셋 중 둘 이상이면 공고로 본다.

        애매하면 공고로 본다 — 잘못 들어온 것은 눈으로 넘기면 되지만,
        안 들어온 것은 존재를 모른다 (계약 §1.3).
        """
        return self.score >= 2


def posting_signals(text: str, due: DueDate | None = None) -> PostingSignals:
    """「애초에 공고가 아닌 글」을 1차로 거른다.

    사용자가 학사공지를 통째로 등록하면(F2-1) 「수강신청 안내」·「졸업요건 변경」이
    함께 수집된다. 최종 판정은 LLM 이 하되, 여기서 확실한 것은 먼저 거른다.
    """
    signals = PostingSignals()
    if (m := _APPLICABLE.search(text)) is not None:
        signals.applicable = True
        signals.hits.append(m.group())
    if (m := _ELIGIBILITY.search(text)) is not None:
        signals.has_eligibility = True
        signals.hits.append(m.group())
    signals.has_deadline = due is not None and (due.iso is not None or due.reason == "no_deadline")
    return signals
