"""프로바이더 공통 — 응답 모양과 실패 유형 (#2).

**실패는 셋으로 가른다. 셋의 처리가 다르다.**

| 유형 | 예 | 처리 |
| --- | --- | --- |
| `TransientError` | 타임아웃·5xx·네트워크 | 1회 재시도 → 그래도 안 되면 BE 에 503 |
| `RateLimited` | 429 | 재시도하지 않고 BE 에 429 — BE 가 백오프한다 |
| `InputError` | 401·400 | 키나 요청이 잘못됐다. 재시도해도 같다. BE 에 503 |
| `SchemaViolation` | JSON 이 아니거나 필드가 틀림 | 게이트웨이가 1회 재요청 (#4) |

재시도가 비용을 두 배로 만든다는 것을 잊지 않는다 — 그래서 **1회**다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Completion:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    cached: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ProviderError(Exception):
    """프로바이더 층의 모든 실패."""


class TransientError(ProviderError):
    """일시적 실패 — 한 번 더 부를 가치가 있다."""


class RateLimited(TransientError):
    """429. BE 가 백오프하므로 이쪽은 재시도하지 않는다."""


class InputError(ProviderError):
    """키·요청이 틀렸다. 재시도해도 같은 결과다."""


class SchemaViolation(ProviderError):
    """모델이 요청한 형식을 지키지 않았다 (#4)."""


class Provider(Protocol):
    name: str
    model: str
    price_in_krw: float
    """입력 토큰당 원. `usage.costKrw` 계산에 쓴다 (#28)."""
    price_out_krw: float

    async def complete(
        self, system: str, user: str, *, max_tokens: int, temperature: float
    ) -> Completion: ...


async def complete_with_retry(
    provider: Provider,
    system: str,
    user: str,
    *,
    max_tokens: int,
    temperature: float,
    backoff_s: float = 0.5,
) -> Completion:
    """`TransientError` 만 1회 재시도. `RateLimited` 는 재시도하지 않는다."""
    try:
        return await provider.complete(system, user, max_tokens=max_tokens, temperature=temperature)
    except RateLimited:
        raise
    except TransientError:
        await asyncio.sleep(backoff_s)
        return await provider.complete(system, user, max_tokens=max_tokens, temperature=temperature)


def cost_krw(provider: Provider, completion: Completion) -> float:
    return round(
        completion.prompt_tokens * provider.price_in_krw
        + completion.completion_tokens * provider.price_out_krw,
        4,
    )
