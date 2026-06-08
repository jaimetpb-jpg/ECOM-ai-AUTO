"""
shared/budget_governor.py — LLM Budget Governor V5.2

CRÍTICO: Protege contra runaway costs de LLM API.
LLMUsageTracker registra costos pero NO los detiene.
Este módulo DETIENE llamadas cuando se excede el límite diario.

Por qué es crítico:
  - Un loop infinito en n8n o un bug en el orchestrator puede generar
    $1,000+ en costos de API en minutos sin este módulo.
  - Ejemplo real: CrewAI con max_iter sin límite + niche muy amplio
    → 500+ llamadas a Sonnet en 10 minutos = $75 en un ciclo.

Diseño:
  - Límites diarios por tier (no globales) para granularidad
  - Hard stop + Slack alert cuando se excede
  - Slack warning al 80% para que el operador intervenga antes del hard stop
  - Persistencia opcional en Redis (fallback a memoria)
  - Thread-safe con asyncio.Lock

Integración en LLMRouter.route():
    if not await budget_governor.check_and_record(tier, estimated_cost):
        raise BudgetExceededError(f"Daily LLM budget exceeded for tier '{tier}'")

V5.2 Budget defaults (calibrados para operación real):
    bulk:      $3/día   — Groq es free, pero por safety
    ops:       $8/día   — Haiku para operaciones batch
    creative:  $12/día  — GPT-4o Mini para hooks
    strategic: $20/día  — Sonnet para decisiones importantes
    total:     $35/día  — Hard cap absoluto del sistema
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Dict, Optional, Tuple

from shared.logging_utils import log_info, log_warning, log_error

logger = logging.getLogger(__name__)


# ─── Custom exceptions ────────────────────────────────────────────────────────

class BudgetExceededError(Exception):
    """Raised when the daily LLM budget for a tier is exceeded."""
    def __init__(self, tier: str, spent: float, limit: float):
        self.tier = tier
        self.spent = spent
        self.limit = limit
        super().__init__(
            f"Daily LLM budget exceeded: tier='{tier}' "
            f"spent=${spent:.4f} limit=${limit:.2f}"
        )


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class TierUsage:
    """Daily usage tracking per tier."""
    tier: str
    daily_limit_usd: float
    spent_usd: float = 0.0
    calls: int = 0
    throttled_calls: int = 0
    alert_80_sent: bool = False
    throttled: bool = False
    last_call_ts: float = field(default_factory=time.time)

    @property
    def pct_used(self) -> float:
        if self.daily_limit_usd <= 0:
            return 0.0
        return (self.spent_usd / self.daily_limit_usd) * 100.0

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.daily_limit_usd - self.spent_usd)

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "daily_limit_usd": self.daily_limit_usd,
            "spent_usd": round(self.spent_usd, 4),
            "calls": self.calls,
            "throttled_calls": self.throttled_calls,
            "pct_used": round(self.pct_used, 2),
            "remaining_usd": round(self.remaining_usd, 4),
            "throttled": self.throttled,
            "alert_80_sent": self.alert_80_sent,
        }


@dataclass
class DailyBudgetReport:
    """Complete daily budget snapshot."""
    date: str
    total_limit_usd: float
    total_spent_usd: float
    total_calls: int
    total_throttled: int
    tiers: Dict[str, dict]
    overall_throttled: bool = False

    @property
    def total_pct_used(self) -> float:
        if self.total_limit_usd <= 0:
            return 0.0
        return (self.total_spent_usd / self.total_limit_usd) * 100.0

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "total_limit_usd": self.total_limit_usd,
            "total_spent_usd": round(self.total_spent_usd, 4),
            "total_calls": self.total_calls,
            "total_throttled": self.total_throttled,
            "total_pct_used": round(self.total_pct_used, 2),
            "overall_throttled": self.overall_throttled,
            "tiers": self.tiers,
        }


# ─── BudgetGovernor ───────────────────────────────────────────────────────────

class BudgetGovernor:
    """
    Financial kill switch for LLM API costs.

    Prevents catastrophic runaway spending by enforcing hard daily limits
    per tier and an absolute total daily cap.

    Usage in LLMRouter:
        governor = BudgetGovernor(slack=slack_notifier)
        # Inside route():
        est_cost = self._estimate_cost(tier, prompt)
        allowed = await governor.check_and_record(tier, est_cost)
        if not allowed:
            raise BudgetExceededError(tier, ...)

    Standalone:
        governor = BudgetGovernor()
        report = governor.get_daily_report()
        print(report.to_dict())

    Custom limits (override defaults via constructor or .env):
        governor = BudgetGovernor(daily_limits={
            "bulk":      5.0,
            "ops":       15.0,
            "creative":  25.0,
            "strategic": 40.0,
            "total":     75.0,
        })
    """

    # Default daily limits — tuned for a real ecommerce AI operation
    # Override via constructor or environment variables:
    #   BUDGET_LIMIT_BULK, BUDGET_LIMIT_OPS, etc.
    DEFAULT_DAILY_LIMITS: Dict[str, float] = {
        "bulk":      3.0,    # Groq free tier, $3 safety net
        "ops":       8.0,    # Haiku for summaries/ops
        "creative":  12.0,   # GPT-4o Mini for hooks
        "strategic": 20.0,   # Sonnet for strategy
        "total":     35.0,   # Absolute daily hard cap
    }

    WARNING_THRESHOLD = 0.80   # Send Slack alert at 80% of limit
    COST_PER_TOKEN = {         # Mirror of LLMUsageTracker in llm_router.py
        "bulk":      0.0,
        "ops":       0.0008 / 1000,
        "creative":  0.0015 / 1000,
        "strategic": 0.015  / 1000,
    }

    def __init__(
        self,
        slack=None,
        daily_limits: Optional[Dict[str, float]] = None,
        redis_client=None,
    ):
        self._slack = slack
        self._redis = redis_client
        self._lock = asyncio.Lock()

        # Resolve limits: constructor > env vars > defaults
        self._limits = self._resolve_limits(daily_limits)

        # In-memory daily state (resets at midnight via _get_today_key())
        self._today_key: str = ""
        self._usage: Dict[str, TierUsage] = {}

        log_info(
            logger, "budget_governor_initialized",
            limits={k: f"${v}" for k, v in self._limits.items()},
        )

    # ── Public API ────────────────────────────────────────────────────────────

    async def check_and_record(
        self,
        tier: str,
        cost_usd: float,
    ) -> bool:
        """
        Check if budget allows this call and record the cost if yes.

        Returns True if the call is allowed.
        Returns False if the budget is exceeded (caller should NOT make the LLM call).

        Also triggers Slack alerts:
          - At 80% of tier limit → warning (non-blocking)
          - At 100% of tier limit → hard stop (blocking, returns False)
          - At 100% of total limit → hard stop (blocks ALL tiers)

        Thread-safe via asyncio.Lock.
        """
        async with self._lock:
            self._ensure_today()
            usage = self._get_or_create_tier(tier)

            # ── Check total daily hard cap ──────────────────────────────────
            total_spent = sum(u.spent_usd for u in self._usage.values())
            total_limit = self._limits.get("total", 999.0)

            if (total_spent + cost_usd) > total_limit:
                usage.throttled_calls += 1
                await self._alert_hard_stop(
                    tier, total_spent, total_limit, reason="total_daily_cap"
                )
                log_warning(
                    logger, "budget_total_hard_stop",
                    tier=tier, total_spent=round(total_spent, 4),
                    total_limit=total_limit, cost_blocked=round(cost_usd, 6),
                )
                return False

            # ── Check tier-specific limit ───────────────────────────────────
            tier_limit = self._limits.get(tier, 999.0)

            if (usage.spent_usd + cost_usd) > tier_limit:
                usage.throttled_calls += 1
                usage.throttled = True
                await self._alert_hard_stop(
                    tier, usage.spent_usd, tier_limit, reason="tier_daily_cap"
                )
                log_warning(
                    logger, "budget_tier_hard_stop",
                    tier=tier, spent=round(usage.spent_usd, 4),
                    limit=tier_limit, cost_blocked=round(cost_usd, 6),
                )
                return False

            # ── Record cost (call is allowed) ───────────────────────────────
            usage.spent_usd += cost_usd
            usage.calls += 1
            usage.last_call_ts = time.time()

            # ── Check 80% warning threshold ─────────────────────────────────
            if (
                not usage.alert_80_sent
                and usage.spent_usd >= tier_limit * self.WARNING_THRESHOLD
            ):
                usage.alert_80_sent = True
                await self._alert_warning_80(tier, usage.spent_usd, tier_limit)

            # ── Check total 80% warning ─────────────────────────────────────
            total_new = sum(u.spent_usd for u in self._usage.values())
            if total_new >= total_limit * self.WARNING_THRESHOLD:
                # Only send once per day
                if not getattr(self, "_total_80_sent", False):
                    self._total_80_sent = True
                    await self._alert_total_warning(total_new, total_limit)

            log_info(
                logger, "budget_call_allowed",
                tier=tier, cost=round(cost_usd, 6),
                tier_spent=round(usage.spent_usd, 4),
                tier_pct=round(usage.pct_used, 1),
            )
            return True

    def estimate_cost(self, tier: str, num_tokens: int) -> float:
        """
        Estimate cost for a call before making it.
        Uses same COST_PER_TOKEN table as LLMUsageTracker.
        """
        return num_tokens * self.COST_PER_TOKEN.get(tier, 0.0)

    def get_daily_report(self) -> DailyBudgetReport:
        """Return complete daily budget status snapshot."""
        self._ensure_today()
        total_spent = sum(u.spent_usd for u in self._usage.values())
        total_calls = sum(u.calls for u in self._usage.values())
        total_throttled = sum(u.throttled_calls for u in self._usage.values())

        return DailyBudgetReport(
            date=self._today_key,
            total_limit_usd=self._limits.get("total", 35.0),
            total_spent_usd=total_spent,
            total_calls=total_calls,
            total_throttled=total_throttled,
            tiers={tier: usage.to_dict() for tier, usage in self._usage.items()},
            overall_throttled=total_spent >= self._limits.get("total", 35.0),
        )

    def is_tier_available(self, tier: str) -> bool:
        """Quick non-async check: can this tier still be called today?"""
        self._ensure_today()
        usage = self._usage.get(tier)
        if not usage:
            return True
        return (
            usage.spent_usd < self._limits.get(tier, 999.0)
            and sum(u.spent_usd for u in self._usage.values())
            < self._limits.get("total", 999.0)
        )

    def reset_for_testing(self) -> None:
        """Reset all state. Used ONLY in tests."""
        self._today_key = ""
        self._usage = {}
        self._total_80_sent = False
        log_info(logger, "budget_governor_reset", reason="testing")

    # ── Private helpers ───────────────────────────────────────────────────────

    def _ensure_today(self) -> None:
        """Reset daily counters when the date changes."""
        today = str(date.today())
        if today != self._today_key:
            self._today_key = today
            self._usage = {}
            self._total_80_sent = False
            log_info(logger, "budget_day_rollover", new_date=today)

    def _get_or_create_tier(self, tier: str) -> TierUsage:
        if tier not in self._usage:
            self._usage[tier] = TierUsage(
                tier=tier,
                daily_limit_usd=self._limits.get(tier, 999.0),
            )
        return self._usage[tier]

    def _resolve_limits(
        self, override: Optional[Dict[str, float]]
    ) -> Dict[str, float]:
        """
        Resolve limits with priority: constructor > env vars > defaults.
        Env vars: BUDGET_LIMIT_BULK, BUDGET_LIMIT_OPS,
                  BUDGET_LIMIT_CREATIVE, BUDGET_LIMIT_STRATEGIC, BUDGET_LIMIT_TOTAL
        """
        limits = dict(self.DEFAULT_DAILY_LIMITS)

        # Environment variable overrides
        env_map = {
            "bulk":      "BUDGET_LIMIT_BULK",
            "ops":       "BUDGET_LIMIT_OPS",
            "creative":  "BUDGET_LIMIT_CREATIVE",
            "strategic": "BUDGET_LIMIT_STRATEGIC",
            "total":     "BUDGET_LIMIT_TOTAL",
        }
        for tier, env_key in env_map.items():
            val = os.getenv(env_key)
            if val:
                try:
                    limits[tier] = float(val)
                except ValueError:
                    logger.warning(f"Invalid env value for {env_key}: {val}")

        # Constructor overrides (highest priority)
        if override:
            limits.update(override)

        return limits

    async def _alert_warning_80(
        self, tier: str, spent: float, limit: float
    ) -> None:
        """Send non-blocking 80% warning to Slack."""
        msg = (
            f"⚠️ *LLM Budget Warning — 80% of daily limit reached*\n"
            f"Tier: `{tier}` | Spent: `${spent:.4f}` / `${limit:.2f}`\n"
            f"Remaining: `${limit - spent:.4f}` | "
            f"Date: `{self._today_key}`\n"
            f"_Review orchestrator cycle frequency if unexpected._"
        )
        await self._send_slack(msg, level="warning")

    async def _alert_hard_stop(
        self, tier: str, spent: float, limit: float, reason: str
    ) -> None:
        """Send blocking hard-stop alert to Slack."""
        msg = (
            f"🛑 *LLM Budget HARD STOP — calls blocked*\n"
            f"Tier: `{tier}` | Reason: `{reason}`\n"
            f"Spent: `${spent:.4f}` / `${limit:.2f}` (100%+ used)\n"
            f"Date: `{self._today_key}`\n"
            f"*All `{tier}` calls are REJECTED until midnight.*\n"
            f"To override: `POST /admin/budget/reset?tier={tier}`"
        )
        await self._send_slack(msg, level="critical")

    async def _alert_total_warning(self, spent: float, limit: float) -> None:
        """Send total daily budget warning."""
        msg = (
            f"⚠️ *Total Daily LLM Budget — 80% reached*\n"
            f"Total Spent: `${spent:.4f}` / `${limit:.2f}`\n"
            f"Date: `{self._today_key}`\n"
            f"Tier breakdown:\n"
            + "\n".join(
                f"  • `{tier}`: ${u.spent_usd:.4f} / ${u.daily_limit_usd:.2f} "
                f"({u.pct_used:.0f}%)"
                for tier, u in self._usage.items()
            )
        )
        await self._send_slack(msg, level="warning")

    async def _send_slack(self, message: str, level: str = "info") -> None:
        """Send Slack message safely (never raises)."""
        if not self._slack:
            return
        try:
            channel = "#alerts"
            if hasattr(self._slack, "notify_alert"):
                self._slack.notify_alert(message)
            elif hasattr(self._slack, "send_message"):
                self._slack.send_message(channel, message)
        except Exception as e:
            log_warning(logger, "budget_slack_failed", error=str(e))


# ─── Singleton ────────────────────────────────────────────────────────────────

_governor_instance: Optional[BudgetGovernor] = None


def get_budget_governor(
    slack=None,
    daily_limits: Optional[Dict[str, float]] = None,
) -> BudgetGovernor:
    """
    Get singleton BudgetGovernor instance.

    Called once at startup in main.py and injected into LLMRouter.
    """
    global _governor_instance
    if _governor_instance is None:
        _governor_instance = BudgetGovernor(
            slack=slack, daily_limits=daily_limits
        )
    return _governor_instance
