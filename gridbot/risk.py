"""Risk management: Stop Loss + Trailing Profit."""

from .notifier import Notifier


class RiskManager:
    def __init__(self, config, grid, notifier: Notifier):
        self.config = config
        self.grid = grid
        self.notifier = notifier

        self.trailing_active = False
        self.trailing_high = 0.0
        self.trailing_stop_price = 0.0
        self.stopped = False

    async def check(self, current_price: float):
        position = self.grid.get_position_btc()
        if position <= 0 or self.stopped:
            self.trailing_active = False
            self.trailing_high = 0.0
            self.trailing_stop_price = 0.0
            return

        avg_entry = self.grid.get_avg_entry()
        if avg_entry == 0:
            return

        # --- STOP LOSS ---
        stop_price = avg_entry * (1 - self.config.stop_loss_pct / 100)
        if current_price <= stop_price:
            await self._execute_stop(current_price, "STOP LOSS")
            return

        # --- TRAILING PROFIT ---
        unrealized_pct = (current_price - avg_entry) / avg_entry * 100

        if unrealized_pct >= self.config.trailing_profit_pct:
            if not self.trailing_active:
                self.trailing_active = True
                self.trailing_high = current_price
                self.trailing_stop_price = current_price * (
                    1 - self.config.trailing_callback_pct / 100
                )
                self.notifier.send(
                    f"Trailing profit activated at ${current_price:,.2f} "
                    f"(+{unrealized_pct:.1f}%)"
                )

        if self.trailing_active:
            if current_price > self.trailing_high:
                self.trailing_high = current_price
                self.trailing_stop_price = self.trailing_high * (
                    1 - self.config.trailing_callback_pct / 100
                )

            if current_price <= self.trailing_stop_price:
                await self._execute_stop(current_price, "TRAILING STOP")

    async def _execute_stop(self, price: float, reason: str):
        position = self.grid.get_position_btc()
        avg_entry = self.grid.get_avg_entry()
        pnl_pct = (price - avg_entry) / avg_entry * 100

        trade = await self.grid.market_sell_all(price)
        if trade:
            self.notifier.send(
                f"{reason} triggered at ${price:,.2f}\n"
                f"Sold {position:.6f} BTC\n"
                f"Entry avg: ${avg_entry:,.2f}\n"
                f"P&L: ${trade['pnl']:+,.2f} ({pnl_pct:+.1f}%)"
            )

        self.stopped = True
        self.trailing_active = False
        self.trailing_high = 0.0
        self.trailing_stop_price = 0.0

        # Reset grid with new base price
        await self.grid.reset()
        self.stopped = False
        self.notifier.send("Grid reset after stop. Trading resumed.")

    def get_status(self) -> dict:
        return {
            "trailing_active": self.trailing_active,
            "trailing_high": self.trailing_high,
            "trailing_stop_price": self.trailing_stop_price,
            "stopped": self.stopped,
        }
