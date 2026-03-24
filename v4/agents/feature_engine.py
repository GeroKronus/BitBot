"""FeatureEngine — Computes all technical features from raw market data.

Uses OHLCV candles for proper True Range calculation (not close-to-close approximation).
Zero API calls — pure computation on data already fetched.
"""

import math
from ..core.interfaces import IFeatureEngine, MarketSnapshot, Features


class FeatureEngine(IFeatureEngine):
    """Computes: ATR, RSI, SMA, BB, momentum, acceleration, liquidity score."""

    def compute(self, snapshot: MarketSnapshot, candles: list[dict]) -> Features:
        f = Features()

        if not candles or len(candles) < 14:
            f.spread_pct = (snapshot.spread / snapshot.price * 100) if snapshot.price > 0 else 0
            return f

        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        volumes = [c.get("volume", 0) for c in candles]
        price = snapshot.price or closes[-1]

        # ============ TREND ============

        # SMA 20
        if len(closes) >= 20:
            f.sma_20 = round(sum(closes[-20:]) / 20, 2)
            # SMA slope: compare SMA now vs 3 candles ago
            sma_3_ago = sum(closes[-23:-3]) / 20 if len(closes) >= 23 else f.sma_20
            f.sma_slope_20 = round((f.sma_20 - sma_3_ago) / sma_3_ago * 100, 4) if sma_3_ago > 0 else 0

        # SMA 50
        if len(closes) >= 50:
            f.sma_50 = round(sum(closes[-50:]) / 50, 2)
        elif len(closes) >= 20:
            f.sma_50 = round(sum(closes[-len(closes):]) / len(closes), 2)

        # Price vs SMAs
        if f.sma_20 > 0:
            f.price_vs_sma20_pct = round((price - f.sma_20) / f.sma_20 * 100, 2)
        if f.sma_50 > 0:
            f.price_vs_sma50_pct = round((price - f.sma_50) / f.sma_50 * 100, 2)

        # SMA alignment
        f.sma_aligned_bullish = f.sma_20 > f.sma_50 > 0
        f.sma_aligned_bearish = 0 < f.sma_20 < f.sma_50

        # ============ VOLATILITY ============

        # ATR (14) — proper True Range using High/Low/Close
        if len(candles) >= 15:
            true_ranges = []
            for i in range(-14, 0):
                h = highs[i]
                l = lows[i]
                prev_c = closes[i - 1]
                tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
                true_ranges.append(tr)
            f.atr = round(sum(true_ranges) / len(true_ranges), 2)
            f.atr_pct = round(f.atr / price * 100, 3) if price > 0 else 0

            # ATR expansion: compare current ATR vs 3h ago
            if len(candles) >= 18:
                old_trs = []
                for i in range(-17, -3):
                    h = highs[i]
                    l = lows[i]
                    prev_c = closes[i - 1]
                    tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
                    old_trs.append(tr)
                old_atr = sum(old_trs) / len(old_trs)
                f.atr_expanding = f.atr > old_atr * 1.1  # 10% increase = expanding

        # Bollinger Bands (20, 2)
        if len(closes) >= 20:
            recent = closes[-20:]
            mean = sum(recent) / 20
            std = math.sqrt(sum((x - mean) ** 2 for x in recent) / 20)
            f.bb_upper = round(mean + 2 * std, 2)
            f.bb_lower = round(mean - 2 * std, 2)
            f.bb_middle = round(mean, 2)
            f.bb_bandwidth_pct = round((f.bb_upper - f.bb_lower) / mean * 100, 2) if mean > 0 else 0
            bb_range = f.bb_upper - f.bb_lower
            f.bb_position_pct = round((price - f.bb_lower) / bb_range * 100, 1) if bb_range > 0 else 50

        # ============ MOMENTUM ============

        # RSI (14)
        if len(closes) >= 15:
            gains, losses = [], []
            for i in range(-14, 0):
                diff = closes[i] - closes[i - 1]
                if diff > 0:
                    gains.append(diff)
                else:
                    losses.append(abs(diff))
            avg_gain = sum(gains) / 14 if gains else 0.001
            avg_loss = sum(losses) / 14 if losses else 0.001
            rs = avg_gain / avg_loss
            f.rsi = round(100 - (100 / (1 + rs)), 2)

        # Momentum (% change)
        if len(closes) >= 2:
            f.momentum_1h = round((price - closes[-2]) / closes[-2] * 100, 3) if closes[-2] > 0 else 0
        if len(closes) >= 5:
            f.momentum_4h = round((price - closes[-5]) / closes[-5] * 100, 3) if closes[-5] > 0 else 0

        # Speed (5 min — using most recent candle high-low as proxy)
        f.speed_5m = round(f.momentum_1h / 12, 3) if f.momentum_1h else 0  # rough estimate
        if snapshot.price > 0 and len(closes) >= 2:
            # Better: use actual price change
            f.speed_5m = round(abs(snapshot.price - closes[-1]) / closes[-1] * 100, 3)

        # ============ LIQUIDITY ============

        f.spread_pct = round(snapshot.spread / price * 100, 4) if price > 0 else 0

        # Volume ratio (current vs 20-period avg)
        if len(volumes) >= 20 and any(v > 0 for v in volumes[-20:]):
            avg_vol = sum(volumes[-20:]) / 20
            current_vol = volumes[-1] if volumes[-1] > 0 else avg_vol
            f.volume_ratio = round(current_vol / avg_vol, 2) if avg_vol > 0 else 1.0

        # ============ ACCELERATION (for predictive kill switch) ============

        # Volatility acceleration: rate of change of ATR
        if f.atr > 0 and len(candles) >= 18:
            old_trs = []
            for i in range(-17, -3):
                if i - 1 >= -len(candles):
                    h = highs[i]
                    l = lows[i]
                    prev_c = closes[i - 1]
                    tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
                    old_trs.append(tr)
            if old_trs:
                old_atr = sum(old_trs) / len(old_trs)
                f.vol_acceleration = round((f.atr - old_atr) / old_atr * 100, 2) if old_atr > 0 else 0

        # Price acceleration: 2nd derivative (change of change)
        if len(closes) >= 4:
            delta_1 = closes[-1] - closes[-2]
            delta_2 = closes[-2] - closes[-3]
            f.price_acceleration = round((delta_1 - delta_2) / price * 100, 4) if price > 0 else 0

        return f
