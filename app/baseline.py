"""BE `HeuristicLlmGateway` 이식본 — **기준선을 숫자로 만들기 위한 것** (#5 #7).

우리는 지금까지 「우리 규칙 전용」을 기준선이라고 불렀지만, 보고서가 물을 기준선은 그게 아니다.
BE 가 AI 서버 없이 돌리는 폴백 구현이 진짜 기준선이고, 그 클래스 주석이 그렇게 적어 뒀다:

    "품질은 LLM 구현(CareerCompass-AI)이 대체하는 것을 전제로 한 기준선이다."

그래서 그 자바 코드를 **그대로** 파이썬으로 옮겼다. 원본은
`CareerCompass-BE` 의 `src/main/java/com/careercompass/analysis/gateway/HeuristicLlmGateway.java`
(PR #51, 로컬 `pr-51` 브랜치에서 읽음). BE 저장소는 읽기만 한다 — 고치지 않는다.

**이식하며 일부러 다르게 한 것 하나**: 연도 없는 날짜(`~ 9/11`)에 원본은 `LocalDate.now()` 의
연도를 쓴다. 그러면 측정 결과가 실행한 해에 따라 바뀌므로, 여기서는 픽스처의 `collectedAt`
연도를 쓴다. 이 차이는 **기준선에 유리하다** — 원본보다 맞을 확률이 높다.

전처리도 하지 않는다. BE 는 `rawContent`(Jsoup `body().text()`)를 그대로 넣는다 — 보일러플레이트
제거·연락처 마스킹·주입 문단 제거가 없다. 그것까지 포함해서 기준선이다.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.fixtures import Fixture

_WORD = re.compile(r"[A-Za-z가-힣][A-Za-z0-9가-힣+#.]{1,19}")

STOPWORDS = frozenset(
    [
        "안내",
        "공고",
        "모집",
        "지원",
        "신청",
        "제출",
        "관련",
        "대상",
        "기간",
        "문의",
        "첨부",
        "이상",
        "이하",
        "및",
        "또는",
        "경우",
        "해당",
        "가능",
        "위한",
        "통해",
        "있는",
        "있습니다",
        "합니다",
        "바랍니다",
        "주시기",
        "학년",
        "학점",
        "우대",
        "마감",
        "접수",
        "채용",
        "장학금",
        "공모전",
        "대외활동",
        "내용",
        "예정",
        "진행",
        "운영",
    ]
)
"""원본 그대로. 「채용·장학금·공모전·대외활동」이 들어 있어 **유형 단서를 스스로 지운다**."""

_FULL_DATE = re.compile(r"(20\d{2})[.\-/년]\s?(\d{1,2})[.\-/월]\s?(\d{1,2})")
_SHORT_DATE = re.compile(r"[~까지]\s?(\d{1,2})\s?[/월]\s?(\d{1,2})")
_QUESTION = re.compile(
    r"^[ \t]*(\d)[.)]\s*(.{5,120}?)\s*(?:\((\d{2,5})자[^)]*\))?\s*$", re.MULTILINE
)

_YEAR_REQ = re.compile(r"(\d)\s?학년\s?(?:이상|재학)")
_GPA_REQ = re.compile(r"(?:학점|평점)\s?(\d\.\d+)\s?(?:이상)?")
_MAJOR_REQ = re.compile(r"([가-힣]{2,10}(?:학과|학부|전공|계열))")

MIN_KEYWORDS = 3


def _first_match(text: str, pattern: re.Pattern[str]) -> str | None:
    m = pattern.search(text)
    return m.group(0).strip() if m else None


def extract_keywords(title: str, raw_content: str) -> list[str]:
    """제목 단어에 5점, 본문 단어에 1점. **2점 미만은 버리고** 상위 10개.

    짧은 공고는 어떤 단어도 2점을 못 넘어 3개 미만이 되고, 그러면 파싱이 통째로 실패한다.
    """
    scores: dict[str, int] = {}
    for word in _WORD.findall(title or ""):
        if word not in STOPWORDS:
            scores[word] = scores.get(word, 0) + 5
    for word in _WORD.findall(raw_content or ""):
        if word not in STOPWORDS and len(word) >= 2:
            scores[word] = scores.get(word, 0) + 1
    ranked = sorted(
        ((w, s) for w, s in scores.items() if s >= 2), key=lambda kv: kv[1], reverse=True
    )
    return [w for w, _ in ranked[:10]]


def reclassify(text: str) -> str | None:
    if "장학" in text:
        return "scholarship"
    if "공모전" in text or "경진대회" in text or "해커톤" in text:
        return "contest"
    if "채용" in text or "인턴" in text or "정규직" in text:
        return "recruit"
    if "대외활동" in text or "서포터즈" in text or "봉사" in text:
        return "activity"
    return None


def extract_preferences(raw_content: str) -> list[str]:
    """「우대」가 든 줄. 헤딩(`우대사항:`)은 빼고 5~100자만. 불릿 문자로도 쪼갠다."""
    out: list[str] = []
    for line in re.split(r"[\n·•\-]", raw_content or ""):
        trimmed = line.strip()
        header = (
            trimmed.endswith(":")
            or trimmed.endswith("：")  # noqa: RUF001 — 원본이 전각 콜론도 본다
            or re.fullmatch(r"우대\s?사항?[:：]?", trimmed) is not None  # noqa: RUF001
        )
        if not header and "우대" in trimmed and 5 <= len(trimmed) <= 100:
            out.append(trimmed)
    return out[:10]


def extract_due_date(text: str, today: date) -> date | None:
    """「상시」가 있으면 `None`. 아니면 **마지막** 완전 날짜, 없으면 첫 짧은 날짜."""
    if "상시" in text:
        return None
    result: date | None = None
    for m in _FULL_DATE.finditer(text):
        try:
            result = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
    if result is not None:
        return result
    short = _SHORT_DATE.search(text)
    if short:
        try:
            return date(today.year, int(short.group(1)), int(short.group(2)))
        except ValueError:
            return None
    return None


def extract_questions(raw_content: str) -> list[dict[str, Any]]:
    """**줄 시작 + 번호**가 있어야 문항이다 — 뭉친 텍스트(BE 크롤러 출력)에서는 0개가 된다."""
    out: list[dict[str, Any]] = []
    for m in _QUESTION.finditer(raw_content or ""):
        if len(out) >= 10:
            break
        body = m.group(2).strip()
        if any(k in body for k in ("?", "작성", "기술", "서술", "주세요")):
            out.append(
                {
                    "order": int(m.group(1)),
                    "question": body,
                    "maxChars": int(m.group(3)) if m.group(3) else None,
                }
            )
    return out


def run_baseline(fx: Fixture) -> dict[str, Any]:
    """`app.evaluation.run_rules` 와 같은 키를 낸다 — 같은 채점기로 재려고."""
    title, raw = fx.title, fx.body
    text = f"{title}\n{raw}"
    today = fx.collected_at or date.today()

    keywords = extract_keywords(title, raw)
    failed = len(keywords) < MIN_KEYWORDS  # 원본은 여기서 ParsingFailedException 을 던진다
    due = extract_due_date(text, today)

    return {
        "dueDate": None if failed else (due.isoformat() if due else None),
        "dueDateRaw": None,
        "yearInferred": False,
        "dueReason": None,
        "type": None if failed else reclassify(text),
        "formQuestions": [] if failed else extract_questions(raw),
        "qualifications": {
            "year": _first_match(text, _YEAR_REQ),
            "gpa": _first_match(text, _GPA_REQ),
            "major": _first_match(text, _MAJOR_REQ),
        },
        "preferences": [] if failed else extract_preferences(raw),
        "keywords": [] if failed else keywords,
        # 원본에는 「공고가 아님」 개념이 없다 — 키워드 3개를 넘기면 전부 공고로 본다
        "likelyPosting": not failed,
        "failReason": "NO_KEYWORDS" if failed else None,
        "truncated": False,
        "maskedContacts": 0,  # 전처리 없음 — 연락처가 그대로 프롬프트/응답에 남는다
        "injectionsStripped": 0,
        "resegmented": False,
        "imageOnly": fx.image_only,
    }
