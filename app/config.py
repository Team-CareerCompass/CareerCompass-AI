from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CC_", extra="ignore")

    version: str = "0.2.0"

    stub_mode: bool = False
    """True 면 모델을 부르지 않고 계약대로의 더미 응답을 낸다 (`app/stubs.py`).

    기본은 **실호출**이다 — 09-15 까지 True 였는데, BE 가 서버를 붙여도 env 를 안 바꾸면 모든 공고에
    Spring/Kotlin 더미가 가는 함정이었다. 키가 없으면 `service` 가 스텁으로 내려가며 크게 로그한다.
    """

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
    """기본 모델 — 제일 싸다. 아래 기능별 설정이 비어 있으면 이것을 쓴다."""

    hcx_model_parse: str | None = None
    hcx_model_comments: str | None = None
    hcx_model_draft: str | None = None
    """기능별 모델 덮어쓰기. 예: 우대 추출이 아쉬우면 `CC_HCX_MODEL_PARSE=HCX-005` 만 올린다."""

    parse_prompt_version: str | None = "v1"
    """`parse_posting` 프롬프트 버전. **최신이 아니라 평가로 고른 버전**을 쓴다.
    근거는 `app/prompts/README.md`. `None` 이면 가장 높은 번호."""

    llm_timeout_s: float = 18.0
    """BE 읽기 타임아웃 20초보다 낮게 — 이쪽이 먼저 포기해야 BE 가 이유를 안다."""

    # ---- 비용 상한 (#31) — 개발 중 실수 한 번이 청구서로 오지 않게 ---------------

    budget_daily_krw: float = 250.0
    """하루 상한(원). 넘으면 모델을 부르지 않고 503. DASH-002 파싱 ≈600건, HCX-005 ≈120건.
    100원은 005 를 섞어 평가셋 한 바퀴(60원)를 두 번 돌리면 끝이라 250 으로 올렸다 (09-15)."""

    budget_monthly_krw: float = 5000.0
    """한 달 상한. 일 250원 x 20일. 운영에서는 둘 다 올린다 — `docs/COST.md` 「상한」."""
    budget_ledger: Path = Path(".cache/budget.json")

    # ---- 리플레이 캐시 (#35) ---------------------------------------------------

    llm_cache: Literal["off", "record", "replay"] = "off"
    llm_cache_dir: Path = Path(".cache/llm")


settings = Settings()
