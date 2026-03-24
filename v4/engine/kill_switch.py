"""Kill Switch v4 — Predictive, not just reactive.

Detects the BEGINNING of chaos, not just chaos itself.

Triggers:
1. Flash move: >2.5% in 15 min (confirmed 2 ticks)
2. ATR critical: >3.5% of price
3. Volatility ACCELERATION: ATR expanding rapidly (predictive)
4. Price acceleration + volume spike (predictive)
5. Data gap: no price for >30 seconds (API issue while market moves)

Actions:
- Cancel all orders
- Close all positions
- Cooldown: 15-60 min (escalating)

Improvements over v3:
- Reduced confirmation to 2 ticks (was 3) per ChatGPT
- Added predictive triggers (vol acceleration, price acceleration)
- Data staleness detection
"""

from datetime import datetime, timezone, timedelta


class KillSwitch:

    def __init__(self):
        self.price_history = []  # (timestamp, price)
        self.cooldown_until = None
        self.trigger_count = 0
        self.last_trigger_reason = ""
        self.last_price_time = None

        # Thresholds
        self.flash_move_pct = 2.5
        self.flash_window_minutes = 15
        self.atr_critical_pct = 3.5
        self.vol_accel_threshold = 40  # % increase in ATR
        self.cooldown_base_minutes = 15
        self.max_cooldown_minutes = 60
        self.stale_price_seconds = 30

    def update(self, price: float, features=None):
        """Update price and check triggers. Returns trigger dict or None."""
        now = datetime.now(timezone.utc)
        self.price_history.append((now, price))
        self.last_price_time = now

        # Keep 30 min of data
        cutoff = now - timedelta(minutes=30)
        self.price_history = [(t, p) for t, p in self.price_history if t > cutoff]

        if self.is_cooling_down():
            return None

        triggers = []

        # 1. Flash move (2 tick confirmation)
        self._check_flash_move(price, now, triggers)

        # 2. ATR critical
        if features and features.atr_pct > self.atr_critical_pct:
            triggers.append(f"ATR critico: {features.atr_pct:.1f}%")

        # 3. PREDICTIVE: Volatility acceleration
        if features and features.vol_acceleration > self.vol_accel_threshold and features.atr_pct > 2.0:
            triggers.append(f"Vol acelerando: +{features.vol_acceleration:.0f}% (preditivo)")

        # 4. PREDICTIVE: Price acceleration + momentum
        if features and abs(features.price_acceleration) > 0.05 and abs(features.speed_5m) > 1.5:
            triggers.append(f"Preco acelerando: {features.price_acceleration:.3f} + speed {features.speed_5m:.1f}%")

        # 5. Data gap detection
        gap = self._check_data_gap(now)
        if gap:
            triggers.append(gap)

        if triggers:
            return self._trigger(triggers, now)

        return None

    def _check_flash_move(self, price: float, now: datetime, triggers: list):
        """Check for flash move with 2-tick confirmation."""
        if len(self.price_history) < 2:
            return

        window_start = now - timedelta(minutes=self.flash_window_minutes)
        window_prices = [p for t, p in self.price_history if t >= window_start]

        if not window_prices:
            return

        oldest = window_prices[0]
        move_pct = abs(price - oldest) / oldest * 100

        if move_pct <= self.flash_move_pct:
            return

        # Data gap check: don't trigger on API glitch
        if len(self.price_history) >= 2:
            prev = self.price_history[-2][1]
            gap = abs(price - prev) / prev * 100
            if gap > 8.0:
                return  # likely data issue

        # 2 tick confirmation
        if len(self.price_history) >= 2:
            last_2 = [p for _, p in self.price_history[-2:]]
            confirmed = all(
                abs(p - oldest) / oldest * 100 > self.flash_move_pct * 0.8
                for p in last_2
            )
            if confirmed:
                direction = "UP" if price > oldest else "DOWN"
                triggers.append(
                    f"Flash {direction}: {move_pct:.1f}% em {self.flash_window_minutes}min "
                    f"(${oldest:,.0f} -> ${price:,.0f})"
                )

    def _check_data_gap(self, now: datetime):
        """Detect if we haven't received price data recently."""
        if len(self.price_history) >= 2:
            prev_time = self.price_history[-2][0]
            gap_seconds = (now - prev_time).total_seconds()
            if gap_seconds > self.stale_price_seconds:
                return f"Data gap: {gap_seconds:.0f}s sem preco"
        return None

    def _trigger(self, reasons: list, now: datetime) -> dict:
        self.trigger_count += 1
        cooldown_min = min(
            self.cooldown_base_minutes * self.trigger_count,
            self.max_cooldown_minutes
        )
        self.cooldown_until = now + timedelta(minutes=cooldown_min)
        self.last_trigger_reason = " | ".join(reasons)

        return {
            "triggered": True,
            "reasons": reasons,
            "cooldown_minutes": cooldown_min,
            "cooldown_until": self.cooldown_until.isoformat(),
            "trigger_count": self.trigger_count,
        }

    def is_cooling_down(self) -> bool:
        if self.cooldown_until is None:
            return False
        if datetime.now(timezone.utc) >= self.cooldown_until:
            self.cooldown_until = None
            return False
        return True

    def get_cooldown_remaining(self) -> str:
        if not self.is_cooling_down():
            return "none"
        remaining = self.cooldown_until - datetime.now(timezone.utc)
        return f"{int(remaining.total_seconds() // 60)}m {int(remaining.total_seconds() % 60)}s"

    def get_status(self) -> dict:
        return {
            "cooling_down": self.is_cooling_down(),
            "cooldown_remaining": self.get_cooldown_remaining(),
            "trigger_count": self.trigger_count,
            "last_reason": self.last_trigger_reason,
        }
