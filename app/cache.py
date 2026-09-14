"""LLM 응답 리플레이 캐시 (#35) — 개발 중에 같은 요청으로 모델을 두 번 부르지 않는다.

운영 캐시(같은 공고를 두 번 파싱하지 않는 것)는 BE 몫이다. 이것은 **개발·평가 캐시**다.
프롬프트 한 줄 고치고 평가셋 13건을 돌릴 때, 바뀐 프롬프트의 호출만 실제로 나간다.

| `CC_LLM_CACHE` | 동작 |
| --- | --- |
| `off` | 캐시 안 씀 (기본) |
| `record` | 있으면 재생, 없으면 호출하고 녹화 |
| `replay` | 있으면 재생, **없으면 `CacheMiss`** — CI 가 조용히 돈을 쓰지 못하게 |

키에 **프로바이더·모델·프롬프트 버전**이 들어간다. 프롬프트를 고쳤으면 다른 요청이다.
`.cache/llm/` 은 gitignore 다 — 자소서가 든 요청(§3)이 저장소에 들어가면 안 된다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from app.providers.base import Completion

CacheMode = Literal["off", "record", "replay"]


class CacheMiss(Exception):
    """`replay` 모드에서 녹화본이 없다."""


class ReplayCache:
    def __init__(self, root: Path | None = None, mode: CacheMode = "off") -> None:
        if mode != "off" and root is None:
            raise ValueError("캐시 디렉터리가 없다")
        self.root = root or Path(".cache/llm")
        self.mode = mode

    @staticmethod
    def key(
        provider: str,
        model: str,
        prompt_version: str,
        system: str,
        user: str,
        *,
        max_tokens: int,
        temperature: float,
    ) -> str:
        payload = json.dumps(
            {
                "provider": provider,
                "model": model,
                "promptVersion": prompt_version,
                "system": system,
                "user": user,
                "maxTokens": max_tokens,
                "temperature": temperature,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str) -> Completion | None:
        if self.mode == "off":
            return None
        path = self._path(key)
        if not path.exists():
            if self.mode == "replay":
                raise CacheMiss(key)
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return Completion(**{**data["completion"], "cached": True})

    def put(self, key: str, completion: Completion, *, note: str = "") -> None:
        if self.mode != "record":
            return
        self.root.mkdir(parents=True, exist_ok=True)
        data = {"note": note, "completion": {**asdict(completion), "cached": False}}
        self._path(key).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
