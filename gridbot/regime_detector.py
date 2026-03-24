"""Regime Detector — Classifies market state for adaptive grid behavior."""

import math
from datetime import datetime, timezone


class RegimeDetector:
    """Detects market regime from price data and technical indicators.

    Regimes:
        RANGE       — Lateral market, grid performs best
        TREND_UP    — Bullish trend, reduce shorts
        TREND_DOWN  — Bearish trend, reduce longs
        ANOMALY     — Extreme volatility, kill switch territory

    Uses: price slope, ATR, distance from SMA, speed of movement, BB bandwidth.
    Does NOT use ADX (too slow, requires OHLC data).
    Runs every tick using cached data — zero API calls.
    AI serves as conservative override only (can make more cautious, never more aggressive).
    """

    # Regime constants
    RANGE = "RANGE"
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    ANOMALY = "ANOMALY"

    # Execution rules per regime
    REGIME_RULES = {
        "RANGE": {
            "grid_active": True,
            "order_size_pct": 100,
            "max_levels": 5,
            "max_leverage": 4,
            "spacing_multiplier": 1.0,
            "new_entries": "all",       # both long and short
            "description": "Mercado lateral — grid pleno",
        },
        "TREND_UP": {
            "grid_active": True,
            "order_size_pct": 60,
            "max_levels": 3,
            "max_leverage": 2,
            "spacing_multiplier": 1.5,   # wider spacing in trend
            "new_entries": "long_only",
            "description": "Tendencia de alta — reduzir shorts",
        },
        "TREND_DOWN": {
            "grid_active": True,
            "order_size_pct": 60,
            "max_levels": 3,
            "max_leverage": 2,
            "spacing_multiplier": 1.5,
            "new_entries": "short_only",
            "description": "Tendencia de baixa — reduzir longs",
        },
        "ANOMALY": {
            "grid_active": False,
            "order_size_pct": 0,
            "max_levels": 0,
            "max_leverage": 1,
            "spacing_multiplier": 2.0,
            "new_entries": "none",
            "description": "Evento anomalo — kill switch",
        },
    }

    def __init__(self):
        self.current_regime = self.RANGE
        self.regime_since = datetime.now(timezone.utc)
        self.confidence = 0.0
        self.indicators = {}
        self._pending_regime = None
        self._pending_count = 0
        self.DEBOUNCE_REQUIRED = 2  # need 2 consecutive detections to change

    def detect(self, prices: list[float], atr: float = 0,
               sma_20: float = 0, sma_50: float = 0) -> str:
        """Detect current market regime.

        Args:
            prices: List of recent prices (at least 20, ideally 50+).
                    Can be hourly candle closes.
            atr: Average True Range (14-period)
            sma_20: 20-period simple moving average
            sma_50: 50-period simple moving average

        Returns:
            Regime string: RANGE, TREND_UP, TREND_DOWN, or ANOMALY
        """
        if len(prices) < 10:
            return self.current_regime

        current = prices[-1]

        # --- Compute indicators ---

        # 1. Speed: % change in last 5 candles (~5 hours if hourly)
        speed = (current - prices[-6]) / prices[-6] * 100 if len(prices) > 5 else 0

        # 2. ATR as % of price (volatility measure)
        atr_pct = (atr / current * 100) if atr > 0 and current > 0 else 0

        # 3. Price slope: linear regression slope of last 20 prices
        slope = self._calculate_slope(prices[-20:]) if len(prices) >= 20 else 0
        slope_pct = slope / current * 100  # normalize as % per candle

        # 4. Distance from SMA20
        dist_sma20 = ((current - sma_20) / sma_20 * 100) if sma_20 > 0 else 0

        # 5. Distance from SMA50
        dist_sma50 = ((current - sma_50) / sma_50 * 100) if sma_50 > 0 else 0

        # 6. SMA alignment (trend confirmation)
        sma_bullish = sma_20 > sma_50 > 0
        sma_bearish = 0 < sma_20 < sma_50

        # 7. Price range compression (Bollinger-like)
        if len(prices) >= 20:
            recent = prices[-20:]
            price_range = (max(recent) - min(recent)) / min(recent) * 100
        else:
            price_range = 0

        # Store for debugging/logging
        self.indicators = {
            "speed": round(speed, 2),
            "atr_pct": round(atr_pct, 2),
            "slope_pct": round(slope_pct, 4),
            "dist_sma20": round(dist_sma20, 2),
            "dist_sma50": round(dist_sma50, 2),
            "sma_bullish": sma_bullish,
            "sma_bearish": sma_bearish,
            "price_range_20": round(price_range, 2),
        }

        # --- Classification logic ---

        # ANOMALY: extreme speed or ATR
        if abs(speed) > 3.0 or atr_pct > 3.5:
            new_regime = self.ANOMALY
            self.confidence = min(abs(speed) / 3.0, 1.0)

        # TREND_UP: positive slope + price above SMAs + bullish alignment
        elif (slope_pct > 0.05 and dist_sma20 > 0.5 and
              (sma_bullish or dist_sma50 > 1.0)):
            new_regime = self.TREND_UP
            self.confidence = min(abs(slope_pct) / 0.15, 1.0)

        # TREND_DOWN: negative slope + price below SMAs + bearish alignment
        elif (slope_pct < -0.05 and dist_sma20 < -0.5 and
              (sma_bearish or dist_sma50 < -1.0)):
            new_regime = self.TREND_DOWN
            self.confidence = min(abs(slope_pct) / 0.15, 1.0)

        # RANGE: low slope + price near SMA + compressed range
        else:
            new_regime = self.RANGE
            self.confidence = 1.0 - min(abs(slope_pct) / 0.1, 0.8)

        # Debounce: require N consecutive detections to change regime
        # Exception: ANOMALY triggers immediately (safety first)
        if new_regime != self.current_regime:
            if new_regime == self.ANOMALY:
                self._pending_regime = None
                self._pending_count = 0
                self.current_regime = new_regime
                self.regime_since = datetime.now(timezone.utc)
            elif new_regime == self._pending_regime:
                self._pending_count += 1
                if self._pending_count >= self.DEBOUNCE_REQUIRED:
                    self.current_regime = new_regime
                    self.regime_since = datetime.now(timezone.utc)
                    self._pending_regime = None
                    self._pending_count = 0
            else:
                self._pending_regime = new_regime
                self._pending_count = 1
        else:
            self._pending_regime = None
            self._pending_count = 0

        return self.current_regime

    def get_rules(self) -> dict:
        """Get execution rules for current regime."""
        return self.REGIME_RULES.get(self.current_regime, self.REGIME_RULES[self.RANGE])

    def apply_ai_filter(self, ai_outlook: str, rules: dict) -> dict:
        """Modify regime rules based on AI outlook.

        AI is a FILTER, not an executor. It adjusts parameters within
        the boundaries set by the regime.

        Hierarchy: Kill Switch > Regime > AI > Grid

        CRITICAL RULES:
        - AI can only make things MORE conservative, never more aggressive
        - Max bias: 60/40 (never more than 60% in one direction)
        - In ANOMALY, AI has zero influence
        """
        filtered = dict(rules)
        MAX_BIAS = 0.6  # never more than 60% in one direction

        if self.current_regime == self.RANGE:
            if ai_outlook == "bullish":
                filtered["buy_bias"] = MAX_BIAS
            elif ai_outlook == "bearish":
                filtered["buy_bias"] = 1.0 - MAX_BIAS
            else:
                filtered["buy_bias"] = 0.5

        elif self.current_regime == self.TREND_UP:
            if ai_outlook == "bearish":
                # AI disagrees with trend → maximum caution
                filtered["order_size_pct"] = 30
                filtered["max_levels"] = 2
                filtered["buy_bias"] = 0.5
            elif ai_outlook == "bullish":
                # AI confirms trend → slightly more confidence (still capped)
                filtered["order_size_pct"] = 75
                filtered["buy_bias"] = MAX_BIAS
            else:
                filtered["buy_bias"] = 0.55

        elif self.current_regime == self.TREND_DOWN:
            if ai_outlook == "bullish":
                # AI disagrees with trend → maximum caution
                filtered["order_size_pct"] = 30
                filtered["max_levels"] = 2
                filtered["buy_bias"] = 0.5
            elif ai_outlook == "bearish":
                # AI confirms trend (capped)
                filtered["order_size_pct"] = 75
                filtered["buy_bias"] = 1.0 - MAX_BIAS
            else:
                filtered["buy_bias"] = 0.45

        elif self.current_regime == self.ANOMALY:
            # In anomaly, AI has ZERO influence. Kill switch rules.
            filtered["buy_bias"] = 0.5

        return filtered

    def _calculate_slope(self, prices: list[float]) -> float:
        """Simple linear regression slope."""
        n = len(prices)
        if n < 2:
            return 0.0
        x_mean = (n - 1) / 2
        y_mean = sum(prices) / n
        numerator = sum((i - x_mean) * (p - y_mean) for i, p in enumerate(prices))
        denominator = sum((i - x_mean) ** 2 for i in range(n))
        if denominator == 0:
            return 0.0
        return numerator / denominator

    def get_status(self) -> dict:
        regime_duration = datetime.now(timezone.utc) - self.regime_since
        return {
            "regime": self.current_regime,
            "confidence": round(self.confidence, 2),
            "since": self.regime_since.isoformat(),
            "duration": str(regime_duration).split(".")[0],
            "rules": self.get_rules(),
            "indicators": self.indicators,
        }
