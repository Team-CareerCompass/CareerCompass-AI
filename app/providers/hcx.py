"""네이버 CLOVA Studio HyperCLOVA X (#2).

2026-09-13 실측한 형식 그대로다 (`.notes/HANDOFF.md` §1).

    POST https://clovastudio.stream.ntruss.com/v3/chat-completions/{model}
    Authorization: Bearer nv-...
    {"messages":[{"role":"system",...},{"role":"user",...}],
     "maxCompletionTokens": 800, "temperature": 0.1}
    → {"status":{"code":20000}, "result":{"message":{"content":"..."}, "usage":{...}}}

- `content` 는 문자열이다. `maxTokens` 가 아니라 **`maxCompletionTokens`** 다.
- JSON 이 ```json 펜스로 감싸져 온다 — 벗기는 것은 게이트웨이 몫.
- **HCX-007 은 하이브리드 추론 모델**이다. 기본이 추론 켬(`thinking.effort: low`)이라 09-13 에
  20토큰 한도를 추론이 다 먹고 답이 비었다. 그래서 **추론 강도를 항상 명시해서** 보낸다 —
  기본 `none`(추론 끔). 추론 토큰은 출력에 합산·과금되므로 켜면 그만큼 한도를 더 준다.
- 기본 모델은 **HCX-DASH-002** — 제일 싸다 (IN 0.00025 / OUT 0.001 원/토큰).
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.providers.base import (
    Completion,
    InputError,
    ProviderTimeout,
    RateLimited,
    TransientError,
)

BASE_URL = "https://clovastudio.stream.ntruss.com"

# 원/토큰 — 인퍼런스 기본 요금 (2026-09 기준, HANDOFF §6)
PRICES_KRW: dict[str, tuple[float, float]] = {
    "HCX-DASH-002": (0.00025, 0.001),
    "HCX-005": (0.00125, 0.005),
    "HCX-007": (0.00125, 0.005),
}
"""**여기 없는 모델은 비용이 0 으로 잡혀 예산 상한이 막지 못한다** — 모델을 늘리면 같이 늘린다."""

THINKING_MODELS = frozenset({"HCX-007"})
"""`thinking` 을 받는 모델. 다른 모델에 보내면 40001 이다."""

THINKING_RESERVE: dict[str, int] = {"none": 0, "low": 4096, "medium": 8192, "high": 16384}
"""추론 토큰 몫으로 `maxCompletionTokens` 에 더 주는 양. 안 주면 추론이 한도를 먹고 답이 빈다.
값은 공식 기본 한도(low 5120 · medium 10240 · high 20480)에서 답 몫을 뺀 근사치다."""

_OK = "20000"  # 숫자로도 문자열로도 온다 — 문자열로 비교한다


class HcxProvider:
    name = "hcx"

    def __init__(
        self,
        api_key: str,
        model: str = "HCX-DASH-002",
        *,
        timeout_s: float = 18.0,
        base_url: str = BASE_URL,
        client: httpx.AsyncClient | None = None,
        thinking_effort: str = "none",
    ) -> None:
        if not api_key:
            raise InputError("CC_HCX_API_KEY 가 비어 있다")
        if thinking_effort not in THINKING_RESERVE:
            raise InputError(
                f"thinking effort 는 {sorted(THINKING_RESERVE)} 중 하나: {thinking_effort}"
            )
        self.model = model
        self.price_in_krw, self.price_out_krw = PRICES_KRW.get(model, (0.0, 0.0))
        self.thinking_effort = thinking_effort if model in THINKING_MODELS else None
        self.reserved_completion_tokens = THINKING_RESERVE.get(self.thinking_effort or "none", 0)
        """예산 추정이 「이 호출 최대 비용」을 셀 때 더한다 — 추론 토큰도 출력 단가로 과금된다."""
        self._url = f"{base_url}/v3/chat-completions/{model}"
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json; charset=utf-8",
        }
        self._client = client
        self._timeout_s = timeout_s

    async def complete(
        self, system: str, user: str, *, max_tokens: int, temperature: float
    ) -> Completion:
        payload: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "maxCompletionTokens": max_tokens + self.reserved_completion_tokens,
            "temperature": temperature,
        }
        if self.thinking_effort is not None:
            payload["thinking"] = {"effort": self.thinking_effort}
        started = time.perf_counter()
        try:
            if self._client is not None:
                res = await self._client.post(self._url, headers=self._headers, json=payload)
            else:
                # 요청마다 클라이언트를 연다 — 평가 스크립트가 asyncio.run 을 여러 번 부르면
                # 공유 클라이언트가 닫힌 루프에 묶인다. 이 호출량에서 연결 재사용은 의미 없다.
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    res = await client.post(self._url, headers=self._headers, json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"HCX 타임아웃: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TransientError(f"HCX 네트워크 오류: {exc}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        if res.status_code == 429:
            raise RateLimited("HCX 429")
        if res.status_code >= 500:
            raise TransientError(f"HCX {res.status_code}")
        if res.status_code in (400, 401, 403, 404):
            raise InputError(f"HCX {res.status_code}: {res.text[:200]}")

        body: dict[str, Any] = res.json()
        status = body.get("status", {})
        if str(status.get("code")) != _OK:
            # 40001 등 — 요청 문제. 42901 은 한도.
            code = status.get("code")
            message = f"HCX status {code}: {status.get('message')}"
            if str(code).startswith("429"):
                raise RateLimited(message)
            raise InputError(message)

        result = body["result"]
        usage = result.get("usage", {})
        return Completion(
            text=result["message"]["content"],
            prompt_tokens=int(usage.get("promptTokens", 0)),
            completion_tokens=int(usage.get("completionTokens", 0)),
            latency_ms=latency_ms,
        )
