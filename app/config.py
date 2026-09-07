from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CC_", extra="ignore")

    internal_token: str = "dev-token"
    """BE 와 공유하는 내부 호출 토큰. 사용자 JWT 는 이 서비스로 넘어오지 않는다."""

    stub_mode: bool = True
    """True 면 모델을 부르지 않고 계약대로의 더미 응답을 낸다."""

    version: str = "0.1.0"

    max_raw_content_chars: int = 40_000
    """계약 §1.1. 초과분은 잘라내고 truncated 로 알린다."""

    max_images: int = 4
    """계약 §1.5."""

    image_fallback_threshold: int = 200
    """계약 §1.5. rawContent 가 이보다 짧고 이미지가 있으면 이미지 경로."""


settings = Settings()
