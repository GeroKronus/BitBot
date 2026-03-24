"""Configuration loading with sensible defaults for paper trading."""

import json
import os

DEFAULTS = {
    "mode": "paper",
    "exchange": "hyperliquid",
    "symbol": "BTC/USDC:USDC",
    "capital_usdt": 1000.0,
    "grid_levels": 10,
    "grid_spacing_pct": 0.5,
    "order_size_usdt": 50.0,
    "leverage": 4,
    "stop_loss_pct": 5.0,
    "trailing_profit_pct": 3.0,
    "trailing_callback_pct": 1.0,
    "tick_interval": 5,
    "report_hour_utc": 21,
    "notify_file": "/home/ubuntu/claude-to-calila.txt",
    "command_file": "/home/ubuntu/calila-to-gridbot.txt",
    "trade_log": "data/trades.jsonl",
    "state_file": "data/state.json",
    "http_port": 8099,
    "binance_api_key": "",
    "binance_api_secret": "",
    "hyperliquid_private_key": "",
    "hyperliquid_wallet_address": "",
}


class Config:
    def __init__(self, data: dict):
        for key, default in DEFAULTS.items():
            setattr(self, key, data.get(key, default))

    def to_dict(self) -> dict:
        return {key: getattr(self, key) for key in DEFAULTS}


def load_config(path: str = "config.json") -> Config:
    data = {}
    if os.path.exists(path):
        with open(path, "r") as f:
            data = json.load(f)
    return Config(data)
