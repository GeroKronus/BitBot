"""RegimeAgent — Detects market regime with state machine, confidence, and anti-flip-flop.

States: RANGE, TREND_STRONG, TREND_WEAK, BREAKOUT, CHAOS

Incorporates:
- Debounce (2 consecutive detections to change)
- Anti flip-flop (max 4 changes/hour → force RANGE)
- Regime confidence (0-1) based on weighted voting with diverse inputs
- Time tracking per regime
- Breakout validation (SUSPECT → CONFIRMED/FAKE)
"""

from datetime import datetime, timezone, timedelta
from ..core.interfaces import IRegimeAgent, Features, RegimeState


class RegimeAgent(IRegimeAgent):

    RANGE = "RANGE"
    TREND_STRONG = "TREND_STRONG"
    TREND_WEAK = "TREND_WEAK"
    BREAKOUT = "BREAKOUT"
    CHAOS = "CHAOS"

    def __init__(self):
        self._pending_regime = None
        self._pending_count = 0
        self._change_timestamps = []  # for flip-flop detection
        self._breakout_start_price = 0.0
        self._breakout_start_time = None
        self.DEBOUNCE_REQUIRED = 2
        self.indicators = {}  # for logging/debugging

    def detect(self, features: Features, state: RegimeState) -> RegimeState:
        now = datetime.now(timezone.utc)
        new_state = RegimeState(
            current=state.current,
            previous=state.previous,
            confidence=state.confidence,
            time_in_regime_seconds=state.time_in_regime_seconds,
            regime_changes_1h=state.regime_changes_1h,
            last_change_at=state.last_change_at,
            strategy_disabled_until=state.strategy_disabled_until,
            consecutive_stops=state.consecutive_stops,
        )

        # Update time in regime
        if new_state.last_change_at:
            new_state.time_in_regime_seconds = (now - new_state.last_change_at).total_seconds()

        # Count changes in last hour (for anti flip-flop)
        self._change_timestamps = [
            t for t in self._change_timestamps
            if t > now - timedelta(hours=1)
        ]
        new_state.regime_changes_1h = len(self._change_timestamps)

        # Anti flip-flop: too many changes → force RANGE
        if new_state.regime_changes_1h >= 4:
            new_state.current = self.RANGE
            new_state.confidence = 0.3
            return new_state

        # Detect candidate regime
        candidate = self._classify(features, new_state)
        confidence = self._calculate_confidence(features, candidate)

        # Validate BREAKOUT (suspect until confirmed)
        if candidate == self.BREAKOUT:
            candidate, confidence = self._validate_breakout(features, now)

        # CHAOS: immediate, no debounce (safety first)
        if candidate == self.CHAOS:
            if candidate != new_state.current:
                new_state.previous = new_state.current
                new_state.last_change_at = now
                self._change_timestamps.append(now)
            new_state.current = self.CHAOS
            new_state.confidence = confidence
            self._pending_regime = None
            self._pending_count = 0
            return new_state

        # Debounce: require N consecutive detections
        # EMA smoothing on confidence (ChatGPT: prevents oscillation)
        alpha = 0.3
        confidence = round(alpha * confidence + (1 - alpha) * new_state.confidence, 2)

        if candidate != new_state.current:
            if candidate == self._pending_regime:
                self._pending_count += 1
                if self._pending_count >= self.DEBOUNCE_REQUIRED and confidence >= 0.6:
                    new_state.previous = new_state.current
                    new_state.current = candidate
                    new_state.confidence = confidence
                    new_state.last_change_at = now
                    new_state.time_in_regime_seconds = 0
                    self._change_timestamps.append(now)
                    self._pending_regime = None
                    self._pending_count = 0
            else:
                self._pending_regime = candidate
                self._pending_count = 1
        else:
            # Same regime — update confidence
            new_state.confidence = confidence
            self._pending_regime = None
            self._pending_count = 0

        return new_state

    def _classify(self, f: Features, state: RegimeState) -> str:
        """Classify regime: hybrid of original (state) + trend_score (event).

        Uses both approaches: original SMA-based for confirmed trends,
        trend_score for early detection. Best of both worlds.
        """
        # CHAOS
        if abs(f.speed_5m) > 2.0 or f.atr_pct > 3.5:
            return self.CHAOS
        if f.vol_acceleration > 50 and f.atr_pct > 2.0:
            return self.CHAOS

        # Compute trend_score for logging and early detection
        trend_score = self._compute_trend_score(f)
        self.indicators["trend_score"] = trend_score

        # BREAKOUT check
        if self._is_breakout(f, state):
            return self.BREAKOUT

        # TREND_STRONG: original SMA-based OR high trend_score
        if (abs(f.sma_slope_20) > 0.1 and
                abs(f.price_vs_sma20_pct) > 0.5 and
                (f.sma_aligned_bullish or f.sma_aligned_bearish) and
                f.bb_bandwidth_pct > 2.5):
            return self.TREND_STRONG

        # NEW: trend_score >= 4 also triggers TREND_STRONG (early detection)
        if trend_score >= 4:
            return self.TREND_STRONG

        # TREND_WEAK: original OR moderate trend_score
        if (abs(f.sma_slope_20) > 0.05 and abs(f.price_vs_sma20_pct) > 0.3):
            return self.TREND_WEAK
        if trend_score >= 3:
            return self.TREND_WEAK

        return self.RANGE

    def _compute_trend_score(self, f: Features) -> int:
        """Score-based trend detection: impulse + expansion + breakout.

        Auditor model:
        - Impulse (rate of change): captures START of movement
        - Expansion (ATR growing): confirms real trend, not noise
        - Breakout (structural): eliminates 80% false positives
        - Confirmation (momentum direction): alignment check
        """
        score = 0

        # Calibrated from real Hyperliquid BTC data (7 days, 168 candles)
        # Thresholds at p75 = top 25% of movements

        # 1. IMPULSE — rate of change (p75 thresholds)
        if abs(f.momentum_1h) > 0.45:   # p75 = 0.456%
            score += 1
        if abs(f.momentum_4h) > 0.9:    # p75 = 0.929%
            score += 1

        # 2. EXPANSION — volatility growing (24% of candles have this)
        if f.atr_expanding:
            score += 1

        # 3. BREAKOUT — price beyond Bollinger Bands (p90/p10)
        if f.bb_position_pct > 85 or f.bb_position_pct < 0:
            score += 2

        # 4. CONFIRMATION — momentum aligns with price vs SMA direction
        if (f.momentum_1h > 0 and f.price_vs_sma20_pct > 0) or \
           (f.momentum_1h < 0 and f.price_vs_sma20_pct < 0):
            score += 1

        return score

    def _is_breakout(self, f: Features, state: RegimeState) -> bool:
        """Detect potential breakout from range."""
        if state.current != self.RANGE:
            return False

        # BB breakout: price outside Bollinger Bands with volume
        bb_break = (f.bb_position_pct > 100 or f.bb_position_pct < 0)
        volume_confirm = f.volume_ratio > 1.3
        momentum = abs(f.momentum_1h) > 1.0

        if bb_break and (volume_confirm or momentum):
            if self._breakout_start_price == 0:
                self._breakout_start_price = f.sma_20 or f.bb_middle
                self._breakout_start_time = datetime.now(timezone.utc)
            return True

        return False

    def _validate_breakout(self, f: Features, now: datetime) -> tuple:
        """Validate breakout — require confirmations, invalidate fakes.

        Returns (regime, confidence) tuple.
        """
        confirmations = 0

        # 1. Volume > 1.5x average
        if f.volume_ratio > 1.5:
            confirmations += 1

        # 2. ATR expanding
        if f.atr_expanding:
            confirmations += 1

        # 3. Price still beyond breakout level (not just wick)
        if self._breakout_start_price > 0:
            move_pct = abs(f.sma_20 - self._breakout_start_price) / self._breakout_start_price * 100
            if move_pct > 0.5:  # minimum distance after breakout
                confirmations += 1

        # 4. Momentum confirms direction
        if abs(f.momentum_1h) > 0.8:
            confirmations += 1

        # Timeout: 15 min without confirmation → FAKE
        if self._breakout_start_time:
            elapsed = (now - self._breakout_start_time).total_seconds()
            if elapsed > 900 and confirmations < 3:  # 15 min
                self._breakout_start_price = 0
                self._breakout_start_time = None
                return (self.RANGE, 0.6)

        if confirmations >= 3:
            # Confirmed breakout → will transition to TREND_STRONG
            confidence = min(confirmations / 4, 1.0)
            self._breakout_start_price = 0
            self._breakout_start_time = None
            return (self.TREND_STRONG, confidence)

        # Still suspect — stay in current regime but flag
        return (self.RANGE, 0.4)

    def _calculate_confidence(self, f: Features, regime: str) -> float:
        """Calculate regime confidence using diverse inputs (not just price-derived).

        Uses: slope (trend), BB width (volatility), RSI (momentum),
              volume ratio (liquidity), funding rate proxy via speed.
        Avoids double-counting by weighting different signal types.
        """
        scores = []

        if regime in (self.TREND_STRONG, self.TREND_WEAK):
            # Trend confidence
            scores.append(min(abs(f.sma_slope_20) / 0.2, 1.0) * 2.5)        # slope (weight 2.5)
            scores.append(min(abs(f.price_vs_sma20_pct) / 1.5, 1.0) * 1.5)   # distance (weight 1.5)
            scores.append(min(abs(f.rsi - 50) / 25, 1.0) * 1.5)              # RSI extremity (weight 1.5)
            scores.append(min(f.volume_ratio / 1.5, 1.0) * 1.5)              # volume (weight 1.5)
            scores.append((1.0 if f.atr_expanding else 0.3) * 1.0)           # vol expanding (weight 1.0)
            total_weight = 2.5 + 1.5 + 1.5 + 1.5 + 1.0

        elif regime == self.RANGE:
            # Range confidence
            scores.append((1.0 - min(abs(f.sma_slope_20) / 0.1, 1.0)) * 2.5)
            scores.append((1.0 - min(abs(f.price_vs_sma20_pct) / 1.0, 1.0)) * 1.5)
            scores.append((1.0 - min(abs(f.rsi - 50) / 20, 1.0)) * 1.5)
            scores.append((1.0 if f.bb_bandwidth_pct < 3.0 else 0.3) * 1.0)
            total_weight = 2.5 + 1.5 + 1.5 + 1.0

        elif regime == self.CHAOS:
            scores.append(min(abs(f.speed_5m) / 3.0, 1.0) * 3.0)
            scores.append(min(f.atr_pct / 4.0, 1.0) * 2.0)
            total_weight = 5.0

        else:
            return 0.5

        raw = sum(scores) / total_weight if total_weight > 0 else 0.5
        return round(max(0.1, min(1.0, raw)), 2)


class PositionCore:
    """Single source of truth for position — always reads from exchange."""

    def __init__(self, market_data):
        self._market_data = market_data
        self._cached = None

    def sync(self):
        """Sync with exchange."""
        from ..core.interfaces import Position
        raw = self._market_data.get_position()
        pos = Position(
            side=raw.get("side", "flat"),
            size=raw.get("size", 0),
            entry_price=raw.get("entry_price", 0),
            unrealized_pnl=raw.get("unrealized_pnl", 0),
            notional=raw.get("notional", 0),
            leverage=raw.get("leverage", 1),
        )
        if pos.side != "flat" and pos.open_since is None:
            pos.open_since = datetime.now(timezone.utc)
        self._cached = pos
        return pos

    def get(self):
        """Get cached position (no API call)."""
        if self._cached is None:
            return self.sync()
        return self._cached
