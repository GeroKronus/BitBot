"""JSON Lines trade logger."""

import json
import os
from datetime import datetime, timezone


def log_trade(trade: dict, filepath: str = "data/trades.jsonl"):
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "side": trade["side"],
        "price": trade["price"],
        "amount": trade["amount"],
        "cost": trade["amount"] * trade["price"],
        "fee": trade.get("fee", 0.0),
        "pnl": trade.get("pnl", 0.0),
        "mode": trade.get("mode", "paper"),
    }
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "a") as f:
        f.write(json.dumps(entry) + "\n")


def load_trades(filepath: str = "data/trades.jsonl") -> list[dict]:
    trades = []
    if not os.path.exists(filepath):
        return trades
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                trades.append(json.loads(line))
    return trades
