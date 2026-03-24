"""Notification system: writes to claude-to-calila.txt for WhatsApp relay."""

import os
from datetime import datetime, timezone


class Notifier:
    def __init__(self, config):
        self.filepath = config.notify_file

    def send(self, message: str):
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        line = f"[BitBot {timestamp}] {message}\n"
        try:
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
            with open(self.filepath, "a") as f:
                f.write(line)
        except Exception:
            pass  # don't crash the bot if notification fails

    def format_trade(self, trade: dict) -> str:
        side = trade["side"].upper()
        price = trade["price"]
        amount = trade["amount"]
        cost = amount * price
        pnl = trade.get("pnl", 0.0)
        pnl_str = f" | P&L: ${pnl:+.2f}" if trade["side"] == "sell" else ""
        return f"{side} {amount:.6f} BTC @ ${price:,.2f} (${cost:.2f}){pnl_str}"
