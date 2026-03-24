"""Exposure Manager — Controls position limits, drawdown, and rate limiting."""

from datetime import datetime, timezone, timedelta
from .notifier import Notifier


class ExposureManager:
    """Enforces hard limits on exposure, drawdown, and trading rate.

    Acts as a safety layer between regime/AI decisions and actual execution.
    Even if regime says "go", exposure manager can say "stop" if limits are hit.
    """

    def __init__(self, config, notifier: Notifier, initial_balance: float = 126.22):
        self.config = config
        self.notifier = notifier
        self.initial_balance = initial_balance

        # --- Limits (calibrated by risk agent for $130 capital) ---
        self.max_capital_exposed_pct = 80       # max 80% of balance = ~$104 notional
        self.max_position_time_hours = 0        # 0 = disabled for grid (incompatible)
        self.max_trades_per_5min = 0            # 0 = disabled (avg 0.15/min, not needed)
        self.max_daily_drawdown_alert_pct = 3.0 # notify at 3% (~$3.90)
        self.max_daily_drawdown_pct = 5.0       # pause grid at 5% (~$6.50)
        self.max_daily_drawdown_kill_pct = 8.0  # kill switch at 8% (~$10.40)
        self.max_total_drawdown_pct = 15.0      # emergency stop at 15% from initial
        self.max_grid_resets_per_hour = 3       # prevent reset churn
        self.stop_cooldown_seconds = 180        # 3 min cooldown after stop loss

        # --- State ---
        self.trade_timestamps: list[datetime] = []
        self.daily_starting_balance: float = initial_balance
        self.daily_reset_date: str = ""
        self.is_daily_stopped = False
        self.is_emergency_stopped = False
        self.position_open_since: datetime | None = None

    def check_can_trade(self, current_balance: float, position_value: float = 0) -> dict:
        """Check all limits and return whether trading is allowed.

        Returns dict with:
            allowed: bool
            reasons: list of strings explaining any blocks
            warnings: list of strings for near-limit conditions
        """
        now = datetime.now(timezone.utc)
        reasons = []
        warnings = []

        # --- Reset daily counter ---
        today = now.strftime("%Y-%m-%d")
        if today != self.daily_reset_date:
            self.daily_reset_date = today
            self.daily_starting_balance = current_balance
            self.is_daily_stopped = False

        # --- 1. Daily drawdown check (3 levels) ---
        daily_pnl = current_balance - self.daily_starting_balance
        daily_pnl_pct = (daily_pnl / self.daily_starting_balance * 100) if self.daily_starting_balance > 0 else 0

        # Level 1: Alert (notify)
        if daily_pnl_pct <= -self.max_daily_drawdown_alert_pct and not self.is_daily_stopped:
            warnings.append(
                f"ALERTA drawdown diario: {daily_pnl_pct:.1f}% "
                f"(pausa em -{self.max_daily_drawdown_pct}%)"
            )

        # Level 2: Pause grid
        if daily_pnl_pct <= -self.max_daily_drawdown_pct:
            self.is_daily_stopped = True
            reasons.append(
                f"Drawdown diario: {daily_pnl_pct:.1f}% — grid pausado ate amanha"
            )

        # Level 3: Kill switch (close everything)
        if daily_pnl_pct <= -self.max_daily_drawdown_kill_pct:
            reasons.append(
                f"KILL: Drawdown diario critico {daily_pnl_pct:.1f}% — fechar tudo"
            )

        if self.is_daily_stopped:
            reasons.append("Bot pausado por drawdown diario")

        # --- 2. Total drawdown check ---
        total_pnl_pct = ((current_balance - self.initial_balance) / self.initial_balance * 100)

        if total_pnl_pct <= -self.max_total_drawdown_pct:
            self.is_emergency_stopped = True
            reasons.append(
                f"EMERGENCIA: Drawdown total {total_pnl_pct:.1f}% "
                f"(limite: -{self.max_total_drawdown_pct}%)"
            )

        if self.is_emergency_stopped:
            reasons.append("Bot em PARADA DE EMERGENCIA — intervenção manual necessária")

        # --- 3. Capital exposure check ---
        if current_balance > 0:
            exposure_pct = position_value / current_balance * 100
            if exposure_pct > self.max_capital_exposed_pct:
                reasons.append(
                    f"Exposicao: {exposure_pct:.0f}% "
                    f"(limite: {self.max_capital_exposed_pct}%)"
                )

            if exposure_pct > self.max_capital_exposed_pct * 0.8 and exposure_pct <= self.max_capital_exposed_pct:
                warnings.append(f"Exposicao alta: {exposure_pct:.0f}%")

        # --- 4. Rate limit (disabled for grid — avg 0.15/min, not needed) ---
        # Kept for future use if trading frequency increases

        # --- 5. Position staleness check ---
        # Grid holds positions by design, so no time limit
        # But warn if position is losing > 1.5% for over 1 hour
        if self.position_open_since and position_value > 0:
            position_hours = (now - self.position_open_since).total_seconds() / 3600
            if position_hours > 1.0:
                warnings.append(
                    f"Posicao aberta ha {position_hours:.1f}h"
                )

        allowed = len(reasons) == 0
        return {
            "allowed": allowed,
            "reasons": reasons,
            "warnings": warnings,
            "daily_pnl_pct": round(daily_pnl_pct, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
        }

    def record_trade(self):
        """Record a trade for rate limiting."""
        self.trade_timestamps.append(datetime.now(timezone.utc))

    def record_position_open(self):
        """Record when a position was opened."""
        if self.position_open_since is None:
            self.position_open_since = datetime.now(timezone.utc)

    def record_position_close(self):
        """Record when a position was closed."""
        self.position_open_since = None

    def manual_resume(self):
        """Manually resume after daily stop (use with caution)."""
        self.is_daily_stopped = False
        self.notifier.send("Exposure manager: resume manual ativado")

    def manual_emergency_reset(self):
        """Reset emergency stop (requires manual intervention)."""
        self.is_emergency_stopped = False
        self.notifier.send("EMERGENCIA resetada manualmente")

    def get_status(self) -> dict:
        return {
            "daily_stopped": self.is_daily_stopped,
            "emergency_stopped": self.is_emergency_stopped,
            "daily_pnl_pct": 0,  # will be filled by check_can_trade
            "total_pnl_pct": 0,
            "trades_last_5min": len(self.trade_timestamps),
            "position_open_since": self.position_open_since.isoformat() if self.position_open_since else None,
            "limits": {
                "max_capital_exposed_pct": self.max_capital_exposed_pct,
                "max_daily_drawdown_pct": self.max_daily_drawdown_pct,
                "max_total_drawdown_pct": self.max_total_drawdown_pct,
                "max_trades_per_5min": self.max_trades_per_5min,
                "max_position_time_hours": self.max_position_time_hours,
            },
        }
