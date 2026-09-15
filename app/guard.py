"""모델 출력을 믿기 전에 거는 것들 (#4 #29) — 전부 규칙이다. LLM 으로 LLM 을 검증하지 않는다.

- `extract_json` — 펜스·잡담을 벗기고 JSON 객체 하나를 꺼낸다
- `fact_check` — 생성물의 수치·영문 고유명사가 입력에 있는지 문자열 대조
- `trim_to_limit` — 글자 수 상한을 문장 경계에서 지킨다
- `drop_sentences_with` — 검증에 걸린 표현이 든 문장을 통째로 뺀다
- `scrub_pii` — 출력에 섞인 이메일·전화·주민번호 모양을 지운다
- `safe_draft` — 모델 없이, **입력 문자열만으로** 만든 초안. 검증을 끝내 못 통과했을 때의 답
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
    if casual:
        tail = "이 경험을 바탕으로 맡은 일을 성실히 해내고 싶어요."
    elif confident:
        tail = "이 경험으로 맡은 역할을 해낼 수 있습니다."
    else:
        tail = "이 경험을 바탕으로 맡은 역할을 성실히 수행하겠습니다."
    draft = " ".join([head, *body, tail])
    return trim_to_limit(draft, limit) if limit else draft
