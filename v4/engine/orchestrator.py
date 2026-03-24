"""StrategyOrchestrator — Selects and runs the right strategy per regime.

RANGE → GridStrategy (mean reversion, multiple levels)
TREND_STRONG → TrendStrategy (pullback entry, trailing stop)
TREND_WEAK → GridStrategy with reduced size
BREAKOUT → NoTradeStrategy (wait for confirmation → becomes TREND_STRONG)
CHAOS → NoTradeStrategy (kill switch territory)

Manages strategy lifecycle and prevents overtrading during transitions.
"""

from ..core.interfaces import (
    IStrategy, Features, RegimeState, Position, GovernorDecision, Signal
)


class StrategyOrchestrator:
    """Selects and executes the appropriate strategy based on regime."""

    def __init__(self, strategies: dict):
        """
        Args:
            strategies: dict mapping regime names to IStrategy instances.
                        Must include at least: "RANGE", "TREND", "NO_TRADE"
        """
        self._strategies = strategies
        self._active_strategy = None
        self._active_name = ""

    def select_and_run(self, features: Features, regime: RegimeState,
                       position: Position, governor: GovernorDecision) -> list:
        """Select strategy based on regime and generate signals."""

        # Governor can disable all trading
        if not governor.allow_trading or governor.mode == "shutdown":
            return self._run("NO_TRADE", features, regime, position, governor)

        # Governor can disable specific strategies
        regime_name = regime.current

        if regime_name == "RANGE":
            if not governor.grid_enabled:
                return self._run("NO_TRADE", features, regime, position, governor)
            return self._run("RANGE", features, regime, position, governor)

        elif regime_name == "TREND_STRONG":
            if not governor.trend_enabled:
                return self._run("NO_TRADE", features, regime, position, governor)
            return self._run("TREND", features, regime, position, governor)

        elif regime_name == "TREND_WEAK":
            # Weak trend: use grid but with reduced confidence
            if not governor.grid_enabled:
                return self._run("NO_TRADE", features, regime, position, governor)
            return self._run("RANGE", features, regime, position, governor)

        elif regime_name == "BREAKOUT":
            # Breakout: don't trade until confirmed (becomes TREND_STRONG)
            return self._run("NO_TRADE", features, regime, position, governor)

        elif regime_name == "CHAOS":
            # CHAOS: absolutely no new trades
            return self._run("NO_TRADE", features, regime, position, governor)

        # Unknown regime: safe default
        return self._run("NO_TRADE", features, regime, position, governor)

    def _run(self, strategy_key: str, features: Features, regime: RegimeState,
             position: Position, governor: GovernorDecision) -> list:
        strategy = self._strategies.get(strategy_key)
        if strategy is None:
            return []

        # Track strategy changes
        if strategy_key != self._active_name:
            self._active_name = strategy_key
            self._active_strategy = strategy

        return strategy.generate_signals(features, regime, position, governor)

    @property
    def active_strategy_name(self) -> str:
        return self._active_name
