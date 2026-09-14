"""모델 출력을 믿기 전에 거는 것들 (#4 #29) — 전부 규칙이다. LLM 으로 LLM 을 검증하지 않는다.

- `extract_json` — 펜스·잡담을 벗기고 JSON 객체 하나를 꺼낸다
- `fact_check` — 생성물의 수치·영문 고유명사가 입력에 있는지 문자열 대조
- `trim_to_limit` — 글자 수 상한을 문장 경계에서 지킨다
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
