"""비용 상한 (#31 #28) — 실수 한 번이 청구서로 오지 않게.

**호출 전에 막는다.** 「지금까지 쓴 것 + 이 호출이 최대로 쓸 수 있는 것」이 상한을 넘으면 모델을
부르지 않고 `BudgetExceeded` 를 낸다. 호출 후에 재는 상한은 이미 돈이 나간 뒤다.

| `CC_BUDGET_DAILY_KRW` | 하루 상한. 기본 250원 — DASH-002 파싱 ≈600건 · HCX-005 ≈120건 |
| `CC_BUDGET_MONTHLY_KRW` | 한 달 상한. 기본 5,000원 |

장부는 `.cache/budget.json` — 날짜별 원화. 프로세스 하나 기준이다. 인스턴스가 여럿이면 각자 센다
(운영은 BE 의 일일 호출 예산이 한 번 더 막는다). 리플레이 캐시 히트는 0원이라 장부에 안 남는다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("careercompass.ai.budget")

WARN_RATIO = 0.8


class BudgetExceeded(Exception):
    def __init__(self, scope: str, spent: float, limit: float, estimate: float) -> None:
        super().__init__(
            f"{scope} 예산 초과: 지금까지 {spent:.2f}원 + 이 호출 최대 {estimate:.2f}원 "
            f"> 상한 {limit:g}원"
        )
        self.scope = scope
        self.spent = spent
        self.limit = limit


@dataclass
class BudgetStatus:
    today_krw: float
    month_krw: float
    daily_limit_krw: float
    monthly_limit_krw: float

    def as_dict(self) -> dict[str, float]:
        return {
            "todayKrw": round(self.today_krw, 2),
            "monthKrw": round(self.month_krw, 2),
            "dailyLimitKrw": self.daily_limit_krw,
            "monthlyLimitKrw": self.monthly_limit_krw,
        }


class Budget:
    def __init__(self, ledger: Path, *, daily_krw: float, monthly_krw: float) -> None:
        self.ledger = ledger
        self.daily_krw = daily_krw
        self.monthly_krw = monthly_krw
        self._warned: set[str] = set()

    # ---- 장부 -------------------------------------------------------------

    def _load(self) -> dict[str, float]:
        if not self.ledger.exists():
            return {}
        try:
            data = json.loads(self.ledger.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("예산 장부를 읽지 못했다 — 0 에서 다시 센다: %s", self.ledger)
            return {}
        return {k: float(v) for k, v in data.items()}

    def _save(self, data: dict[str, float]) -> None:
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).date().isoformat()

    def status(self) -> BudgetStatus:
        data = self._load()
        today = self._today()
        month = today[:7]
        return BudgetStatus(
            today_krw=data.get(today, 0.0),
            month_krw=sum(v for k, v in data.items() if k.startswith(month)),
            daily_limit_krw=self.daily_krw,
            monthly_limit_krw=self.monthly_krw,
        )

    # ---- 검사·기록 ----------------------------------------------------------

    def check(self, estimate_krw: float) -> None:
        """호출 **전에** 부른다. 넘으면 `BudgetExceeded`, 80% 를 넘으면 경고 한 번."""
        st = self.status()
        for scope, spent, limit in (
            ("일일", st.today_krw, self.daily_krw),
            ("월간", st.month_krw, self.monthly_krw),
        ):
            if spent + estimate_krw > limit:
                raise BudgetExceeded(scope, spent, limit, estimate_krw)
            key = f"{scope}:{self._today() if scope == '일일' else self._today()[:7]}"
            if spent >= limit * WARN_RATIO and key not in self._warned:
                self._warned.add(key)
                logger.warning(
                    "%s 예산 %.0f%% 사용: %.2f / %g원", scope, spent / limit * 100, spent, limit
                )

    def record(self, cost_krw: float) -> None:
        if cost_krw <= 0:
            return
        data = self._load()
        today = self._today()
        data[today] = round(data.get(today, 0.0) + cost_krw, 4)
        self._save(data)
