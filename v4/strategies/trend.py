"""TrendStrategy — Pullback entry in confirmed trends with trailing stop.

Only active in TREND_STRONG.
Enters on pullback to SMA20 with structural confirmation.

Key design decisions (from ChatGPT review):
- Requires higher_high/higher_low structure, not just candle patterns
- Trailing stop: min(1.5 * ATR, 1.2%) — absolute cap prevents giving back too much
- No pure time stop — closes on regime_confidence drop instead
- Max 2 entries per regime cycle
- Disables after 2 consecutive stops
- Won't enter if price already moved >2% since regime start (too late)
"""

from datetime import datetime, timezone, timedelta
from ..core.interfaces import IStrategy, Features, RegimeState, Position, GovernorDecision, Signal


class TrendStrategy(IStrategy):

    def __init__(self, config: dict):
        """
        Config keys:
            trend_size_pct: float — % of capital per trade (default 15)
            trailing_atr_k: float — trailing stop ATR multiplier (default 1.5)
            trailing_max_pct: float — absolute cap on trailing (default 1.2)
            max_entries_per_cycle: int (default 2)
            max_consecutive_stops: int (default 2)
            min_pullback_pct: float — minimum pullback to SMA for entry (default 0.2)
            max_late_entry_pct: float — don't enter if moved >X% since regime start (default 2.0)
        """
        self.trend_size_pct = config.get("trend_size_pct", 15)
        self.trailing_atr_k = config.get("trailing_atr_k", 1.5)
        self.trailing_max_pct = config.get("trailing_max_pct", 1.2)
        self.max_entries = config.get("max_entries_per_cycle", 2)
        self.max_stops = config.get("max_consecutive_stops", 2)
        self.min_pullback_pct = config.get("min_pullback_pct", 0.2)
        self.max_late_entry_pct = config.get("max_late_entry_pct", 2.0)

        # State per regime cycle
        self._entries_this_cycle = 0
        self._regime_start_price = 0.0
        self._last_regime = ""
        self._trailing_high = 0.0
        self._trailing_low = 999999.0
        self._candle_history = []  # recent closes for structure detection

    def name(self) -> str:
        return "TREND"

    def generate_signals(self, features: Features, regime: RegimeState,
                         position: Position, governor: GovernorDecision) -> list:
        signals = []
        price = features.sma_20 or 0
        if price <= 0:
            return signals

        # ===== REGIME CYCLE RESET =====
        if regime.current != self._last_regime:
            self._entries_this_cycle = 0
            self._regime_start_price = price
            self._last_regime = regime.current
            self._trailing_high = 0.0
            self._trailing_low = 999999.0

        # ===== DISABLED AFTER CONSECUTIVE STOPS =====
        if regime.consecutive_stops >= self.max_stops:
            # Close any position and wait
            if position.side != "flat" and position.size > 0:
                signals.append(self._close_signal(position, price, "Disabled after consecutive stops"))
            return signals

        # ===== CLOSE ON CONFIDENCE DROP =====
        # Instead of time stop, close when regime confidence drops
        if position.side != "flat" and regime.confidence < 0.5:
            signals.append(self._close_signal(position, price,
                                              f"Confidence dropped to {regime.confidence}"))
            return signals

        # ===== DETERMINE TREND DIRECTION =====
        is_bullish = features.sma_aligned_bullish or features.sma_slope_20 > 0.05
        is_bearish = features.sma_aligned_bearish or features.sma_slope_20 < -0.05

        if not is_bullish and not is_bearish:
            return signals

        # ===== MANAGE EXISTING POSITION =====
        if position.side != "flat" and position.size > 0:
            return self._manage_position(features, position, price, is_bullish)

        # ===== ENTRY LOGIC =====
        if self._entries_this_cycle >= self.max_entries:
            return signals  # max entries reached

        # Don't enter too late
        if self._regime_start_price > 0:
            moved_pct = abs(price - self._regime_start_price) / self._regime_start_price * 100
            if moved_pct > self.max_late_entry_pct:
                return signals

        # Check for pullback entry
        entry_signal = self._check_pullback_entry(features, price, is_bullish, is_bearish,
                                                  regime, governor)
        if entry_signal:
            signals.append(entry_signal)

        return signals

    def _check_pullback_entry(self, features: Features, price: float,
                              is_bullish: bool, is_bearish: bool,
                              regime: RegimeState, governor: GovernorDecision):
        """Check if we have a valid pullback entry.

        Requirements:
        1. Price pulled back to SMA20 (within min_pullback_pct)
        2. Structural confirmation: higher low (bullish) or lower high (bearish)
        3. RSI not extreme (not overbought for long, not oversold for short)
        """

        # 1. Pullback to SMA20
        distance_to_sma = features.price_vs_sma20_pct
        at_sma = abs(distance_to_sma) < self.min_pullback_pct + 0.5

        if not at_sma:
            return None

        # 2. Structural confirmation
        if is_bullish:
            # For long: price should be near SMA but above it (bouncing)
            # Higher low structure: momentum_1h > 0 (recovering from pullback)
            structure_ok = (distance_to_sma > -0.3 and
                           features.momentum_1h > 0 and
                           features.rsi > 40 and features.rsi < 70)
        elif is_bearish:
            # For short: price near SMA but below it
            structure_ok = (distance_to_sma < 0.3 and
                           features.momentum_1h < 0 and
                           features.rsi > 30 and features.rsi < 60)
        else:
            return None

        if not structure_ok:
            return None

        # 3. Calculate position size
        capital = 130.0  # will be passed from governor in real impl
        size_pct = self.trend_size_pct * (0.5 + 0.5 * regime.confidence)
        size_pct = min(size_pct, governor.max_exposure_pct / 2)  # never more than half of max exposure
        size_usdt = capital * size_pct / 100
        amount = round(size_usdt / price, 5) if price > 0 else 0

        if amount <= 0 or size_usdt < 10:
            return None

        # 4. Calculate stop loss
        stop_distance = self._calculate_stop_distance(features, price)
        if is_bullish:
            stop_price = round(price - stop_distance, 1)
        else:
            stop_price = round(price + stop_distance, 1)

        self._entries_this_cycle += 1

        side = "buy" if is_bullish else "sell"
        return Signal(
            side=side,
            price=round(price * (0.999 if is_bullish else 1.001), 1),
            amount=amount,
            order_type="limit",
            source="TREND",
            confidence=regime.confidence,
            metadata={
                "entry_type": "pullback",
                "stop_loss": stop_price,
                "trend_direction": "bullish" if is_bullish else "bearish",
                "entries_this_cycle": self._entries_this_cycle,
            },
        )

    def _manage_position(self, features: Features, position: Position,
                         price: float, is_bullish: bool) -> list:
        """Manage existing position with trailing stop.

        Trailing: min(1.5 * ATR, 1.2%) — absolute cap (ChatGPT recommendation).
        """
        signals = []

        # Update trailing
        if position.side == "long":
            if price > self._trailing_high or self._trailing_high == 0:
                self._trailing_high = price

            trail_distance = self._calculate_stop_distance(features, price)
            trailing_stop = self._trailing_high - trail_distance

            if price <= trailing_stop:
                signals.append(self._close_signal(
                    position, price,
                    f"Trailing stop hit: ${trailing_stop:,.0f} (high was ${self._trailing_high:,.0f})"
                ))

            # Wrong side: trend reversed
            if not is_bullish and features.sma_slope_20 < -0.05:
                signals.append(self._close_signal(
                    position, price, "Trend reversed — closing long"
                ))

        elif position.side == "short":
            if price < self._trailing_low or self._trailing_low == 999999:
                self._trailing_low = price

            trail_distance = self._calculate_stop_distance(features, price)
            trailing_stop = self._trailing_low + trail_distance

            if price >= trailing_stop:
                signals.append(self._close_signal(
                    position, price,
                    f"Trailing stop hit: ${trailing_stop:,.0f} (low was ${self._trailing_low:,.0f})"
                ))

            # Wrong side
            if is_bullish and features.sma_slope_20 > 0.05:
                signals.append(self._close_signal(
                    position, price, "Trend reversed — closing short"
                ))

        return signals

    def _calculate_stop_distance(self, features: Features, price: float) -> float:
        """Calculate stop distance: min(ATR * k, max_pct of price).

        ChatGPT recommendation: absolute cap prevents giving back too much in high vol.
        """
        atr_stop = features.atr * self.trailing_atr_k if features.atr > 0 else price * 0.01
        pct_stop = price * self.trailing_max_pct / 100

        return min(atr_stop, pct_stop)

    def _close_signal(self, position: Position, price: float, reason: str) -> Signal:
        return Signal(
            side="close",
            price=price,
            amount=position.size,
            order_type="market",
            reduce_only=True,
            source="TREND",
            confidence=1.0,
            metadata={"reason": reason},
        )

    def reset(self):
        """Reset on regime change."""
        self._entries_this_cycle = 0
        self._regime_start_price = 0.0
        self._trailing_high = 0.0
        self._trailing_low = 999999.0
