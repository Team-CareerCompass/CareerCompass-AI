"""네이버 CLOVA Studio HyperCLOVA X (#2).

2026-09-13 실측한 형식 그대로다 (`.notes/HANDOFF.md` §1).

    POST https://clovastudio.stream.ntruss.com/v3/chat-completions/{model}
    Authorization: Bearer nv-...
    {"messages":[{"role":"system",...},{"role":"user",...}],
     "maxCompletionTokens": 800, "temperature": 0.1}
    → {"status":{"code":20000}, "result":{"message":{"content":"..."}, "usage":{...}}}

- `content` 는 문자열이다. `maxTokens` 가 아니라 **`maxCompletionTokens`** 다.
- JSON 이 ```json 펜스로 감싸져 온다 — 벗기는 것은 게이트웨이 몫.
- **HCX-007 은 추론 모델**이라 thinking 토큰이 출력에 합산·과금된다. 쓰지 않는다.
- 기본 모델은 **HCX-DASH-002** — 제일 싸다 (IN 0.00025 / OUT 0.001 원/토큰).
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.providers.base import (
    Completion,
    InputError,
    RateLimited,
    TransientError,
)

BASE_URL = "https://clovastudio.stream.ntruss.com"

# 원/토큰 — 인퍼런스 기본 요금 (2026-09 기준, HANDOFF §6)
PRICES_KRW: dict[str, tuple[float, float]] = {
    "HCX-DASH-002": (0.00025, 0.001),
    "HCX-005": (0.00125, 0.005),
}

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
    ) -> None:
        if not api_key:
            raise InputError("CC_HCX_API_KEY 가 비어 있다")
        self.model = model
        self.price_in_krw, self.price_out_krw = PRICES_KRW.get(model, (0.0, 0.0))
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
        payload = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "maxCompletionTokens": max_tokens,
            "temperature": temperature,
        }
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
            raise TransientError(f"HCX 타임아웃: {exc}") from exc
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
