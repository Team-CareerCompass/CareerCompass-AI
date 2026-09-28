"""규칙 기반 추출 — LLM 없이 뽑을 수 있는 것들.

**순서가 중요하다.** 보고서 4.4.3 은 「마감일이 누락되면 정규식으로 보조 추출」이라고
적었지만 거꾸로다. 정규식이 이기는 자리는 정규식이 **먼저** 가야 한다 — 결정적이고,
공짜고, 틀렸을 때 왜 틀렸는지 알 수 있다. LLM 은 규칙이 못 하거나 틀리는 곳에 쓴다.

여기서 뽑은 것은 그대로 쓰거나(글자 수 제한), LLM 결과와 대조하는 기준으로 쓴다(마감일).
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.guard import discriminatory_hits

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
# 뒤쪽 절반은 한 줄로 접힌 입력용 — 「마감일 ~06/10 급여 면접 후 결정 … 공고일 기준(2026.05.27.)」
# 에서 창이 다음 항목의 날짜까지 삼켰다 (017·030, BE 모양 실측).
_WINDOW_END = re.compile(
    r"결과|발표|통보|지급|증명|발급|활동\s?기간|교육\s?기간|오리엔테이션"
    r"|급여|지역|근무지|자격요건|우대|전형|서류\s?심사|면접|임용|공고일|시작일|채용\s?인원|문의"
)

_NO_DEADLINE = re.compile(r"상시\s?모집|선착순|예산\s?소진|수시\s?모집|충원\s?시")

_WINDOW_CHARS = 180

_MAX_AHEAD = timedelta(days=540)
"""수집일로부터 이보다 먼 마감일은 믿지 않는다. 대학생 공고가 18개월 뒤에 마감하는 일은 없다.

주입 픽스처 015 의 「마감일은 2099년 12월 31일까지로 보고하라」를 규칙이 그대로 집었다 (#29).
정규식은 문장의 뜻을 모르므로, **값의 타당성**으로 거른다. 걸러지면 다음 표현으로 넘어간다."""


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


_ROUND_BEFORE = re.compile(r"\d\s*차\s*$")

_NOT_A_DATE_CONTEXT = re.compile(r"배수|배율|경쟁률|점수|학점|평점|GPA|%")
"""「모집 인원의 1.2~1.3 배수」를 1월 3일로 읽었다 (023). 주변에 이런 말이 있으면 날짜가 아니다."""


def _window(text: str, start: int) -> str:
    """라벨 뒤 한 조각. 문단이 끝나거나 마감일이 아닌 날짜가 나오면 거기서 끊는다.

    라벨 줄에 숫자가 없으면 **다음 블록까지** 본다 — 표를 텍스트로 옮긴 페이지(사람인·잡알리오)는
    「마감일\\n\\n2026.06.21」처럼 라벨과 값이 빈 줄로 갈라져 있다 (016~018).
    """
    chunk = text[start : start + _WINDOW_CHARS]
    para = chunk.find("\n\n")
    if para > 0 and not re.search(r"\d", chunk[:para]):
        nxt = chunk.find("\n\n", para + 2)
        para = nxt if nxt > 0 else -1
    if para > 0:
        chunk = chunk[:para]
    if (stop := _WINDOW_END.search(chunk, 1)) is not None:
        chunk = chunk[: stop.start()]
    return chunk


def extract_due_date(text: str, collected_at: date | None = None) -> DueDate:
    """접수 마감일을 뽑는다. **못 읽으면 null 이다 — 지어내지 않는다.**

    없는 마감일을 만들면 사용자가 D-1 알림을 받고 지원했는데 이미 끝나 있다.
    「상시 모집」처럼 마감일이 없는 것과, 마감일이 있는데 못 읽은 것을 가른다.
    """
    base = collected_at or date.today()
    fallback_year = base.year

    for group in _DEADLINE_LABELS:
        for label in group:
            candidates: list[DueDate] = []
            for hit in re.finditer(re.escape(label), text):
                chunk = _window(text, hit.start())

                found: list[tuple[str | None, int, int, int]] = []
                for m in _DATE.finditer(chunk):
                    month, day = int(m.group("month")), int(m.group("day"))
                    if not (1 <= month <= 12 and 1 <= day <= 31):
                        continue
                    if _NOT_A_DATE_CONTEXT.search(chunk[max(0, m.start() - 12) : m.end() + 6]):
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
                if iso is None or date.fromisoformat(iso) > base + _MAX_AHEAD:
                    continue

                candidates.append(
                    DueDate(
                        iso=iso,
                        raw=chunk.strip()[:80],
                        year_inferred=inferred,
                        has_time=_TIME.search(chunk[end : end + 12]) is not None,
                    )
                )
                # 「1차 접수 기간 … 2차 접수 기간」처럼 차수가 붙은 라벨은 끝까지 모아
                # 마지막 차수를 집는다 (021). 차수가 없으면 첫 매치가 답이다.
                if not _ROUND_BEFORE.search(text[max(0, hit.start() - 8) : hit.start()]):
                    break

            if candidates:
                return max(candidates, key=lambda d: d.iso or "")

    if _NO_DEADLINE.search(text):
        return DueDate(reason="no_deadline")
    return DueDate(reason="not_found")


# --------------------------------------------------------------------------
# 지원서 양식
# --------------------------------------------------------------------------

_MAX_CHARS = re.compile(
    r"(?:띄어쓰기\s?포함\s?)?(?P<n>\d{2,5})\s*자\s*(?P<bound>이내|이하|내외|정도|미만|이상)?",
    re.IGNORECASE,
)
_QUESTION_LINE = re.compile(
    r"^\s*(?:[-•*]|\d+[.)]|[①-⑩])\s*(?P<q>.{6,120}?)\s*(?:\((?P<limit>[^)]*자[^)]*)\))?\s*$",
    re.MULTILINE,
)
# 문항은 **끝이 요청형**이다. 「~하세요」·「~해 주세요」·「~하시오」·「~까?」.
# 「지원서 작성 내용이 … 취소됩니다」처럼 문장 중간에 작성·기술이 나오는 안내문은 문항이 아니다
# (009 에서 실제로 잡혔다).
_QUESTION_HINT = re.compile(
    r"(?:하세요|해\s?주세요|하시오|하십시오|주십시오|주세요|바랍니다|까\??)\s*[.!?]?\s*$"
)

# 자소서 문항이 절대 묻지 않는 것. 주입된 가짜 문항(015)이나 피싱을 문항으로 내보내지 않는다 (#29).
SENSITIVE_QUESTION = re.compile(
    r"주민\s?(?:등록\s?)?번호|계좌\s?번호|비밀\s?번호|카드\s?번호|여권\s?번호|공인\s?인증|OTP",
    re.IGNORECASE,
)

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
    """「500자 이내」·「띄어쓰기 포함 800자」. 정규식이 거의 100% 맞는 자리다.

    **「50자 이상 400자 이내」에서는 400 이다.** 최소·최대가 같이 오면 최대를 집는다 —
    009(두산)에서 첫 매치를 집어 50 을 냈었다. 「이상」만 있는 값은 하한이라 버린다.
    """
    upper: int | None = None
    bare: int | None = None
    for m in _MAX_CHARS.finditer(text):
        n, bound = int(m.group("n")), m.group("bound")
        if bound == "이상":
            continue
        if bound is not None:
            upper = upper if upper is not None else n
        elif bare is None:
            bare = n
    return upper if upper is not None else bare


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
        if SENSITIVE_QUESTION.search(body) or discriminatory_hits(body):
            # 채용절차법 4조의3 — 「가족사항(부모님의 직업과 재산)을 기재해 주세요」 같은 문항은
            # 공고에 있어도 옮기지 않는다 (#29). 초안 생성까지 흘러가면 사용자가 쓰게 된다
            continue
        limit = extract_max_chars(m.group("limit") or "") or extract_max_chars(body)
        questions.append(FormQuestion(order=len(questions) + 1, question=body, max_chars=limit))
    return questions


# --------------------------------------------------------------------------
# 자격·우대
# --------------------------------------------------------------------------

# 자격 조건은 **줄 단위로** 본다. 본문 아무 데나 있는 「3학년」·「영상의학과」를 집으면 날조다 —
# 평가셋에 정답을 붙이고 나서야 보였다 (#5). 어느 줄이 자격을 말하는지 먼저 가린다.

_QUAL_HEADING = re.compile(
    r"지원\s?자격|신청\s?자격|응시\s?자격|참가\s?자격|응모\s?자격|자격\s?요건|지원\s?요건|"
    r"선발\s?대상|모집\s?대상|참여\s?대상|(?<!취업)지원\s?대상|"
    r"성적\s?기준|학점\s?기준|이수\s?학점|학년\s?기준"
)
"""자격 절의 시작. 「취업**지원대상**자」가 헤딩으로 잡히던 것을 막았다 — 040 에서 우대 절이
자격 절로 다시 열려 「환경관련학과」를 조건이라고 냈다."""

_BULLET_LINE = re.compile(r"^\s*[-•*▶·ㅇ○◦]\s*")
"""기호 불릿 줄은 **항목이지 헤딩이 아니다.** 번호(「3. 지원자격」)는 헤딩일 수 있다."""

_SECTION_END = re.compile(
    r"^[^\n]{0,4}(?:우대\s?(?:사항|조건|요건)?|가점|전형\s?(?:절차|일정|방법)?|제출\s?서류|"
    r"접수\s?(?:방법|기간)|문의\s?(?:처)?|시상\s?(?:내역|규모)?|유의\s?사항|기타\s?사항)"
    r"[^\n]{0,8}$"
)
"""여기서 자격 절이 끝난다 — **헤딩 줄일 때만**. 문장 속에 「전형」이 나왔다고 닫으면
017 처럼 아직 오지 않은 전공 조건을 놓친다."""

_LINE_SKIP = re.compile(
    r"우대|가점|추천\s?인원|모집\s?인원|선발\s?인원|채용\s?인원|배수|합격자?\s?발표|"
    r"시상|상금|장학금액|지원금|^\s*\d+\s*[.)]\s*[가-힣]{2,8}(?:과|실|팀|부|센터)\s"
)
"""이 줄 하나만 건너뛴다(절은 계속). 028 「10. 영상의학과 5급 의료기사」가 여기서 걸린다."""

QUAL_BLOCK_LINES = 14
"""헤딩 뒤 몇 줄까지 자격 절로 볼 것인가. 빈 줄로 끊지 않는다 — 003 처럼 항목 사이가
한 줄씩 비어 있는 공지가 흔하다. 017 은 헤딩과 전공 줄 사이에 잡음이 9줄 있었다."""

# 학년·학기. **학년을 먼저 본다** — 「2026 년도 1 학기 재학 예정인 … 3 학년 1 학기」에서
# 학기를 먼저 찾으면 입학 연도의 학기를 집는다(020 에서 실제로 그랬다).
# 「2027학년도」의 7을 학년으로 집지 않게 앞뒤를 막는다.
_YEAR_GRADE = re.compile(
    r"(?<!\d)\d\s*학년(?!도)(?:\s*\d\s*학기)?\s*(?:이상|이하|재학|진학|등록|편입)?"
)
_YEAR_SEMESTER = re.compile(
    r"(?<!\d)\d\s*~\s*\d\s*학기\s*(?:재학|이상|진학)|(?<!년도\s)(?<!\d)\d\s*학기\s*(?:진학|재학)"
)
# 학점·평점. 「4.5만점 기준 3.5학점 이상」처럼 만점과 기준이 함께 오는 꼴을 **먼저** 본다 —
# 앞에서부터 찾으면 002 에서 「12학점이상 이수 및 성적 4.5만점」을 집었다.
_GPA_PATTERNS = (
    # 「4.5만점 기준 3.5학점 이상」처럼 만점과 기준이 함께 오는 꼴을 **먼저** 본다.
    # 교대(|)로 묶으면 정규식은 더 좋은 쪽이 아니라 **더 앞쪽**을 고른다 — 002 에서
    # 「12학점이상 이수 및 성적 4.5만점」을 집었다.
    re.compile(r"\d\.\d+\s*(?:\(|/)?\s*(?:만점)?[^\n]{0,10}?\d\.\d+\s*(?:학점|점)?\s*이상"),
    re.compile(r"(?:백분위|백분율)[^\n]{0,14}?\d{2,3}(?:\s*/\s*100)?\s*점?\s*이상"),
    re.compile(r"(?:학점|평점|성적)[^\n]{0,14}?\d\.\d+[^\n]{0,12}?(?:이상|만점)"),
    re.compile(r"성적[^\n]{0,12}?\d{2,3}\s*점\s*이상"),
)
# 전공. 「전공 무관」이면 조건이 없는 것이다 — 1차에서 「무관, 고졸 이상 학사학위…」를 통째로
# 전공 조건이라고 냈다(038·039·040). 목록은 **학문 이름처럼 생긴 것**만 받는다.
_MAJOR_UNRESTRICTED = re.compile(r"전공[^\n]{0,24}?(?:무관|불문|상관\s?없|제한\s?없)")
_MAJOR_ITEM = r"[가-힣A-Za-z]{2,12}"
_MAJOR_LABEL = re.compile(
    r"전공\s?(?:분야|계열)?\s*[:：]?\s*(?P<list>"  # noqa: RUF001
    + _MAJOR_ITEM
    + r"(?:\s?[/,·]\s?"
    + _MAJOR_ITEM
    + r"){1,7})"
)
_MAJOR_REQ = re.compile(
    _MAJOR_ITEM
    + r"(?:\s?[/,·]\s?"
    + _MAJOR_ITEM
    + r"){0,7}(?:\s?관련)?\s?(?:학과(?![장생])|학부(?!생)|전공|계열)"
)
_MAJOR_STOP = (
    "자격요건",
    "수행업무",
    "지원자격",
    "응시자격",
    "모집",
    "제한",
    "무관",
    "기타",
    "서류",
    "전형",
    "경력",
    "제외",
    "졸업예정자",
    "소지자",
    "이상",
    "포함",
)
"""전공 이름이 아닌 것. 목록에 이런 말이 섞이면 전공 조건이 아니다 —
009 「수행업무/전공/자격요건」·002 「전공대, 대학원대 … 은 제외」가 그랬다."""
_MAJOR_ANY = re.compile(r"전공|계열|학과|학부")

_PREFERENCE_HEADING = re.compile(
    r"^[^\n]{0,6}(?:우대\s?(?:사항|조건|요건)|가점|우대)[^\n]{0,10}$", re.MULTILINE
)
_BULLET = re.compile(r"^\s*(?:[-•*▶·]|\d+[.)]|[가-하][.)])\s*(?P<item>.{4,80})\s*$")


@dataclass
class Qualifications:
    year: str | None = None
    gpa: str | None = None
    major: str | None = None


def _qualification_lines(text: str) -> list[str]:
    """자격을 말하는 줄만 고른다.

    자격 헤딩(「지원자격」·「성적기준」 등)이 있는 줄과 그 뒤 몇 줄을 본다. 헤딩 줄 자체도
    조건을 담는다 — 019 「3. 지원자격 : … 5학기 진학예정자, 현재 2학년」.
    """
    picked: list[str] = []
    budget = 0
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if _SECTION_END.match(line):
            budget = 0  # 다음 절(우대·전형·제출서류)이 시작됐다
            continue
        if _LINE_SKIP.search(line):
            continue  # 이 줄만 건너뛴다 — 절은 아직 열려 있다
        if _QUAL_HEADING.search(line) and not _BULLET_LINE.match(line):
            budget = QUAL_BLOCK_LINES
            picked.append(line)
            continue
        if budget > 0:
            budget -= 1
            picked.append(line)
    return picked


def extract_qualifications(text: str) -> Qualifications:
    """학년·학점·전공. **자격을 말하는 줄에서만** 찾는다 (#7).

    전에는 본문 전체에서 첫 정규식 일치를 집었다. 그래서 모집 인원 배분(「자연.이공계열 80%」)·
    부서 목록(「10. 영상의학과 5급 의료기사」)·우대 문구(「환경관련학과」)를 자격 조건이라고 냈다.
    평가셋에 정답 라벨을 붙이고 나서야 드러난 것들이다 (`docs/EVAL.md`).
    """
    lines = _qualification_lines(text)
    year = gpa = major = None
    for line in lines:
        if year is None and (m := _YEAR_GRADE.search(line)):
            year = m.group().strip()
        if gpa is None:
            for pattern in _GPA_PATTERNS:
                if m := pattern.search(line):
                    gpa = m.group().strip()
                    break
        if major is None:
            major = _major_in(line)
    if year is None:  # 학년으로 못 말한 공고는 학기로 말한다 (023 「5~8 학기 재학생」)
        for line in lines:
            if m := _YEAR_SEMESTER.search(line):
                year = m.group().strip()
                break
    return Qualifications(year=year, gpa=gpa, major=major)


def _major_in(line: str) -> str | None:
    """이 줄이 전공 **조건**을 말하는가. 「전공 무관」은 조건이 아니다."""
    if not _MAJOR_ANY.search(line) or _MAJOR_UNRESTRICTED.search(line):
        return None
    label = _MAJOR_LABEL.search(line)
    candidate = None
    if label:
        candidate = label.group("list").strip().rstrip(".,·")
    elif m := _MAJOR_REQ.search(line):
        candidate = m.group().strip()
    if candidate is None or any(stop in candidate for stop in _MAJOR_STOP):
        return None
    return candidate


def extract_preferences(text: str) -> list[str]:
    """「우대 사항」 헤딩 아래의 불릿을 모은다. 헤딩이 없으면 빈 목록이다.

    채용절차법이 금지한 요구가 든 항목은 뺀다 (#29).
    """
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
        item = m.group("item").strip()
        if discriminatory_hits(item):
            continue  # 「부모의 직업이 안정적인 자」 같은 우대는 내보내지 않는다 (#29)
        items.append(item)
        if len(items) >= 10:
            break
    return items


# --------------------------------------------------------------------------
# 유형
# --------------------------------------------------------------------------

_TYPE_HINTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("scholarship", re.compile(r"장학(?:금|생|재단|회)|등록금\s?지원|생활비\s?지원")),
    ("contest", re.compile(r"공모전|경진\s?대회|해커톤|아이디어\s?공모|콘테스트")),
    (
        "activity",
        re.compile(
            r"서포터즈|기자단|대외\s?활동|앰배서더|체험단|봉사단|크리에이터|인플루언서|홍보\s?대사|멘토단"
        ),
    ),
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

# 학사 행정 어휘 — 제목에 있으면 공고가 아니다. 「신청·기간·대상」이 다 있어도 그렇다.
# 010(이수의무 면제 신청)·014(수강바구니) 가 규칙 신호만으로는 공고로 보였다.
_ACADEMIC_ADMIN = re.compile(
    r"수강\s?신청|수강\s?바구니|이수\s?의무|이수\s?면제|졸업\s?요건|졸업\s?사정|등록금\s?납부|"
    r"휴학|복학|성적\s?(?:정정|열람|공시)|학사\s?일정|계절\s?학기|수강\s?정정|시험\s?시간표|"
    # 022 등록일정 · 024 군e러닝 교과목 안내 — 기간·대상이 있어도 지원 공고가 아니다
    r"등록\s?일정|등록\s?기간|\[등록\]|교과목\s?(?:안내|개설|홍보)|e러닝|취득학점\s?포기"
)
_ELIGIBILITY = re.compile(r"자격|대상|요건|조건")


@dataclass
class PostingSignals:
    applicable: bool = False
    has_eligibility: bool = False
    has_deadline: bool = False
    academic_admin: bool = False
    """제목에 학사 행정 어휘가 있다. 다른 신호와 무관하게 공고가 아니다."""

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
        return not self.academic_admin and self.score >= 2


def posting_signals(text: str, due: DueDate | None = None, title: str = "") -> PostingSignals:
    """「애초에 공고가 아닌 글」을 1차로 거른다.

    사용자가 학사공지를 통째로 등록하면(F2-1) 「수강신청 안내」·「졸업요건 변경」이
    함께 수집된다. 최종 판정은 LLM 이 하되, 여기서 확실한 것은 먼저 거른다.
    """
    signals = PostingSignals()
    if (m := _ACADEMIC_ADMIN.search(title)) is not None:
        signals.academic_admin = True
        signals.hits.append(m.group())
        return signals
    if (m := _APPLICABLE.search(text)) is not None:
        signals.applicable = True
        signals.hits.append(m.group())
    if (m := _ELIGIBILITY.search(text)) is not None:
        signals.has_eligibility = True
        signals.hits.append(m.group())
    signals.has_deadline = due is not None and (due.iso is not None or due.reason == "no_deadline")
    return signals
