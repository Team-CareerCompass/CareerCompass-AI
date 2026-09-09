from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CC_", extra="ignore")

    version: str = "0.2.0"

    stub_mode: bool = True
    """True 면 모델을 부르지 않고 계약대로의 더미 응답을 낸다."""

    require_internal_token: bool = False
    """BE 는 인증 헤더를 보내지 않는다 (계약 v0.2). 운영에서는 네트워크 경계로 막는다 (#31)."""

    internal_token: str = "dev-token"
    """require_internal_token 이 True 일 때만 쓴다."""

    max_raw_content_chars: int = 40_000
    """계약 §1.1. 초과분은 잘라내고 truncated 로 알린다."""


settings = Settings()
