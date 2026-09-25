"""모델 출력을 믿기 전에 거는 것들 (#4 #29) — 전부 규칙이다. LLM 으로 LLM 을 검증하지 않는다.

- `extract_json` — 펜스·잡담을 벗기고 JSON 객체 하나를 꺼낸다
- `fact_check` — 생성물의 수치·영문 고유명사가 입력에 있는지 문자열 대조
- `trim_to_limit` — 글자 수 상한을 문장 경계에서 지킨다
- `drop_sentences_with` — 검증에 걸린 표현이 든 문장을 통째로 뺀다
- `scrub_pii` — 출력에 섞인 이메일·전화·주민번호 모양을 지운다
- `safe_draft` — 모델 없이, **입력 문자열만으로** 만든 초안. 검증을 끝내 못 통과했을 때의 답
- `ending_ratio` — 문장 어미로 톤을 실측한다. 「~해요」를 부탁해도 모델은 「~합니다」로 쓴다 (#18)
- `unsupported_claims` — 입력에 없는 **자격·수상·소속** 단정. 한국어 날조 중 잡을 수 있는 것만 (#29)
- `discriminatory_hits` — 채용절차법이 금지한 요구(용모·혼인·출신지역·가족 재산)가 출력에 (#29)
- `slurs_in` — 욕설·혐오 표현. 실측 0건이라 보험이다 — **오해의 여지가 없는 말만** 넣는다 (#29)
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.providers.base import SchemaViolation

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?\s*(?:%|년|개월|명|건|위|등|배|회|점|만|억|원|시간|일|주)?")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9+#./-]{1,}")
_SENTENCE_END = re.compile(r"[.!?。]\s*|다\.\s*")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。])\s+")
_SENTENCE_OR_LINE = re.compile(r"(?<=[.!?。])\s+|\n+")
_TRAILING = re.compile(r"[\s.!?。~\"'\u201d\u2019」』)\]]+$")
_FORMAL_END = re.compile(r"(니다|십니까)$")
_CASUAL_END = re.compile(r"(요|죠)$")
TONE_ENDINGS = {"formal": _FORMAL_END, "confident": _FORMAL_END, "casual": _CASUAL_END}
"""톤별 문장 어미. formal·confident 는 「~니다」, casual 은 「~요」「~죠」."""
_PII_OUT = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.]+"  # 이메일
    r"|\b0\d{1,2}[-.)]\s?\d{3,4}[-.]\d{4}\b|\b01[016-9][-.]?\d{3,4}[-.]?\d{4}\b"  # 전화
    r"|\b\d{6}[-\s]?[1-4]\d{6}\b"  # 주민등록번호 모양
)


def extract_json(text: str) -> dict[str, Any]:
    """HCX 는 ```json 펜스로 감싸 온다. 펜스가 없으면 첫 `{` 부터 마지막 `}` 까지 본다."""
    m = _FENCE.search(text)
    candidate = m.group(1) if m else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end < start:
        raise SchemaViolation("JSON 객체가 없다")
    try:
        data = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError as exc:
        raise SchemaViolation(f"JSON 파싱 실패: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise SchemaViolation("JSON 최상위가 객체가 아니다")
    return data


def _normalize(s: str) -> str:
    return re.sub(r"[\s,]", "", s).lower()


def fact_check(answer: str, sources: list[str]) -> list[str]:
    """생성물에 나왔는데 입력 어디에도 없는 수치·영문 토큰. 비어 있으면 통과.

    한국어 고유명사는 형태 변화 때문에 문자열 대조가 안 된다 — 여기서는 잡지 않는다.
    **잡는 것만 확실히 잡는다.** 없는 수치(「매출 30% 증가」)와 없는 기술명이 가장 흔한 날조다.
    """
    haystack = _normalize(" ".join(sources))
    unverified: list[str] = []
    for m in _NUMBER.finditer(answer):
        token = m.group(0).strip()
        digits = re.sub(r"\D", "", token)
        if len(digits) < 2:
            continue  # 「1개」「2명」 같은 한 자리는 서술어에 가깝다
        if _normalize(token) not in haystack and digits not in haystack:
            unverified.append(token)
    for m in _LATIN.finditer(answer):
        token = m.group(0)
        if _normalize(token) not in haystack:
            unverified.append(token)
    # 순서 유지 중복 제거
    return list(dict.fromkeys(unverified))


def trim_to_limit(text: str, limit: int) -> str:
    """상한을 넘으면 **문장 경계**에서 자른다. 경계가 없으면 하드 컷."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = text[:limit]
    ends = [m.end() for m in _SENTENCE_END.finditer(head)]
    if ends and ends[-1] > limit // 2:
        return head[: ends[-1]].rstrip()
    return head.rstrip()


def drop_sentences_with(text: str, tokens: list[str]) -> str:
    """검증에 걸린 표현이 든 문장을 통째로 뺀다. 표현만 지우면 문장이 거짓말로 남는다.

    「Redis 를 사용해 본 경험이 있습니다」에서 Redis 만 지우면 「를 사용해 본 경험이 있습니다」다.
    """
    if not tokens:
        return text
    norm_tokens = [_normalize(t) for t in tokens if t]
    kept: list[str] = []
    for para in text.split("\n"):
        sentences = [s for s in _SENTENCE_SPLIT.split(para) if s.strip()]
        kept_para = [s for s in sentences if not any(t in _normalize(s) for t in norm_tokens)]
        kept.append(" ".join(kept_para))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def sentences(text: str) -> list[str]:
    """문장 부호 뒤 공백·줄바꿈에서 나눈다. 어미 판정과 재생성 겹침 측정이 같은 단위를 쓴다."""
    return [s.strip() for s in _SENTENCE_OR_LINE.split(text) if s.strip()]


def ending_ratio(text: str, tone: str) -> float | None:
    """문장 중 그 톤의 어미로 끝나는 비율. 문장이 없으면 None.

    v1 실측(09-16): casual 을 부탁한 14건 중 12건이 「~합니다」체였다 — 프롬프트만으로는 안 되고
    글자 수처럼 **실측해서 재요청**해야 한다. 「~다.」로 끝나는 문어체는 어느 쪽도 아니다.
    """
    pattern = TONE_ENDINGS.get(tone, _FORMAL_END)
    sents = sentences(text)
    if not sents:
        return None
    hits = sum(bool(pattern.search(_TRAILING.sub("", s))) for s in sents)
    return hits / len(sents)


CLAIM_WORDS: dict[str, tuple[str, ...]] = {
    "자격": ("자격증", "면허", "수료증", "인증서", "취득", "기사"),
    "수상": (
        "수상",
        "최우수",
        "우수상",
        "장려상",
        "입상",
        "금상",
        "은상",
        "대상을 받",
        "대상 수상",
    ),
    "소속": ("입사", "재직", "근무 경험", "정규직", "인턴십을 수료"),
}
"""입력에 없으면 날조가 되는 **범주**. 한국어 고유명사 전체를 잡으려는 시도는 실패했다 (#29).

221개 실측 답에 「입력에 없는 한국어 명사」를 전부 플래그해 봤더니 답 하나당 30건이었다 —
「통해」「또한」「능력」 같은 말이 쏟아진다. 문서빈도로 걸러도 답당 10건에 재현율만 떨어졌다.
**그래서 범주를 좁혔다.** 자격·수상·소속은 셋 다 「입력이 말하지 않았으면 지어낸 것」이 확실하고,
같은 범주의 말이 입력에 있으면 봐준다 — 그래야 「정보처리기사 취득」(입력)을 「자격증을
취득했습니다」(답)로 바꿔 쓴 것을 날조로 오해하지 않는다.

재현율은 좁다. 회사명(「엔씨소프트에 입사한다면」)은 「입사」로 걸리지만 「타입스크립트」처럼
영문의 한글 표기는 여기서 안 잡힌다 — 평가셋의 `forbidden` 이 사후에 잡는다.
"""


DISCRIMINATORY: dict[str, re.Pattern[str]] = {
    "신체조건": re.compile(
        r"용모\s*(?:단정|수려|준수)|신체\s?조건|(?:키|신장)\s*\d{3}|체중\s*\d{2}|"
        r"(?:키|신장|체중)\s*(?:제한|기준)"
    ),
    "혼인": re.compile(
        r"미혼\s*(?:자|인|여성|남성|에\s?한|만)|기혼\s*(?:자|여성|남성)|"
        r"혼인\s*(?:여부|상태|관계)|결혼\s*(?:여부|유무)"
    ),
    "출신지역": re.compile(r"출신\s?지역|본적지|거주지\s*제한|(?:서울|수도권)\s*거주자에\s*한"),
    "가족": re.compile(
        r"(?:부모|모친|부친|가족|보호자)(?:님)?의?\s*(?:직업|재산|소득|학력|근무)|"
        r"직계\s?존비속|형제자매의\s*(?:직업|학력)"
    ),
    "성별지정": re.compile(
        r"(?:남자|여자|남성|여성)\s*(?:만|에\s?한(?:함|해)|우대|지원\s?가능)|성별\s*[:：]\s*(?:남|여)"  # noqa: RUF001
    ),
}
"""**채용절차법 제4조의3** 이 구직자에게 요구하지 못하게 한 것 — 용모·키·체중 등 신체조건,
출신지역·혼인 여부·재산, 직계존비속과 형제자매의 학력·직업·재산. 남녀고용평등법 제7조의 성별
지정도 함께 본다.

**낱말이 아니라 요구 맥락으로 잡는다.** 낱말 목록은 실제로 못 쓴다는 것을 실측했다(#29):
「성별」「연령」「병역」 같은 낱말을 그대로 찾으면 실물 공고 31건 중 28건이 걸리는데
「금**신장**학재단」「기**본적**인」「학력·연령·성별 **제한없음**」「병역필 또는 면제자」처럼
거의 전부 오탐이다. 병역·연령은 법에 근거가 따로 있어(병역법·청년고용촉진특별법) 그 자체로는
차별이 아니다 — 그래서 목록에 없다.

맥락 패턴으로 바꾸니 실물 31건에서 **검출 0**(오탐 0), 합성 위법 표본 3건은 전부 잡았다.
이것은 공고가 위법인지 판정하는 기능이 아니다 — **그런 요구가 우리 출력을 타고 사용자에게
전달되는 것**을 막는 것이다. 판정은 사람과 법의 몫이다.
"""


SLURS = (
    "씨발",
    "시발",
    "좆",
    "병신",
    "븅신",
    "새끼",
    "지랄",
    "닥쳐",
    "꺼져",
    "찐따",
    "틀딱",
    "맘충",
    "김치녀",
    "한남충",
    "급식충",
    "장애인 같",
    "애자",
)
"""출력에서 그냥 지울 말. **오해의 여지가 없는 것만 넣는다.**

실측(09-25): 기록된 답 278개에 욕설·비하 표현 **0건**. 모델이 자소서를 쓰면서 욕할 일은
사실상 없다 — 그래서 이것은 잡으려는 그물이 아니라 보험이다. 보험이니 **오탐이 0 이어야**
한다: 「미친 듯이 노력했습니다」「무능함을 느꼈습니다」「한심했던 저를」처럼 자소서에
정상적으로 쓰이는 말은 넣지 않았다. 유해 표현 분류기를 붙이지 않는 이유도 같다 —
학습 데이터가 없고, 정상 문장을 거르면 사용자가 쓴 진짜 반성문이 사라진다.
"""


def slurs_in(text: str) -> list[str]:
    """욕설·혐오 표현이 들어 있으면 그 말들. 하나라도 나오면 출력이 잘못된 것이다."""
    return [w for w in SLURS if w in text]


def discriminatory_hits(text: str) -> list[str]:
    """그런 요구가 이 문자열에 들어 있는가. 걸린 범주 이름들 (#29)."""
    return [name for name, pattern in DISCRIMINATORY.items() if pattern.search(text)]


def unsupported_claims(text: str, sources: list[str]) -> list[str]:
    """답이 말했는데 입력은 같은 범주를 한 번도 말하지 않은 자격·수상·소속 표현 (#29).

    실측 221개 답에서 3건이 걸렸고 셋 다 진짜였다 — 오탐 0. 좁은 대신 확실하다.
    """
    haystack = " ".join(sources)
    found: list[str] = []
    for words in CLAIM_WORDS.values():
        if any(w in haystack for w in words):
            continue  # 입력이 같은 범주를 말했다면 답이 말하는 것도 근거가 있다
        found += [w for w in words if w in text]
    return list(dict.fromkeys(found))


def scrub_pii(text: str) -> tuple[str, int]:
    """출력에 이메일·전화·주민번호 모양이 있으면 지운다. 입력에도 없어야 정상이라 개수를 센다."""
    scrubbed, n = _PII_OUT.subn("[삭제]", text)
    return scrubbed, n


def safe_draft(
    question: str,
    posting_title: str,
    experiences: list[str],
    *,
    tone: str = "formal",
    limit: int = 0,
) -> str:
    """모델 없이 만드는 초안 — 검증을 끝내 못 통과했거나 모델이 답을 못 냈을 때 (#29).

    **입력 문자열만 쓴다.** 경험 요약을 그대로 인용하므로 날조가 생길 수 없다. 대신 문장이
    빤하다 — 사용자가 고쳐 쓰라고 에디터에 올리는 출발점이지 완성본이 아니다.
    """
    casual = tone == "casual"
    confident = tone == "confident"
    if posting_title:
        head = f"{posting_title}에 지원해요." if casual else f"{posting_title}에 지원합니다."
    else:
        head = (
            "이 질문에 제 경험으로 답해 볼게요."
            if casual
            else f"{question}에 대해 말씀드리겠습니다."
        )
    suffix = "경험이 있어요." if casual else "경험이 있습니다."
    body = [f"{s.strip().rstrip('.')} {suffix}" for s in experiences[:3] if s.strip()]
    if not body:
        # 근거가 0 이면 모델도 부르지 않는다 (#16). 무엇을 하면 되는지를 답 자리에 적는다.
        body = [
            "등록된 경험 카드가 없어 근거가 되는 사실을 넣지 못했어요. "
            "경험 카드를 등록한 뒤 다시 생성하면 그 내용으로 초안을 써요."
            if casual
            else "등록된 경험 카드가 없어 근거가 되는 사실을 넣지 못했습니다. "
            "경험 카드를 등록한 뒤 다시 생성하면 그 내용으로 초안을 작성합니다."
        ]
    if casual:
        tail = "이 경험을 바탕으로 맡은 일을 성실히 해내고 싶어요."
    elif confident:
        tail = "이 경험으로 맡은 역할을 해낼 수 있습니다."
    else:
        tail = "이 경험을 바탕으로 맡은 역할을 성실히 수행하겠습니다."
    if not any(s.strip() for s in experiences):
        tail = ""  # 「이 경험을 바탕으로」할 경험이 없다
    draft = " ".join(x for x in [head, *body, tail] if x)
    return trim_to_limit(draft, limit) if limit else draft
