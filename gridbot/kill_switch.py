"""Kill Switch — Emergency protection for extreme market events."""

from datetime import datetime, timezone, timedelta
from .notifier import Notifier


class KillSwitch:
    """Monitors for extreme market conditions and triggers emergency shutdown.

    Hierarchy: Kill Switch > Regime > AI > Grid
    When triggered, overrides everything and closes all positions.
    """

    def __init__(self, config, notifier: Notifier):
        self.config = config
        self.notifier = notifier
        self.price_history: list[tuple[datetime, float]] = []
        self.cooldown_until: object = None
        self.trigger_count = 0
        self.last_trigger_reason = ""

        # Thresholds (configurable)
        self.flash_move_pct = 2.5          # % move in window that triggers
        self.flash_window_minutes = 15      # time window for flash detection
        self.atr_critical_pct = 3.0         # ATR as % of price that's critical
        self.cooldown_base_minutes = 15     # base cooldown after trigger
        self.max_cooldown_minutes = 60      # max cooldown (escalating)

    def update_price(self, price: float, timestamp: datetime = None):
        """Record price for flash move detection."""
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        self.price_history.append((timestamp, price))

        # Keep only last 30 minutes of data
        cutoff = timestamp - timedelta(minutes=30)
        self.price_history = [(t, p) for t, p in self.price_history if t > cutoff]

    def check(self, current_price: float, atr: float = 0) :
        """Check if kill switch should trigger.

        Returns None if safe, or dict with trigger details if triggered.
        """
        now = datetime.now(timezone.utc)

        # If cooling down, don't re-trigger but report status
        if self.is_cooling_down():
            return None

        triggers = []

        # 1. Flash move detection: price moved > X% in Y minutes
        #    Requires 3 consecutive ticks confirming (avoid wick false positives)
        if len(self.price_history) >= 3:
            window_start = now - timedelta(minutes=self.flash_window_minutes)
            window_prices = [p for t, p in self.price_history if t >= window_start]

            if window_prices:
                oldest = window_prices[0]
                move_pct = abs(current_price - oldest) / oldest * 100

                # Confirm with last 3 prices (15 seconds at 5s tick)
                if len(self.price_history) >= 3:
                    last_3 = [p for _, p in self.price_history[-3:]]
                    all_confirm = all(
                        abs(p - oldest) / oldest * 100 > self.flash_move_pct * 0.8
                        for p in last_3
                    )
                else:
                    all_confirm = False

                if move_pct > self.flash_move_pct and all_confirm:
                    direction = "UP" if current_price > oldest else "DOWN"
                    triggers.append(
                        f"Flash {direction}: {move_pct:.1f}% em {self.flash_window_minutes}min "
                        f"(${oldest:,.0f} → ${current_price:,.0f}) [confirmado 3 ticks]"
                    )

        # 1b. Data gap detection (exchange down or API failure)
        if len(self.price_history) >= 2:
            prev_price = self.price_history[-2][1]
            gap_pct = abs(current_price - prev_price) / prev_price * 100
            if gap_pct > 8.0:
                # Likely data issue, not real move — don't trigger kill
                return None

        # 2. ATR critical: volatility too high
        if atr > 0:
            atr_pct = atr / current_price * 100
            if atr_pct > self.atr_critical_pct:
                triggers.append(f"ATR critico: {atr_pct:.1f}% (threshold: {self.atr_critical_pct}%)")

        # 3. Rapid succession: multiple small moves (choppy dangerous market)
        if len(self.price_history) >= 6:
            recent = [p for _, p in self.price_history[-6:]]
            reversals = 0
            for i in range(2, len(recent)):
                if (recent[i] - recent[i-1]) * (recent[i-1] - recent[i-2]) < 0:
                    reversals += 1
            if reversals >= 4:
                max_swing = max(recent) - min(recent)
                swing_pct = max_swing / min(recent) * 100
                if swing_pct > 1.5:
                    triggers.append(f"Mercado erratico: {reversals} reversoes, swing {swing_pct:.1f}%")

        if triggers:
            return self._trigger(triggers, now)

        return None

    def _trigger(self, reasons: list[str], now: datetime) -> dict:
        """Execute kill switch trigger."""
        self.trigger_count += 1

        # Escalating cooldown: 15min first time, 30min second, up to 60min
        cooldown_minutes = min(
            self.cooldown_base_minutes * self.trigger_count,
            self.max_cooldown_minutes
        )
        self.cooldown_until = now + timedelta(minutes=cooldown_minutes)
        self.last_trigger_reason = " | ".join(reasons)

        result = {
            "triggered": True,
            "reasons": reasons,
            "cooldown_minutes": cooldown_minutes,
            "cooldown_until": self.cooldown_until.isoformat(),
            "trigger_count": self.trigger_count,
            "action": "CLOSE_ALL_AND_PAUSE",
        }

        # Notify
        msg = (
            f"KILL SWITCH ATIVADO!\n"
            f"Motivo: {self.last_trigger_reason}\n"
            f"Acao: Fechar todas posicoes e pausar\n"
            f"Cooldown: {cooldown_minutes} minutos\n"
            f"Triggers hoje: {self.trigger_count}"
        )
        self.notifier.send(msg)

        return result

    def is_cooling_down(self) -> bool:
        """Check if bot should remain paused after trigger."""
        if self.cooldown_until is None:
            return False
        now = datetime.now(timezone.utc)
        if now >= self.cooldown_until:
            self.cooldown_until = None
            return False
        return True

    def get_cooldown_remaining(self) -> str:
        """Get remaining cooldown time as human-readable string."""
        if not self.is_cooling_down():
            return "none"
        remaining = self.cooldown_until - datetime.now(timezone.utc)
        minutes = int(remaining.total_seconds() // 60)
        seconds = int(remaining.total_seconds() % 60)
        return f"{minutes}m {seconds}s"

    def reset_trigger_count(self):
        """Reset escalation counter (call daily or on manual resume)."""
        self.trigger_count = 0

    def get_status(self) -> dict:
        return {
            "cooling_down": self.is_cooling_down(),
            "cooldown_remaining": self.get_cooldown_remaining(),
            "trigger_count": self.trigger_count,
            "last_trigger_reason": self.last_trigger_reason,
            "price_history_len": len(self.price_history),
            "thresholds": {
                "flash_move_pct": self.flash_move_pct,
                "flash_window_minutes": self.flash_window_minutes,
                "atr_critical_pct": self.atr_critical_pct,
            },
        }
