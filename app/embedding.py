"""HCX 임베딩 v2 — 텍스트 하나를 1,024차원 벡터로 (보고서 4.4.4 「분야 유사도」 비교용).

보고서는 Bedrock Titan 임베딩 + pgvector 로 설계했다. Bedrock 은 서울 리전 할당량이 풀리지 않아
같은 자리를 CLOVA Studio 임베딩 v2 로 잰다. 형식은 공식 문서 그대로다:

    POST https://clovastudio.stream.ntruss.com/v1/api-tools/embedding/v2
    Authorization: Bearer nv-...
    {"text": "..."}                                    # 최대 8,192 토큰
    → {"status":{"code":"20000"}, "result":{"embedding":[...1024개], "inputTokens": N}}

- **단가 0.0002원/토큰** (입력만). 생성 호출과 같은 예산 장부를 쓴다 — 호출 전에 막고, 뒤에 적는다.
- **같은 텍스트는 다시 부르지 않는다.** 벡터를 `cache_dir` 에 남긴다 — 비교 스크립트를 몇 번 돌려도
  처음 한 번만 돈이 든다. 파일 이름은 텍스트의 해시이고 텍스트 자체는 적지 않는다.
- 아직 엔드포인트로 내지 않는다. BE 와 계약을 바꿔야 하는 일이라, 비교 결과를 보고 팀이 정한다.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.budget import Budget
from app.providers.base import InputError, ProviderTimeout, RateLimited, TransientError
from app.providers.hcx import BASE_URL

PATH = "/v1/api-tools/embedding/v2"
MODEL = "embedding-v2"
PRICE_KRW_PER_TOKEN = 0.0002
DIMENSION = 1024

TOKENS_PER_CHAR_UPPER = 1.0
"""예산 추정용 상한 — 한국어는 글자 하나가 토큰 하나를 넘지 않는다. 장부에는 `inputTokens` 를
적는다."""

_OK = "20000"  # 숫자로도 문자열로도 온다 — 문자열로 비교한다


@dataclass(frozen=True)
class Embedding:
    vector: list[float]
    input_tokens: int
    cached: bool


class HcxEmbedder:
    def __init__(
        self,
        api_key: str,
        *,
        budget: Budget | None = None,
        cache_dir: Path | None = None,
        timeout_s: float = 10.0,
        base_url: str = BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise InputError("CC_HCX_API_KEY 가 비어 있다")
        self._url = f"{base_url}{PATH}"
        self._api_key = api_key
        self.budget = budget
        self.cache_dir = cache_dir
        self._client = client
        self._timeout_s = timeout_s
        self.calls = 0
        self.spent_krw = 0.0

    def _cache_path(self, text: str) -> Path | None:
        if self.cache_dir is None:
            return None
        digest = hashlib.sha256(f"{MODEL}\n{text}".encode()).hexdigest()
        return self.cache_dir / f"{digest}.json"

    async def embed(self, text: str) -> Embedding:
        text = text.strip()
        if not text:
            raise InputError("빈 텍스트는 임베딩하지 않는다")
        path = self._cache_path(text)
        if path is not None and path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return Embedding(data["embedding"], int(data["inputTokens"]), cached=True)

        if self.budget is not None:
            self.budget.check(len(text) * TOKENS_PER_CHAR_UPPER * PRICE_KRW_PER_TOKEN)
        body = await self._post(text)
        result = body["result"]
        vector = [float(x) for x in result["embedding"]]
        tokens = int(result.get("inputTokens", 0))
        cost = tokens * PRICE_KRW_PER_TOKEN
        if self.budget is not None:
            self.budget.record(cost)
        self.calls += 1
        self.spent_krw += cost
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"embedding": vector, "inputTokens": tokens}), "utf-8")
        return Embedding(vector, tokens, cached=False)

    async def _post(self, text: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "X-NCP-CLOVASTUDIO-REQUEST-ID": uuid.uuid4().hex,
            "Content-Type": "application/json; charset=utf-8",
        }
        payload = {"text": text}
        try:
            if self._client is not None:
                res = await self._client.post(self._url, headers=headers, json=payload)
            else:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    res = await client.post(self._url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"HCX 임베딩 타임아웃: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TransientError(f"HCX 임베딩 네트워크 오류: {exc}") from exc

        if res.status_code == 429:
            raise RateLimited("HCX 임베딩 429")
        if res.status_code >= 500:
            raise TransientError(f"HCX 임베딩 {res.status_code}")
        if res.status_code >= 400:
            raise InputError(f"HCX 임베딩 {res.status_code}: {res.text[:200]}")

        body: dict[str, Any] = res.json()
        status = body.get("status", {})
        code = str(status.get("code"))
        if code != _OK:
            message = f"HCX 임베딩 status {code}: {status.get('message')}"
            if code.startswith("429"):
                raise RateLimited(message)
            raise InputError(message)
        return body


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"차원이 다르다: {len(a)} vs {len(b)}")
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb)
