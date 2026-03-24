"""Risk Management v3 — Unified stop loss, trailing, ATR-based, works for long AND short."""

from datetime import datetime, timezone, timedelta
from .notifier import Notifier


class RiskManager:
    """Unified risk manager that handles:
    - ATR-based dynamic stop loss (replaces fixed %)
    - Trailing profit (for both long and short)
    - Cooldown after stop (prevents crash loops)
    - Integration with analyst's dynamic targets
    """

    def __init__(self, config, grid, notifier: Notifier):
        self.config = config
        self.grid = grid
        self.notifier = notifier

        # Trailing state
        self.trailing_active = False
        self.trailing_high = 0.0    # highest price seen (for long)
        self.trailing_low = 0.0     # lowest price seen (for short)
        self.trailing_stop_price = 0.0

        # Cooldown state
        self.stopped = False
        self.cooldown_until = None
        self.cooldown_seconds = 180  # 3 minutes default

        # Dynamic stop from analyst (if available)
        self.analyst_stop_loss = 0.0

        # ATR-based stop parameters
        self.atr_multiplier = 1.5  # stop at 1.5 * ATR from entry
        self.current_atr = 0.0

    def set_analyst_stop(self, stop_price: float):
        """Called by analyst when dynamic targets update."""
        self.analyst_stop_loss = stop_price

    def set_atr(self, atr: float):
        """Update current ATR for dynamic stop calculation."""
        self.current_atr = atr

    async def check(self, current_price: float):
        """Check all risk conditions. Works for LONG and SHORT positions."""

        # Cooldown check: don't act during cooldown
        if self._is_cooling_down():
            return

        # Get position from exchange if real mode, otherwise from grid
        position = self._get_position()
        if abs(position) < 0.000001:
            # No position, reset trailing
            self._reset_trailing()
            self.stopped = False
            return

        entry = self._get_entry_price()
        if entry == 0:
            return

        is_long = position > 0

        # --- Calculate stop loss price ---
        stop_price = self._calculate_stop(entry, is_long)

        # --- Check STOP LOSS ---
        if is_long and current_price <= stop_price:
            await self._execute_stop(current_price, position, entry, "STOP LOSS")
            return
        elif not is_long and current_price >= stop_price:
            await self._execute_stop(current_price, position, entry, "STOP LOSS (SHORT)")
            return

        # --- TRAILING PROFIT ---
        if is_long:
            unrealized_pct = (current_price - entry) / entry * 100
        else:
            unrealized_pct = (entry - current_price) / entry * 100

        # Activate trailing at configured threshold
        if unrealized_pct >= self.config.trailing_profit_pct:
            if not self.trailing_active:
                self.trailing_active = True
                self.trailing_high = current_price if is_long else 0
                self.trailing_low = current_price if not is_long else 0
                callback = self.config.trailing_callback_pct / 100
                if is_long:
                    self.trailing_stop_price = current_price * (1 - callback)
                else:
                    self.trailing_stop_price = current_price * (1 + callback)
                self.notifier.send(
                    f"Trailing profit ativado: ${current_price:,.2f} "
                    f"(+{unrealized_pct:.1f}%)"
                )

        if self.trailing_active:
            callback = self.config.trailing_callback_pct / 100
            if is_long:
                if current_price > self.trailing_high:
                    self.trailing_high = current_price
                    self.trailing_stop_price = self.trailing_high * (1 - callback)
                if current_price <= self.trailing_stop_price:
                    await self._execute_stop(current_price, position, entry, "TRAILING STOP")
            else:
                if current_price < self.trailing_low or self.trailing_low == 0:
                    self.trailing_low = current_price
                    self.trailing_stop_price = self.trailing_low * (1 + callback)
                if current_price >= self.trailing_stop_price:
                    await self._execute_stop(current_price, position, entry, "TRAILING STOP (SHORT)")

    def _calculate_stop(self, entry: float, is_long: bool) -> float:
        """Calculate best stop loss from multiple sources."""
        stops = []

        # 1. Config-based fixed % stop
        if is_long:
            stops.append(entry * (1 - self.config.stop_loss_pct / 100))
        else:
            stops.append(entry * (1 + self.config.stop_loss_pct / 100))

        # 2. ATR-based dynamic stop
        if self.current_atr > 0:
            if is_long:
                stops.append(entry - self.atr_multiplier * self.current_atr)
            else:
                stops.append(entry + self.atr_multiplier * self.current_atr)

        # 3. Analyst's dynamic stop (from AI/support levels)
        if self.analyst_stop_loss > 0:
            stops.append(self.analyst_stop_loss)

        # For LONG: use the HIGHEST stop (most protective)
        # For SHORT: use the LOWEST stop (most protective)
        if is_long:
            return max(stops)
        else:
            return min(stops)

    def _get_position(self) -> float:
        """Get current position. Prefer exchange data over internal tracking."""
        if self.config.mode == "real" and hasattr(self.grid.exchange, "ccxt_client"):
            try:
                positions = self.grid.exchange.ccxt_client.fetch_positions([self.config.symbol])
                for p in positions:
                    contracts = float(p.get("contracts", 0) or 0)
                    if contracts != 0:
                        side = p.get("side", "")
                        return contracts if side == "long" else -contracts
            except Exception:
                pass
        return self.grid.get_position_btc()

    def _get_entry_price(self) -> float:
        """Get entry price. Prefer exchange data."""
        if self.config.mode == "real" and hasattr(self.grid.exchange, "ccxt_client"):
            try:
                positions = self.grid.exchange.ccxt_client.fetch_positions([self.config.symbol])
                for p in positions:
                    contracts = float(p.get("contracts", 0) or 0)
                    if contracts != 0:
                        return float(p.get("entryPrice", 0) or 0)
            except Exception:
                pass
        return self.grid.get_avg_entry()

    async def _execute_stop(self, price: float, position: float,
                            entry: float, reason: str):
        """Execute stop loss with cooldown."""
        is_long = position > 0
        if is_long:
            pnl_pct = (price - entry) / entry * 100
        else:
            pnl_pct = (entry - price) / entry * 100

        # Close position
        trade = await self.grid.market_sell_all(price)
        if trade:
            self.notifier.send(
                f"{reason} @ ${price:,.2f}\n"
                f"Posicao: {'LONG' if is_long else 'SHORT'} {abs(position):.6f} BTC\n"
                f"Entry: ${entry:,.2f}\n"
                f"P&L: ${trade['pnl']:+,.2f} ({pnl_pct:+.1f}%)"
            )

        self.stopped = True
        self._reset_trailing()

        # Cooldown: wait before resetting grid (prevents crash loops)
        self.cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=self.cooldown_seconds)
        self.notifier.send(
            f"Cooldown: {self.cooldown_seconds}s antes de recriar grid"
        )

        # Don't reset grid immediately — let cooldown expire first
        # The main loop will detect cooldown_until and skip trading

    def _is_cooling_down(self) -> bool:
        if self.cooldown_until is None:
            return False
        now = datetime.now(timezone.utc)
        if now >= self.cooldown_until:
            # Cooldown expired, reset grid
            self.cooldown_until = None
            self.stopped = False
            return False
        return True

    def _reset_trailing(self):
        self.trailing_active = False
        self.trailing_high = 0.0
        self.trailing_low = 0.0
        self.trailing_stop_price = 0.0

    def get_status(self) -> dict:
        cooldown_remaining = ""
        if self.cooldown_until:
            remaining = (self.cooldown_until - datetime.now(timezone.utc)).total_seconds()
            if remaining > 0:
                cooldown_remaining = f"{int(remaining)}s"

        return {
            "trailing_active": self.trailing_active,
            "trailing_high": self.trailing_high,
            "trailing_low": self.trailing_low,
            "trailing_stop_price": self.trailing_stop_price,
            "stopped": self.stopped,
            "cooling_down": self._is_cooling_down(),
            "cooldown_remaining": cooldown_remaining,
            "atr": self.current_atr,
            "analyst_stop": self.analyst_stop_loss,
        }
