"""HCX 프로바이더 — 요청 모양만 본다. 실호출 0 (`httpx.MockTransport`)."""

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.providers.base import InputError
from app.providers.hcx import PRICES_KRW, THINKING_RESERVE, HcxProvider

OK_BODY = {
    "status": {"code": "20000", "message": "OK"},
    "result": {
        "message": {"role": "assistant", "content": "{}"},
        "usage": {"promptTokens": 10, "completionTokens": 5, "totalTokens": 15},
    },
}


SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


def _send(model: str, json_schema: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """요청 본문을 잡아 돌려준다."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=OK_BODY)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = HcxProvider("nv-test", model, client=client, **kwargs)
            await provider.complete(
                "s", "u", max_tokens=300, temperature=0.1, json_schema=json_schema
            )

    asyncio.run(run())
    return seen


def test_every_model_we_can_pick_has_a_price() -> None:
    """단가가 없으면 비용이 0 으로 잡혀 예산 상한이 막지 못한다."""
    for model in ("HCX-DASH-002", "HCX-005", "HCX-007"):
        assert all(p > 0 for p in PRICES_KRW[model]), model


def test_hcx007_sends_thinking_off_by_default() -> None:
    """HCX-007 기본은 추론 켬(low)이다 — 명시하지 않으면 추론이 한도를 먹고 답이 빈다(09-13)."""
    body = _send("HCX-007")
    assert body["thinking"] == {"effort": "none"}
    assert body["maxCompletionTokens"] == 300


def test_hcx007_thinking_gets_extra_completion_budget() -> None:
    body = _send("HCX-007", thinking_effort="low")
    assert body["thinking"] == {"effort": "low"}
    assert body["maxCompletionTokens"] == 300 + THINKING_RESERVE["low"]


@pytest.mark.parametrize("model", ["HCX-DASH-002", "HCX-005"])
def test_other_models_never_get_thinking(model: str) -> None:
    """추론을 안 받는 모델에 `thinking` 을 보내면 40001 이다. 설정이 켜져 있어도 보내지 않는다."""
    body = _send(model, thinking_effort="high")
    assert "thinking" not in body
    assert body["maxCompletionTokens"] == 300


def test_structured_output_only_where_the_api_accepts_it() -> None:
    """구조화 출력은 HCX-007 · 추론 끔에서만 된다(공식 문서). 다른 조합에 보내면 요청이 깨진다."""
    body = _send("HCX-007", json_schema=SCHEMA)
    assert body["responseFormat"] == {"type": "json", "schema": SCHEMA}
    assert "responseFormat" not in _send("HCX-007", json_schema=SCHEMA, thinking_effort="low")
    assert "responseFormat" not in _send("HCX-005", json_schema=SCHEMA)
    assert "responseFormat" not in _send("HCX-007")  # 스키마를 안 주면 안 보낸다


def test_unknown_thinking_effort_is_rejected() -> None:
    with pytest.raises(InputError):
        HcxProvider("nv-test", "HCX-007", thinking_effort="max")
