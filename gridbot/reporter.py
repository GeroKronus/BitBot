"""Daily P&L reporter — sends report at configured hour (default 21h UTC)."""

from datetime import datetime, timezone, date

from .logger import load_trades
from .notifier import Notifier


class Reporter:
    def __init__(self, config, grid, notifier: Notifier):
        self.config = config
        self.grid = grid
        self.notifier = notifier
        self.last_report_date: date | None = None

    def check_schedule(self):
        now = datetime.now(timezone.utc)
        if now.hour == self.config.report_hour_utc and now.date() != self.last_report_date:
            self.last_report_date = now.date()
            self.notifier.send(self.get_pnl_text())

    def get_pnl_text(self) -> str:
        trades = load_trades(self.config.trade_log)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        today_trades = [
            t for t in trades if t.get("timestamp", "").startswith(today)
        ]

        buys = [t for t in today_trades if t["side"] == "buy"]
        sells = [t for t in today_trades if t["side"] == "sell"]
        total_buy_vol = sum(t["cost"] for t in buys)
        total_sell_vol = sum(t["cost"] for t in sells)
        realized = sum(t.get("pnl", 0.0) for t in sells)

        position = self.grid.get_position_btc()
        unrealized = self.grid.get_unrealized_pnl()
        balance = self.grid.exchange.get_balance()

        all_time_pnl = self.grid.realized_pnl

        return (
            f"--- Daily P&L Report ---\n"
            f"Date: {today}\n"
            f"Trades today: {len(today_trades)} "
            f"({len(buys)} buys, {len(sells)} sells)\n"
            f"Buy volume: ${total_buy_vol:,.2f}\n"
            f"Sell volume: ${total_sell_vol:,.2f}\n"
            f"Realized P&L (today): ${realized:+,.2f}\n"
            f"Realized P&L (total): ${all_time_pnl:+,.2f}\n"
            f"Position: {position:.6f} BTC\n"
            f"Unrealized P&L: ${unrealized:+,.2f}\n"
            f"Balance: {balance.get('USDT', balance.get('USDC', 0.0)):.2f} "
            f"{'USDC' if 'USDC' in balance else 'USDT'} | "
            f"{balance.get('BTC', 0.0):.6f} BTC"
        )
