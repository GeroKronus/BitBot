"""GridStrategy — Mean reversion grid trading.

Only active in RANGE (and TREND_WEAK with reduced size).
Generates buy levels below price, sell levels above.

Key improvements over v3:
- Spacing based on max(ATR * k, spread * 2) — spread-aware
- No trade zone: skip if BB bandwidth < 1.5% (market too dead)
- Exposure cap integrated: won't generate buys if exposure > limit
- Size adjusted by regime confidence
- Cooldown after taking profit (avoid overtrading in euphoria)
"""

import math
from datetime import datetime, timezone, timedelta
from ..core.interfaces import IStrategy, Features, RegimeState, Position, GovernorDecision, Signal


class GridStrategy(IStrategy):

    def __init__(self, config: dict):
        """
        Config keys:
            grid_levels: int (default 5)
            order_size_usdt: float (default 20)
            min_spacing_pct: float (default 0.3)
            max_spacing_pct: float (default 2.0)
            spacing_atr_k: float (default 0.8) — spacing = ATR * k
        """
        self.grid_levels = config.get("grid_levels", 5)
        self.order_size_usdt = config.get("order_size_usdt", 20.0)
        self.min_spacing_pct = config.get("min_spacing_pct", 0.3)
        self.max_spacing_pct = config.get("max_spacing_pct", 2.0)
        self.spacing_atr_k = config.get("spacing_atr_k", 0.8)

        # State
        self._base_price = 0.0
        self._active_buy_levels = []
        self._active_sell_levels = []
        self._last_profit_at = None
        self._trade_pnls = []  # for health tracking

    def name(self) -> str:
        return "GRID"

    def generate_signals(self, features: Features, regime: RegimeState,
                         position: Position, governor: GovernorDecision) -> list:
        signals = []
        price = features.sma_20 or features.bb_middle
        if price <= 0:
            return signals

        # ===== NO TRADE ZONE (ChatGPT 3.2: "energy check") =====
        # Skip if expected move < cost × 2 (not enough energy to profit)
        round_trip_cost = (0.05 + 0.05) * 2  # 0.20%
        expected_move = features.atr_pct if features.atr_pct > 0 else features.bb_bandwidth_pct / 2
        if expected_move < round_trip_cost * 2 and expected_move > 0:
            return signals  # not enough energy

        # Also skip if BB too tight (original check)
        if features.bb_bandwidth_pct < 1.5 and features.bb_bandwidth_pct > 0:
            return signals

        # ===== COOLDOWN AFTER PROFIT =====
        # Avoid overtrading in euphoria
        if self._last_profit_at:
            cooldown = timedelta(seconds=30)
            if datetime.now(timezone.utc) - self._last_profit_at < cooldown:
                return signals

        # ===== CALCULATE DYNAMIC SPACING =====
        spacing_pct = self._calculate_spacing(features)

        # ===== ADJUST FOR REGIME AND CONFIDENCE =====
        levels = self.grid_levels
        size_mult = 1.0

        if regime.current == "TREND_WEAK":
            levels = max(2, int(self.grid_levels * 0.6))
            size_mult = 0.6

        # Scale by regime confidence
        size_mult *= (0.5 + 0.5 * regime.confidence)

        # Scale by governor max exposure
        if governor.max_exposure_pct < 80:
            size_mult *= governor.max_exposure_pct / 80.0

        order_size = round(self.order_size_usdt * size_mult, 2)
        if order_size < 10:  # Hyperliquid minimum
            return signals

        # ===== MICRO-TREND DETECTION (ChatGPT: disable one side) =====
        # If slight trend detected while still in RANGE, only trade WITH the trend
        micro_trend_up = features.sma_slope_20 > 0.03 and features.momentum_1h > 0.3
        micro_trend_down = features.sma_slope_20 < -0.03 and features.momentum_1h < -0.3
        disable_sells = micro_trend_up   # don't sell against rising market
        disable_buys = micro_trend_down  # don't buy against falling market

        # ===== EXPOSURE CHECK =====
        max_exposure = governor.max_exposure_pct / 100
        current_exposure = position.notional / (position.notional + 100) if position.notional > 0 else 0

        # ===== SET BASE PRICE =====
        if self._base_price == 0:
            self._base_price = price

        # Rebase if price moved too far from base
        deviation = abs(price - self._base_price) / self._base_price
        if deviation > 0.03:  # >3% deviation → rebase
            self._base_price = price

        # ===== ECONOMIC FILTER (ChatGPT: skip if net profit <= 0) =====
        round_trip_cost_pct = (0.05 + 0.05) * 2  # (fee + slip) × 2 sides = 0.20%
        net_profit_per_cycle = spacing_pct - round_trip_cost_pct
        if net_profit_per_cycle <= 0:
            return signals  # not profitable — don't trade

        # ===== GENERATE GRID LEVELS =====
        spacing = spacing_pct / 100

        for i in range(1, levels + 1):
            # Buy levels below (only if exposure and micro-trend allow)
            if current_exposure < max_exposure and not disable_buys:
                buy_price = round(self._base_price * (1 - spacing * i), 1)
                buy_amount = round(order_size / buy_price, 5) if buy_price > 0 else 0

                if buy_amount > 0 and buy_price > 0:
                    signals.append(Signal(
                        side="buy",
                        price=buy_price,
                        amount=buy_amount,
                        order_type="limit",
                        source="GRID",
                        confidence=regime.confidence,
                        metadata={
                            "level": i,
                            "spacing_pct": spacing_pct,
                            "size_mult": size_mult,
                        },
                    ))

            # Sell levels above (skip if micro-trend up)
            if disable_sells:
                continue

            sell_price = round(self._base_price * (1 + spacing * i), 1)
            sell_amount = round(order_size / sell_price, 5) if sell_price > 0 else 0

            if sell_amount > 0 and sell_price > 0:
                signals.append(Signal(
                    side="sell",
                    price=sell_price,
                    amount=sell_amount,
                    order_type="limit",
                    source="GRID",
                    confidence=regime.confidence,
                    metadata={
                        "level": i,
                        "spacing_pct": spacing_pct,
                        "size_mult": size_mult,
                    },
                ))

        return signals

    def _calculate_spacing(self, features: Features) -> float:
        """Dynamic spacing with volatility regime adjustment.

        Layers (all must be satisfied):
        1. Cost floor: spacing >= round_trip_cost × 2.5
        2. ATR-adaptive: ATR × k with volatility regime multiplier
        3. Spread floor: spacing >= spread × 2
        4. Clamped to min/max config
        """
        fee_pct = 0.05
        slippage_pct = 0.05
        round_trip_cost = (fee_pct + slippage_pct) * 2  # 0.20%
        min_profitable = round_trip_cost * 2.5  # 0.50%

        # ATR-based with volatility regime multiplier (ChatGPT 3.1)
        atr_spacing = features.atr_pct * self.spacing_atr_k if features.atr_pct > 0 else 0.5
        if features.atr_pct < 0.8:
            atr_spacing *= 1.5   # low vol → wider spacing (conservative)
        elif features.atr_pct > 2.0:
            atr_spacing *= 0.8   # high vol → tighter spacing (capture more moves)

        # Spread floor
        spread_spacing = features.spread_pct * 2 if features.spread_pct > 0 else 0

        # Use the LARGEST
        spacing = max(atr_spacing, spread_spacing, min_profitable)

        # Clamp
        spacing = max(self.min_spacing_pct, min(self.max_spacing_pct, spacing))

        return round(spacing, 2)

    def record_profit(self, pnl: float = 0, cost: float = 0):
        """Called externally when a grid cycle completes."""
        self._last_profit_at = datetime.now(timezone.utc)
        self._trade_pnls.append(pnl - cost)

    def get_health(self) -> dict:
        """Key metric: profit_per_trade_after_cost (ChatGPT 3.3)."""
        if not self._trade_pnls:
            return {"avg_profit_after_cost": 0, "trades": 0, "healthy": True}
        avg = sum(self._trade_pnls) / len(self._trade_pnls)
        return {
            "avg_profit_after_cost": round(avg, 6),
            "trades": len(self._trade_pnls),
            "healthy": avg >= 0,  # positive = system working
        }

    def reset(self):
        """Reset grid state (called on regime change or manual reset)."""
        self._base_price = 0.0
        self._active_buy_levels = []
        self._active_sell_levels = []
        self._trade_pnls = []
