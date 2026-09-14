from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CC_", extra="ignore")

    version: str = "0.2.0"

    stub_mode: bool = True
    """True 면 모델을 부르지 않고 계약대로의 더미 응답을 낸다 (`app/stubs.py`)."""

    require_internal_token: bool = False
    """BE 는 인증 헤더를 보내지 않는다 (계약 v0.2). 운영에서는 네트워크 경계로 막는다 (#31)."""

    internal_token: str = "dev-token"
    """require_internal_token 이 True 일 때만 쓴다."""

    max_raw_content_chars: int = 40_000
    """계약 §1.1. 초과분은 잘라내고 truncated 로 알린다."""

    # ---- 프로바이더 (#2) — 지금은 HCX 하나다 ----------------------------------

    hcx_api_key: str = ""
    """CLOVA Studio 테스트 키. `.env` 에만 둔다. 코드·채팅에 붙여넣지 않는다."""

    hcx_model: str = "HCX-DASH-002"
    """제일 싼 모델. 품질 문제가 숫자로 나오면 HCX-005 로 올린다 (#5)."""

    parse_prompt_version: str | None = "v1"
    """`parse_posting` 프롬프트 버전. **최신이 아니라 평가로 고른 버전**을 쓴다.
    근거는 `app/prompts/README.md`. `None` 이면 가장 높은 번호."""

    llm_timeout_s: float = 18.0
    """BE 읽기 타임아웃 20초보다 낮게 — 이쪽이 먼저 포기해야 BE 가 이유를 안다."""

    # ---- 리플레이 캐시 (#35) ---------------------------------------------------

    llm_cache: Literal["off", "record", "replay"] = "off"
    llm_cache_dir: Path = Path(".cache/llm")


settings = Settings()
