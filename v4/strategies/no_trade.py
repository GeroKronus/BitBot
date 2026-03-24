"""NoTradeStrategy — Returns no signals. Used in CHAOS, BREAKOUT, or when governor says stop.

Also generates CLOSE signals if there's an open position that should be closed
(e.g., regime changed to CHAOS while holding a position).
"""

from ..core.interfaces import IStrategy, Features, RegimeState, Position, GovernorDecision, Signal


class NoTradeStrategy(IStrategy):

    def name(self) -> str:
        return "NO_TRADE"

    def generate_signals(self, features: Features, regime: RegimeState,
                         position: Position, governor: GovernorDecision) -> list:

        signals = []

        # If in CHAOS or governor shutdown, close any open position
        if regime.current == "CHAOS" or governor.mode == "shutdown":
            if position.side != "flat" and position.size > 0:
                signals.append(Signal(
                    side="close",
                    price=features.sma_20 or 0,
                    amount=position.size,
                    order_type="market",
                    reduce_only=True,
                    source="NO_TRADE",
                    confidence=1.0,
                    metadata={"reason": f"Emergency close — regime: {regime.current}"},
                ))

        return signals
