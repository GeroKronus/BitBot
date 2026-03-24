"""RiskEngine — Evaluates and filters signals before execution.

Dynamic risk budget based on: f(volatility, regime_confidence, governor)
Can block trades, resize positions, force exits.

Key features:
- Risk budget = capital * base_risk * regime_confidence * volatility_factor
- Exposure limit dynamic: 80% normal, 50% high vol, 30% chaos
- Max position size enforcement
- Drawdown tracking (daily + total)
- Force exit if position violates rules
"""

from datetime import datetime, timezone
from ..core.interfaces import (
    IRiskEngine, Signal, Position, Features, RegimeState, GovernorDecision
)


class RiskEngine(IRiskEngine):

    def __init__(self, config: dict):
        """
        Config keys:
            capital: float (total capital in USDC)
            base_risk_pct: float (base risk per trade, default 2.0)
            max_exposure_normal_pct: float (default 80)
            max_exposure_high_vol_pct: float (default 50)
            max_exposure_chaos_pct: float (default 30)
            atr_high_threshold: float (ATR% above which = high vol, default 2.0)
            max_daily_drawdown_pct: float (default 5.0)
            max_total_drawdown_pct: float (default 15.0)
            min_order_value: float (exchange minimum, default 10.0)
        """
        self.capital = config.get("capital", 130.0)
        self.base_risk_pct = config.get("base_risk_pct", 2.0)
        self.max_exp_normal = config.get("max_exposure_normal_pct", 80)
        self.max_exp_high_vol = config.get("max_exposure_high_vol_pct", 50)
        self.max_exp_chaos = config.get("max_exposure_chaos_pct", 30)
        self.atr_high_threshold = config.get("atr_high_threshold", 2.0)
        self.max_daily_dd = config.get("max_daily_drawdown_pct", 5.0)
        self.max_total_dd = config.get("max_total_drawdown_pct", 15.0)
        self.min_order_value = config.get("min_order_value", 10.0)

        # Tracking
        self._daily_start_balance = self.capital
        self._daily_date = ""
        self._daily_stopped = False
        self._emergency_stopped = False

    def evaluate(self, signals: list, position: Position,
                 features: Features, regime: RegimeState,
                 governor: GovernorDecision) -> list:
        """Filter and modify signals. Returns approved signals only."""

        if self._emergency_stopped:
            # Only allow close signals
            return [s for s in signals if s.side == "close"]

        # Update daily tracking
        self._update_daily(features)

        # PREEMPTIVE: cut exposure if volatility accelerating (ChatGPT: semi-predictive)
        if features.vol_acceleration > 30 and features.atr_pct > 1.5:
            # Vol expanding rapidly — reduce exposure before ATR catches up
            preemptive_signals = self._preemptive_cut(position, features)
            if preemptive_signals:
                return preemptive_signals  # override everything

        # Calculate dynamic limits
        max_exposure_pct = self._dynamic_exposure_limit(features, regime, governor)
        risk_budget = self._calculate_risk_budget(regime, governor)

        approved = []
        cumulative_new_exposure = 0.0  # track total new exposure from this batch
        for signal in signals:
            # BLOCK if cumulative new signals would exceed limit (ChatGPT: don't just resize)
            if signal.side != "close" and not signal.reduce_only:
                projected = position.notional + cumulative_new_exposure + signal.amount * signal.price
                projected_pct = (projected / self.capital * 100) if self.capital > 0 else 0
                if projected_pct > max_exposure_pct:
                    continue  # BLOCK entirely, not resize

            result = self._evaluate_signal(signal, position, features,
                                           max_exposure_pct, risk_budget)
            if result is not None:
                approved.append(result)
                if result.side in ("buy", "sell") and not result.reduce_only:
                    cumulative_new_exposure += result.amount * result.price

        # Check for forced exits
        force_exits = self._check_force_exits(position, features, regime,
                                              max_exposure_pct)
        approved.extend(force_exits)

        return approved

    def _evaluate_signal(self, signal: Signal, position: Position,
                         features: Features, max_exposure_pct: float,
                         risk_budget: float):
        """Evaluate a single signal. Returns modified signal or None."""

        # Always allow close/reduce signals
        if signal.side == "close" or signal.reduce_only:
            return signal

        # Check daily drawdown
        if self._daily_stopped:
            return None

        price = signal.price or features.sma_20
        if price <= 0:
            return None

        # Check order minimum
        order_value = signal.amount * price
        if order_value < self.min_order_value:
            return None

        # Check exposure limit
        current_exposure_pct = (position.notional / self.capital * 100) if self.capital > 0 else 0
        new_exposure = order_value + position.notional
        new_exposure_pct = (new_exposure / self.capital * 100) if self.capital > 0 else 0

        if new_exposure_pct > max_exposure_pct:
            # Can we resize to fit?
            remaining = (max_exposure_pct / 100 * self.capital) - position.notional
            if remaining > self.min_order_value:
                new_amount = round(remaining / price, 5)
                if new_amount * price >= self.min_order_value:
                    signal.amount = new_amount
                    signal.metadata["resized_by_risk"] = True
                    return signal
            return None  # can't fit

        # Apply risk budget to non-grid signals
        if signal.source == "TREND":
            max_size_usdt = self.capital * risk_budget / 100
            if order_value > max_size_usdt:
                new_amount = round(max_size_usdt / price, 5)
                if new_amount * price >= self.min_order_value:
                    signal.amount = new_amount
                    signal.metadata["resized_by_risk_budget"] = True
                else:
                    return None

        return signal

    def _preemptive_cut(self, position: Position, features: Features) -> list:
        """Preemptively reduce exposure when volatility is accelerating.

        ChatGPT: 'volatility_acceleration = d(ATR)/dt → cut_exposure_preemptively'
        This acts BEFORE ATR fully catches up, giving us a head start.
        """
        if position.side == "flat" or position.size == 0:
            return []

        from ..core.interfaces import Signal
        return [Signal(
            side="close",
            price=features.sma_20 or 0,
            amount=position.size,
            order_type="market",
            reduce_only=True,
            source="RISK_PREEMPTIVE",
            confidence=1.0,
            metadata={
                "reason": f"Vol accelerating: +{features.vol_acceleration:.0f}% "
                          f"(ATR: {features.atr_pct:.1f}%) — preemptive cut"
            },
        )]

    def _check_force_exits(self, position: Position, features: Features,
                           regime: RegimeState, max_exposure_pct: float) -> list:
        """Check if current position violates rules and needs forced exit."""
        exits = []

        if position.side == "flat" or position.size == 0:
            return exits

        current_exposure_pct = (position.notional / self.capital * 100) if self.capital > 0 else 0

        # Force exit if over max exposure
        if current_exposure_pct > max_exposure_pct * 1.2:  # 20% grace before force
            exits.append(Signal(
                side="close",
                price=features.sma_20 or 0,
                amount=position.size,
                order_type="market",
                reduce_only=True,
                source="RISK_ENGINE",
                confidence=1.0,
                metadata={"reason": f"Exposure {current_exposure_pct:.0f}% > {max_exposure_pct:.0f}% limit"},
            ))

        return exits

    def _dynamic_exposure_limit(self, features: Features, regime: RegimeState,
                                governor: GovernorDecision) -> float:
        """Dynamic exposure limit based on ATR, regime, governor.

        ChatGPT recommendation: 80% normal, 50% high vol, 30% chaos.
        Scales by regime confidence.
        """
        # Base from regime
        if regime.current == "CHAOS":
            base = self.max_exp_chaos
        elif features.atr_pct > self.atr_high_threshold:
            base = self.max_exp_high_vol
        else:
            base = self.max_exp_normal

        # Governor override (always respect lower limit)
        base = min(base, governor.max_exposure_pct)

        # Scale by confidence: low confidence → lower limit
        scaled = base * (0.5 + 0.5 * regime.confidence)

        return round(scaled, 1)

    def _calculate_risk_budget(self, regime: RegimeState,
                               governor: GovernorDecision) -> float:
        """Risk budget per trade = base_risk * confidence * governor_mode.

        Returns % of capital that can be risked per trade.
        """
        budget = self.base_risk_pct * regime.confidence

        if governor.mode == "conservative":
            budget *= 0.5
        elif governor.mode == "aggressive":
            budget *= 1.5

        return round(max(0.5, min(5.0, budget)), 2)

    def _update_daily(self, features: Features):
        """Track daily drawdown."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_date:
            self._daily_date = today
            self._daily_start_balance = self.capital
            self._daily_stopped = False

    def update_capital(self, new_capital: float):
        """Called when balance changes."""
        daily_pnl_pct = ((new_capital - self._daily_start_balance) /
                         self._daily_start_balance * 100) if self._daily_start_balance > 0 else 0

        if daily_pnl_pct <= -self.max_daily_dd:
            self._daily_stopped = True

        total_pnl_pct = ((new_capital - self.capital) / self.capital * 100) if self.capital > 0 else 0
        if total_pnl_pct <= -self.max_total_dd:
            self._emergency_stopped = True

    def get_status(self) -> dict:
        return {
            "daily_stopped": self._daily_stopped,
            "emergency_stopped": self._emergency_stopped,
            "capital": self.capital,
        }
