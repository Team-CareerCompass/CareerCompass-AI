"""테스트는 `.env` 를 모른다.

CI 에는 `.env` 가 없고 로컬에는 있다. 그 차이로 CI 만 깨졌다 (PR #51) — 「키 없으면 스텁」 규칙이
테스트의 `stub_mode=False` 를 덮어썼다. 반대로 로컬에 키가 있고 스텁이 꺼진 사람이 계약 테스트를
돌리면 HCX 를 실제로 부른다. 둘 다 여기서 막는다: 기본은 스텁·키 없음·캐시 off, 게이트웨이를
쓰는 테스트만 명시적으로 켠다 (`real_gateway` 픽스처).

**나머지 설정도 전부 코드 기본값으로 되돌린다.** 11-15 에 시연용 `.env`(파싱 v2 + HCX-005,
초안 HCX-007)를 넣자 「파싱 프롬프트 기본은 v1」 테스트 둘이 로컬에서만 깨졌다. 값을 하나씩 고르면
다음에 `.env` 에 넣는 줄이 또 샌다.
"""

import pytest

from app import service
from app.config import Settings, settings


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, field in Settings.model_fields.items():
        monkeypatch.setattr(settings, name, field.get_default(call_default_factory=True))
    monkeypatch.setattr(settings, "stub_mode", True)
    monkeypatch.setattr(settings, "hcx_api_key", "")
    monkeypatch.setattr(settings, "llm_cache", "off")
    monkeypatch.setattr(service, "_stub_warned", True)  # 테스트 로그를 어지럽히지 않는다


@pytest.fixture
def real_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """라우터가 스텁이 아니라 게이트웨이로 가게 — 키는 가짜, 프로바이더는 테스트가 바꿔 끼운다."""
    monkeypatch.setattr(settings, "stub_mode", False)
    monkeypatch.setattr(settings, "hcx_api_key", "test-key")
