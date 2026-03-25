"""GridStrategy — Micro-profit machine for lateral markets.

Philosophy: "ganha pouco mas sempre" (Caminho A)
- 5-7 levels per side
- Spacing 0.4%-0.6% (tighter = more trades)
- Does NOT disable in weak trend — only in strong trend
- Low energy mode: operates small instead of zero
- Spread-aware, cost-aware, micro-trend aware
"""

import math
from datetime import datetime, timezone, timedelta
from ..core.interfaces import IStrategy, Features, RegimeState, Position, GovernorDecision, Signal


# Centralized multiplier calculation (auditor: eliminate implicit dependencies)
def _compute_size_multiplier(regime: RegimeState, governor: GovernorDecision,
                             low_energy: bool) -> float:
    """Single source of truth for order size multiplier."""
    mult = 1.0

    # Regime adjustment — only reduce in STRONG trend, not weak
    if regime.current == "TREND_STRONG":
        mult *= 0.4
    elif regime.current == "TREND_WEAK":
        mult *= 0.7
    # RANGE: full size (1.0)

    # Confidence scaling
    mult *= (0.5 + 0.5 * regime.confidence)

    # Governor max exposure
    if governor.max_exposure_pct < 80:
        mult *= governor.max_exposure_pct / 80.0

    # Low energy mode
    if low_energy:
        mult *= 0.3

    return round(mult, 3)


class GridStrategy(IStrategy):

    def __init__(self, config: dict):
        self.grid_levels = config.get("grid_levels", 7)         # 7 per side (was 5)
        self.order_size_usdt = config.get("order_size_usdt", 20.0)
        self.min_spacing_pct = config.get("min_spacing_pct", 0.4)  # tighter (was 0.3)
        self.max_spacing_pct = config.get("max_spacing_pct", 1.5)  # lower cap
        self.spacing_atr_k = config.get("spacing_atr_k", 0.6)     # tighter ATR multiplier

        # State
        self._base_price = 0.0
        self._last_profit_at = None
        self._trade_pnls = []
        self._no_trade_reasons = []  # log why we don't trade

    def name(self) -> str:
        return "GRID"

    def generate_signals(self, features: Features, regime: RegimeState,
                         position: Position, governor: GovernorDecision) -> list:
        signals = []
        price = features.sma_20 or features.bb_middle
        if price <= 0:
            self._log_no_trade("No price data")
            return signals

        # ===== 1. ENERGY CHECK =====
        round_trip_cost = (0.05 + 0.05) * 2  # 0.20%
        expected_move = features.atr_pct if features.atr_pct > 0 else features.bb_bandwidth_pct / 2
        low_energy = (expected_move < round_trip_cost * 2 and expected_move > 0) or \
                     (features.bb_bandwidth_pct < 1.0 and features.bb_bandwidth_pct > 0)

        # ===== 2. SIZE MULTIPLIER (centralized — no more implicit deps) =====
        size_mult = _compute_size_multiplier(regime, governor, low_energy)

        order_size = round(self.order_size_usdt * size_mult, 2)
        if order_size < 10:  # Hyperliquid minimum
            self._log_no_trade(f"Order size too small: ${order_size:.2f}")
            return signals

        # ===== 3. COOLDOWN AFTER PROFIT =====
        if self._last_profit_at:
            cooldown = timedelta(seconds=20)  # shorter cooldown for micro-profit
            if datetime.now(timezone.utc) - self._last_profit_at < cooldown:
                return signals

        # ===== 4. SPACING (dynamic, cost-aware) =====
        spacing_pct = self._calculate_spacing(features, low_energy)

        # ===== 5. ECONOMIC FILTER =====
        net_profit_per_cycle = spacing_pct - round_trip_cost
        if net_profit_per_cycle <= 0:
            self._log_no_trade(f"Not profitable: spacing {spacing_pct}% <= cost {round_trip_cost}%")
            return signals

        # ===== 6. MICRO-TREND DETECTION =====
        # In RANGE or TREND_WEAK: disable one side if slight direction detected
        # In TREND_STRONG: strategy orchestrator handles (reduces size via multiplier)
        disable_sells = False
        disable_buys = False
        if regime.current in ("RANGE", "TREND_WEAK"):
            if features.sma_slope_20 > 0.03 and features.momentum_1h > 0.2:
                disable_sells = True  # micro uptrend: don't sell against it
            elif features.sma_slope_20 < -0.03 and features.momentum_1h < -0.2:
                disable_buys = True   # micro downtrend: don't buy against it

        # ===== 7. LEVELS =====
        levels = self.grid_levels
        if regime.current == "TREND_STRONG":
            levels = max(3, int(self.grid_levels * 0.5))  # reduce in strong trend only

        # ===== 8. EXPOSURE CHECK =====
        max_exposure = governor.max_exposure_pct / 100
        current_exposure = position.notional / (position.notional + 100) if position.notional > 0 else 0

        # ===== 9. BASE PRICE =====
        if self._base_price == 0:
            self._base_price = price
        deviation = abs(price - self._base_price) / self._base_price
        if deviation > 0.025:  # rebase at 2.5% (tighter for micro-profit)
            self._base_price = price

        # ===== 10. GENERATE GRID =====
        spacing = spacing_pct / 100

        for i in range(1, levels + 1):
            # Buy levels
            if current_exposure < max_exposure and not disable_buys:
                buy_price = round(self._base_price * (1 - spacing * i), 1)
                buy_amount = round(order_size / buy_price, 5) if buy_price > 0 else 0
                if buy_amount > 0 and buy_price > 0:
                    signals.append(Signal(
                        side="buy", price=buy_price, amount=buy_amount,
                        order_type="limit", source="GRID",
                        confidence=regime.confidence,
                        metadata={"level": i, "spacing_pct": spacing_pct, "size_mult": size_mult},
                    ))

            # Sell levels
            if not disable_sells:
                sell_price = round(self._base_price * (1 + spacing * i), 1)
                sell_amount = round(order_size / sell_price, 5) if sell_price > 0 else 0
                if sell_amount > 0 and sell_price > 0:
                    signals.append(Signal(
                        side="sell", price=sell_price, amount=sell_amount,
                        order_type="limit", source="GRID",
                        confidence=regime.confidence,
                        metadata={"level": i, "spacing_pct": spacing_pct, "size_mult": size_mult},
                    ))

        return signals

    def _calculate_spacing(self, features: Features, low_energy: bool) -> float:
        """Dynamic spacing: cost-aware, ATR-adaptive, volatility regime."""
        fee_pct = 0.05
        slippage_pct = 0.05
        round_trip_cost = (fee_pct + slippage_pct) * 2
        min_profitable = round_trip_cost * 2.5

        # ATR-based with regime multiplier
        atr_spacing = features.atr_pct * self.spacing_atr_k if features.atr_pct > 0 else 0.5
        if features.atr_pct < 0.8:
            atr_spacing *= 1.3   # low vol: slightly wider
        elif features.atr_pct > 2.0:
            atr_spacing *= 0.7   # high vol: tighter to capture more

        # Spread floor
        spread_spacing = features.spread_pct * 2 if features.spread_pct > 0 else 0

        # Low energy: wider + cost floor
        if low_energy:
            min_cost_spacing = round_trip_cost * 3.0
            spacing = max(atr_spacing, spread_spacing, min_profitable, min_cost_spacing)
        else:
            spacing = max(atr_spacing, spread_spacing, min_profitable)

        return round(max(self.min_spacing_pct, min(self.max_spacing_pct, spacing)), 2)

    def _log_no_trade(self, reason: str):
        """Track reasons for not trading (auditor: log no-trade reasons)."""
        self._no_trade_reasons.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
        })
        # Keep last 50
        self._no_trade_reasons = self._no_trade_reasons[-50:]

    def record_profit(self, pnl: float = 0, cost: float = 0):
        self._last_profit_at = datetime.now(timezone.utc)
        self._trade_pnls.append(pnl - cost)

    def get_health(self) -> dict:
        if not self._trade_pnls:
            return {"avg_profit_after_cost": 0, "trades": 0, "healthy": True,
                    "no_trade_reasons": len(self._no_trade_reasons),
                    "last_no_trade": self._no_trade_reasons[-1]["reason"] if self._no_trade_reasons else ""}
        avg = sum(self._trade_pnls) / len(self._trade_pnls)
        return {
            "avg_profit_after_cost": round(avg, 6),
            "trades": len(self._trade_pnls),
            "healthy": avg >= 0,
            "no_trade_reasons": len(self._no_trade_reasons),
            "last_no_trade": self._no_trade_reasons[-1]["reason"] if self._no_trade_reasons else "",
        }

    def reset(self):
        self._base_price = 0.0
        self._trade_pnls = []
        self._no_trade_reasons = []
