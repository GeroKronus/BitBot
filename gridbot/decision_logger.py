"""Decision Logger — Structured logging of all bot decisions for observability."""

import json
import os
from datetime import datetime, timezone


class DecisionLogger:
    """Logs every decision the bot makes in structured JSON format.

    Captures: regime, AI outlook, actions taken, reasons, parameters changed.
    Enables post-mortem analysis of why the bot did what it did.
    """

    def __init__(self, log_path: str = "data/decisions.jsonl"):
        self.log_path = log_path
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

    def log(self, decision_type: str, details: dict):
        """Log a decision.

        Args:
            decision_type: Type of decision (tick, regime_change, kill_switch,
                          ai_analysis, exposure_block, trade, parameter_change)
            details: Dict with decision details
        """
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": decision_type,
            **details,
        }
        try:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def log_tick(self, price: float, regime: str, ai_outlook: str,
                 can_trade: bool, position: float, balance: float,
                 buy_orders: int, sell_orders: int):
        """Log a regular tick (every N ticks, not every 5 seconds)."""
        self.log("tick", {
            "price": price,
            "regime": regime,
            "ai_outlook": ai_outlook,
            "can_trade": can_trade,
            "position_btc": position,
            "balance": balance,
            "buy_orders": buy_orders,
            "sell_orders": sell_orders,
        })

    def log_regime_change(self, old_regime: str, new_regime: str,
                          indicators: dict, confidence: float):
        """Log when market regime changes."""
        self.log("regime_change", {
            "old_regime": old_regime,
            "new_regime": new_regime,
            "indicators": indicators,
            "confidence": confidence,
        })

    def log_kill_switch(self, reasons: list[str], cooldown_minutes: int,
                        price: float, position: float):
        """Log kill switch activation."""
        self.log("kill_switch", {
            "reasons": reasons,
            "cooldown_minutes": cooldown_minutes,
            "price": price,
            "position_btc": position,
        })

    def log_ai_analysis(self, outlook: str, confidence: int, signal: str,
                        reason: str, adjustments: dict):
        """Log AI analysis result and what it changed."""
        self.log("ai_analysis", {
            "outlook": outlook,
            "confidence": confidence,
            "signal": signal,
            "reason": reason,
            "adjustments": adjustments,
        })

    def log_exposure_block(self, reasons: list[str], warnings: list[str],
                           daily_pnl_pct: float):
        """Log when exposure manager blocks trading."""
        self.log("exposure_block", {
            "reasons": reasons,
            "warnings": warnings,
            "daily_pnl_pct": daily_pnl_pct,
        })

    def log_parameter_change(self, parameter: str, old_value, new_value,
                             reason: str):
        """Log when any parameter is changed."""
        self.log("parameter_change", {
            "parameter": parameter,
            "old_value": old_value,
            "new_value": new_value,
            "reason": reason,
        })

    def get_recent(self, count: int = 50, decision_type: str = None) -> list[dict]:
        """Get recent decisions for dashboard display."""
        entries = []
        try:
            if not os.path.exists(self.log_path):
                return entries
            with open(self.log_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entry = json.loads(line)
                            if decision_type is None or entry.get("type") == decision_type:
                                entries.append(entry)
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass
        return entries[-count:]
