"""Grid Trading engine: level calculation, fill detection, counter-orders."""

import json
import os
from datetime import datetime, timezone

from .exchange import BaseExchange, Order
from .logger import log_trade


class GridManager:
    def __init__(self, config, exchange: BaseExchange):
        self.config = config
        self.exchange = exchange
        self.symbol = config.symbol

        self.base_price = 0.0
        self.last_price = 0.0
        self.buy_orders: dict[str, dict] = {}   # order_id -> {order, level_price}
        self.sell_orders: dict[str, dict] = {}   # order_id -> {order, level_price}

        self.total_bought_btc = 0.0
        self.total_spent_usdt = 0.0
        self.total_sold_btc = 0.0
        self.total_received_usdt = 0.0
        self.realized_pnl = 0.0
        self.trade_count = 0
        self.paused = False
        self.start_time = datetime.now(timezone.utc)

        # Queue for counter-orders that failed due to insufficient margin
        self.pending_counter_orders: list[dict] = []  # {side, price, amount, retries}

    async def initialize(self):
        # Always cancel existing orders on exchange before starting
        if self.config.mode == "real" and hasattr(self.exchange, "ccxt_client"):
            try:
                orders = self.exchange.ccxt_client.fetch_open_orders(self.symbol)
                for o in orders:
                    try:
                        self.exchange.ccxt_client.cancel_order(o["id"], self.symbol)
                    except Exception:
                        pass
                if orders:
                    import time
                    time.sleep(2)
                    print(f"  Cancelled {len(orders)} existing orders on exchange")
            except Exception:
                pass

        state = self._load_state()
        if state:
            self._restore_state(state)
            # Re-place grid since we cancelled all orders
            if self.config.mode == "real":
                self.buy_orders.clear()
                self.sell_orders.clear()
                await self._place_grid()
        else:
            # Load historical P&L from trade log
            self._load_historical_pnl()

            # Sync position from exchange (real mode)
            if self.config.mode == "real" and hasattr(self.exchange, "ccxt_client"):
                try:
                    positions = self.exchange.ccxt_client.fetch_positions([self.symbol])
                    for p in positions:
                        contracts = float(p.get("contracts", 0) or 0)
                        if contracts > 0:
                            entry = float(p.get("entryPrice", 0) or 0)
                            side = p.get("side", "")
                            print(f"  Synced position from exchange: {side} {contracts} BTC @ ${entry:,.2f}")
                except Exception as e:
                    print(f"  Warning: could not sync position: {e}")

            self.base_price = await self.exchange.fetch_price(self.symbol)
            await self._place_grid()

    def _load_historical_pnl(self):
        """Load realized P&L and trade count from historical trade log (P&L only, not position)."""
        try:
            from .logger import load_trades
            trades = load_trades(self.config.trade_log)
            for t in trades:
                if t.get("side") in ("buy", "sell"):
                    self.trade_count += 1
                    self.realized_pnl += t.get("pnl", 0)
            if self.trade_count > 0:
                print(f"  Loaded history: {self.trade_count} trades, P&L: ${self.realized_pnl:+.2f}")
        except Exception as e:
            print(f"  Warning: could not load trade history: {e}")

    def _get_available_margin(self) -> float:
        """Get available margin (free collateral) from the exchange."""
        try:
            balance = self.exchange.get_balance()
            stable = balance.get("USDT", balance.get("USDC", 0.0))

            # For real exchange, try to get free margin from ccxt
            if hasattr(self.exchange, "ccxt_client") and self.config.mode == "real":
                try:
                    bal = self.exchange.ccxt_client.fetch_balance()
                    free = float(bal.get("USDC", {}).get("free", 0) or 0)
                    if free > 0:
                        return free
                    # Fallback: try 'free' at top level
                    free = float(bal.get("free", {}).get("USDC", 0) or 0)
                    if free > 0:
                        return free
                except Exception:
                    pass

            return stable
        except Exception:
            return 0.0

    def _margin_needed_for_order(self, order_usdt: float) -> float:
        """Calculate margin needed for a single order considering leverage."""
        leverage = getattr(self.config, "leverage", 1)
        return order_usdt / leverage

    async def _place_grid(self):
        spacing = self.config.grid_spacing_pct / 100
        levels = self.config.grid_levels
        order_usdt = self.config.order_size_usdt
        leverage = getattr(self.config, "leverage", 1)

        available_margin = self._get_available_margin()
        margin_per_order = self._margin_needed_for_order(order_usdt)

        # Calculate how many orders we can afford (buy + sell share margin)
        # Reserve some margin (10%) for safety
        usable_margin = available_margin * 0.90
        max_orders = int(usable_margin / margin_per_order) if margin_per_order > 0 else 0

        if max_orders == 0:
            print(f"  Warning: No margin available (${available_margin:.2f}). Cannot place grid.")
            return

        # Distribute orders: alternate buy/sell, prioritize closer levels
        buy_count = 0
        sell_count = 0
        orders_placed = 0

        for i in range(1, levels + 1):
            if orders_placed >= max_orders:
                print(f"  Info: Margin limit reached after {orders_placed} orders "
                      f"(margin: ${available_margin:.2f}, per order: ${margin_per_order:.2f})")
                break

            # Buy levels below
            buy_price = round(self.base_price * (1 - spacing * i), 1)
            buy_amount = round(order_usdt / buy_price, 5)

            # Check if we have margin for this order
            if orders_placed < max_orders:
                try:
                    order = await self.exchange.place_limit_buy(self.symbol, buy_amount, buy_price)
                    self.buy_orders[order.id] = {"order": order, "level_price": buy_price}
                    orders_placed += 1
                    buy_count += 1
                except Exception as e:
                    err_str = str(e).lower()
                    if "margin" in err_str or "insufficient" in err_str or "balance" in err_str:
                        # Try with reduced size (50%)
                        reduced_amount = round(buy_amount * 0.5, 5)
                        reduced_usdt = reduced_amount * buy_price
                        if reduced_usdt >= 10:  # minimum order value
                            try:
                                order = await self.exchange.place_limit_buy(
                                    self.symbol, reduced_amount, buy_price
                                )
                                self.buy_orders[order.id] = {"order": order, "level_price": buy_price}
                                orders_placed += 1
                                buy_count += 1
                                print(f"  Info: Reduced buy at ${buy_price:,.2f} to {reduced_amount} BTC (50%)")
                            except Exception as e2:
                                print(f"  Warning: could not place buy at ${buy_price:,.2f} even reduced: {e2}")
                        else:
                            print(f"  Warning: skipping buy at ${buy_price:,.2f} — insufficient margin: {e}")
                    else:
                        print(f"  Warning: could not place buy at ${buy_price:,.2f}: {e}")

            # Sell levels above
            if orders_placed >= max_orders:
                continue

            sell_price = round(self.base_price * (1 + spacing * i), 1)
            sell_amount = round(order_usdt / sell_price, 5)

            try:
                order = await self.exchange.place_limit_sell(self.symbol, sell_amount, sell_price)
                self.sell_orders[order.id] = {"order": order, "level_price": sell_price}
                orders_placed += 1
                sell_count += 1
            except Exception as e:
                err_str = str(e).lower()
                if "margin" in err_str or "insufficient" in err_str or "balance" in err_str:
                    reduced_amount = round(sell_amount * 0.5, 5)
                    reduced_usdt = reduced_amount * sell_price
                    if reduced_usdt >= 10:
                        try:
                            order = await self.exchange.place_limit_sell(
                                self.symbol, reduced_amount, sell_price
                            )
                            self.sell_orders[order.id] = {"order": order, "level_price": sell_price}
                            orders_placed += 1
                            sell_count += 1
                            print(f"  Info: Reduced sell at ${sell_price:,.2f} to {reduced_amount} BTC (50%)")
                        except Exception as e2:
                            print(f"  Warning: could not place sell at ${sell_price:,.2f} even reduced: {e2}")
                    else:
                        print(f"  Warning: skipping sell at ${sell_price:,.2f} — insufficient margin: {e}")
                else:
                    print(f"  Warning: could not place sell at ${sell_price:,.2f}: {e}")

        print(f"  Grid placed: {buy_count} buys, {sell_count} sells "
              f"(margin used: ~${orders_placed * margin_per_order:.2f} of ${available_margin:.2f})")

    async def check_fills(self, current_price: float) -> list[dict]:
        self.last_price = current_price
        if self.paused:
            return []

        filled = []

        if self.config.mode == "real":
            # Real mode: check open orders via exchange API
            filled = await self._check_fills_real(current_price)
            # Retry any pending counter-orders that previously failed
            await self._retry_pending_orders()
        else:
            # Paper mode: simulate fills based on price
            filled = await self._check_fills_paper(current_price)

        self._save_state()
        return filled

    async def _place_counter_order(self, side: str, price: float, amount: float) -> bool:
        """Try to place a counter-order. If margin insufficient, try reduced size or queue.
        Returns True if order was placed, False if queued for retry."""
        original_amount = amount

        for attempt in range(2):  # Try full size, then 50%
            try:
                if side == "sell":
                    new_order = await self.exchange.place_limit_sell(
                        self.symbol, amount, price
                    )
                    self.sell_orders[new_order.id] = {
                        "order": new_order, "level_price": price,
                    }
                else:
                    new_order = await self.exchange.place_limit_buy(
                        self.symbol, amount, price
                    )
                    self.buy_orders[new_order.id] = {
                        "order": new_order, "level_price": price,
                    }
                if attempt > 0:
                    print(f"  Info: Placed reduced counter-{side} at ${price:,.2f} "
                          f"({amount}/{original_amount} BTC)")
                return True
            except Exception as e:
                err_str = str(e).lower()
                is_margin_error = any(kw in err_str for kw in
                                      ["margin", "insufficient", "balance", "not enough"])
                if is_margin_error and attempt == 0:
                    # Try with 50% size
                    amount = round(amount * 0.5, 5)
                    if amount * price < 10:  # too small
                        break
                    continue
                elif is_margin_error:
                    break
                else:
                    # Non-margin error, log and don't retry
                    log_trade({"side": "error", "price": 0, "amount": 0,
                               "fee": 0, "pnl": 0,
                               "mode": f"counter_{side}_error: {e}"},
                              self.config.trade_log)
                    return False

        # Queue for later retry
        self.pending_counter_orders.append({
            "side": side,
            "price": price,
            "amount": original_amount,
            "retries": 0,
            "queued_at": datetime.now(timezone.utc).isoformat(),
        })
        print(f"  Info: Queued counter-{side} at ${price:,.2f} for retry (insufficient margin)")
        log_trade({"side": "info", "price": price, "amount": original_amount,
                   "fee": 0, "pnl": 0,
                   "mode": f"counter_{side}_queued_margin"},
                  self.config.trade_log)
        return False

    async def _retry_pending_orders(self):
        """Retry queued counter-orders that failed due to insufficient margin."""
        if not self.pending_counter_orders:
            return

        still_pending = []
        available_margin = self._get_available_margin()
        margin_per_order = self._margin_needed_for_order(self.config.order_size_usdt)

        for pending in self.pending_counter_orders:
            # Skip if too many retries (max 50 retries = ~4 hours at 5s tick)
            if pending["retries"] >= 50:
                print(f"  Warning: Dropping queued {pending['side']} at ${pending['price']:,.2f} "
                      f"after {pending['retries']} retries")
                log_trade({"side": "info", "price": pending["price"],
                           "amount": pending["amount"], "fee": 0, "pnl": 0,
                           "mode": f"counter_{pending['side']}_dropped_max_retries"},
                          self.config.trade_log)
                continue

            # Check if we have enough margin
            if available_margin < margin_per_order * 0.5:
                pending["retries"] += 1
                still_pending.append(pending)
                continue

            # Try to place
            try:
                if pending["side"] == "sell":
                    new_order = await self.exchange.place_limit_sell(
                        self.symbol, pending["amount"], pending["price"]
                    )
                    self.sell_orders[new_order.id] = {
                        "order": new_order, "level_price": pending["price"],
                    }
                else:
                    new_order = await self.exchange.place_limit_buy(
                        self.symbol, pending["amount"], pending["price"]
                    )
                    self.buy_orders[new_order.id] = {
                        "order": new_order, "level_price": pending["price"],
                    }
                available_margin -= margin_per_order
                print(f"  Info: Retry success — placed {pending['side']} at ${pending['price']:,.2f}")
            except Exception:
                # Try reduced amount
                reduced = round(pending["amount"] * 0.5, 5)
                try:
                    if pending["side"] == "sell":
                        new_order = await self.exchange.place_limit_sell(
                            self.symbol, reduced, pending["price"]
                        )
                        self.sell_orders[new_order.id] = {
                            "order": new_order, "level_price": pending["price"],
                        }
                    else:
                        new_order = await self.exchange.place_limit_buy(
                            self.symbol, reduced, pending["price"]
                        )
                        self.buy_orders[new_order.id] = {
                            "order": new_order, "level_price": pending["price"],
                        }
                    available_margin -= margin_per_order * 0.5
                    print(f"  Info: Retry success (reduced) — {pending['side']} "
                          f"at ${pending['price']:,.2f} ({reduced} BTC)")
                except Exception:
                    pending["retries"] += 1
                    still_pending.append(pending)

        self.pending_counter_orders = still_pending

    async def _check_fills_real(self, current_price: float) -> list[dict]:
        """Check fills via exchange API for real trading."""
        filled = []
        try:
            open_orders = self.exchange.ccxt_client.fetch_open_orders(self.symbol)
            open_ids = {str(o["id"]) for o in open_orders}
        except Exception as e:
            print(f"  Warning: failed to fetch open orders: {e}")
            return filled

        # SAFETY: cancel orphan orders (on exchange but not tracked by bot)
        max_orders = self.config.grid_levels * 2 + 4  # grid + small buffer
        bot_ids = set(self.buy_orders.keys()) | set(self.sell_orders.keys())
        orphan_ids = open_ids - bot_ids
        if len(open_orders) > max_orders and orphan_ids:
            print(f"  Cleaning {len(orphan_ids)} orphan orders (exchange: {len(open_orders)}, bot: {len(bot_ids)})")
            for oid in orphan_ids:
                try:
                    self.exchange.ccxt_client.cancel_order(oid, self.symbol)
                except Exception:
                    pass

        # Check buy orders that are no longer open (= filled)
        for oid, entry in list(self.buy_orders.items()):
            if oid not in open_ids:
                order = entry["order"]
                trade = {
                    "side": "buy",
                    "price": order.price,
                    "amount": order.amount,
                    "fee": order.amount * order.price * 0.0005,
                    "pnl": 0.0,
                    "mode": "real",
                }
                self.total_bought_btc += order.amount
                self.total_spent_usdt += order.amount * order.price
                self.trade_count += 1
                filled.append(trade)
                log_trade(trade, self.config.trade_log)
                del self.buy_orders[oid]

                # Place counter sell with margin and order limit check
                total_bot_orders = len(self.buy_orders) + len(self.sell_orders)
                if total_bot_orders < max_orders:
                    counter_price = round(order.price * (1 + self.config.grid_spacing_pct / 100), 1)
                    counter_amount = round(self.config.order_size_usdt / counter_price, 5)
                    await self._place_counter_order("sell", counter_price, counter_amount)

        # Check sell orders that are no longer open (= filled)
        for oid, entry in list(self.sell_orders.items()):
            if oid not in open_ids:
                order = entry["order"]
                avg_buy = (
                    self.total_spent_usdt / self.total_bought_btc
                    if self.total_bought_btc > 0 else order.price
                )
                fee = order.amount * order.price * 0.0005
                pnl = (order.price - avg_buy) * order.amount - fee

                trade = {
                    "side": "sell",
                    "price": order.price,
                    "amount": order.amount,
                    "fee": fee,
                    "pnl": pnl,
                    "mode": "real",
                }
                self.total_sold_btc += order.amount
                self.total_received_usdt += order.amount * order.price
                self.realized_pnl += pnl
                self.trade_count += 1
                filled.append(trade)
                log_trade(trade, self.config.trade_log)
                del self.sell_orders[oid]

                # Place counter buy with margin and order limit check
                total_bot_orders = len(self.buy_orders) + len(self.sell_orders)
                if total_bot_orders < max_orders:
                    counter_price = round(order.price * (1 - self.config.grid_spacing_pct / 100), 1)
                    counter_amount = round(self.config.order_size_usdt / counter_price, 5)
                    await self._place_counter_order("buy", counter_price, counter_amount)

        return filled

    async def _check_fills_paper(self, current_price: float) -> list[dict]:
        """Simulate fills based on price for paper trading."""
        filled = []

        # Check buy fills: price dropped to or below order price
        for oid, entry in list(self.buy_orders.items()):
            order: Order = entry["order"]
            if current_price <= order.price:
                fee = self.exchange.execute_fill(order)
                trade = {
                    "side": "buy",
                    "price": order.price,
                    "amount": order.amount,
                    "fee": fee,
                    "pnl": 0.0,
                    "mode": self.config.mode,
                }
                self.total_bought_btc += order.amount
                self.total_spent_usdt += order.amount * order.price
                self.trade_count += 1
                filled.append(trade)
                log_trade(trade, self.config.trade_log)
                del self.buy_orders[oid]

                counter_price = round(order.price * (1 + self.config.grid_spacing_pct / 100), 2)
                counter_amount = self.config.order_size_usdt / counter_price
                new_order = await self.exchange.place_limit_sell(
                    self.symbol, counter_amount, counter_price
                )
                self.sell_orders[new_order.id] = {
                    "order": new_order, "level_price": counter_price,
                }

        # Check sell fills: price rose to or above order price
        for oid, entry in list(self.sell_orders.items()):
            order: Order = entry["order"]
            if current_price >= order.price:
                fee = self.exchange.execute_fill(order)
                avg_buy = (
                    self.total_spent_usdt / self.total_bought_btc
                    if self.total_bought_btc > 0 else order.price
                )
                pnl = (order.price - avg_buy) * order.amount - fee
                trade = {
                    "side": "sell",
                    "price": order.price,
                    "amount": order.amount,
                    "fee": fee,
                    "pnl": pnl,
                    "mode": self.config.mode,
                }
                self.total_sold_btc += order.amount
                self.total_received_usdt += order.amount * order.price
                self.realized_pnl += pnl
                self.trade_count += 1
                filled.append(trade)
                log_trade(trade, self.config.trade_log)
                del self.sell_orders[oid]

                counter_price = round(order.price * (1 - self.config.grid_spacing_pct / 100), 2)
                counter_amount = self.config.order_size_usdt / counter_price
                new_order = await self.exchange.place_limit_buy(
                    self.symbol, counter_amount, counter_price
                )
                self.buy_orders[new_order.id] = {
                    "order": new_order, "level_price": counter_price,
                }

        return filled

    def get_position_btc(self) -> float:
        return self.total_bought_btc - self.total_sold_btc

    def get_avg_entry(self) -> float:
        if self.total_bought_btc == 0:
            return 0.0
        return self.total_spent_usdt / self.total_bought_btc

    def get_unrealized_pnl(self) -> float:
        position = self.get_position_btc()
        if position <= 0 or self.last_price == 0:
            return 0.0
        avg_entry = self.get_avg_entry()
        return (self.last_price - avg_entry) * position

    async def cancel_all(self):
        for oid in list(self.buy_orders.keys()):
            await self.exchange.cancel_order(oid)
        for oid in list(self.sell_orders.keys()):
            await self.exchange.cancel_order(oid)
        self.buy_orders.clear()
        self.sell_orders.clear()

    async def reset(self):
        await self.cancel_all()
        self.total_bought_btc = 0.0
        self.total_spent_usdt = 0.0
        self.total_sold_btc = 0.0
        self.total_received_usdt = 0.0
        self.realized_pnl = 0.0
        self.trade_count = 0
        self.base_price = await self.exchange.fetch_price(self.symbol)
        await self._place_grid()
        self._save_state()

    async def market_sell_all(self, price: float) -> dict | None:
        position = self.get_position_btc()
        if abs(position) < 0.000001:
            return None
        await self.cancel_all()

        fee = 0.0
        # REAL MODE: close position on exchange
        if self.config.mode == "real" and hasattr(self.exchange, "ccxt_client"):
            try:
                # Check real position from exchange
                positions = self.exchange.ccxt_client.fetch_positions([self.symbol])
                for p in positions:
                    contracts = float(p.get("contracts", 0) or 0)
                    if contracts > 0:
                        side = p.get("side", "")
                        close_price = round(price * 0.995, 1) if side == "long" else round(price * 1.005, 1)
                        if side == "long":
                            self.exchange.ccxt_client.create_limit_sell_order(
                                self.symbol, contracts, close_price, {"reduceOnly": True}
                            )
                        else:
                            self.exchange.ccxt_client.create_limit_buy_order(
                                self.symbol, contracts, close_price, {"reduceOnly": True}
                            )
                        fee = contracts * price * 0.0005
            except Exception as e:
                print(f"  WARNING: Failed to close position on exchange: {e}")
        elif hasattr(self.exchange, "execute_fill"):
            # Paper mode
            order = Order("market-sell", "sell", abs(position), price)
            fee = self.exchange.execute_fill(order)

        # Update internal tracking
        avg_entry = self.get_avg_entry()
        if position > 0:
            pnl = (price - avg_entry) * position - fee
        else:
            pnl = (avg_entry - price) * abs(position) - fee

        trade = {
            "side": "sell" if position > 0 else "buy",
            "price": price,
            "amount": abs(position),
            "fee": fee,
            "pnl": pnl,
            "mode": self.config.mode,
        }
        self.total_sold_btc += abs(position) if position > 0 else 0
        self.total_bought_btc += abs(position) if position < 0 else 0
        self.total_received_usdt += abs(position) * price if position > 0 else 0
        self.total_spent_usdt += abs(position) * price if position < 0 else 0
        self.realized_pnl += pnl
        self.trade_count += 1
        log_trade(trade, self.config.trade_log)
        return trade

    def get_status_text(self) -> str:
        position = self.get_position_btc()
        unrealized = self.get_unrealized_pnl()
        balance = self.exchange.get_balance()
        # Support both USDT and USDC balance keys
        stable = balance.get("USDT", balance.get("USDC", 0.0))
        stable_name = "USDC" if "USDC" in balance else "USDT"
        leverage = getattr(self.config, "leverage", 1)
        return (
            f"Mode: {self.config.mode}\n"
            f"Exchange: {getattr(self.config, 'exchange', 'binance')}\n"
            f"Leverage: {leverage}x\n"
            f"Price: ${self.last_price:,.2f}\n"
            f"Base: ${self.base_price:,.2f}\n"
            f"Buys open: {len(self.buy_orders)}\n"
            f"Sells open: {len(self.sell_orders)}\n"
            f"Position: {position:.6f} BTC\n"
            f"Avg entry: ${self.get_avg_entry():,.2f}\n"
            f"Realized P&L: ${self.realized_pnl:,.2f}\n"
            f"Unrealized P&L: ${unrealized:,.2f}\n"
            f"Trades: {self.trade_count}\n"
            f"Pending retries: {len(self.pending_counter_orders)}\n"
            f"Balance: {stable:.2f} {stable_name} | {balance.get('BTC', 0.0):.6f} BTC\n"
            f"Paused: {self.paused}"
        )

    def _save_state(self):
        state = {
            "base_price": self.base_price,
            "last_price": self.last_price,
            "total_bought_btc": self.total_bought_btc,
            "total_spent_usdt": self.total_spent_usdt,
            "total_sold_btc": self.total_sold_btc,
            "total_received_usdt": self.total_received_usdt,
            "realized_pnl": self.realized_pnl,
            "trade_count": self.trade_count,
            "paused": self.paused,
            "buy_orders": {
                oid: {"order": e["order"].to_dict(), "level_price": e["level_price"]}
                for oid, e in self.buy_orders.items()
            },
            "sell_orders": {
                oid: {"order": e["order"].to_dict(), "level_price": e["level_price"]}
                for oid, e in self.sell_orders.items()
            },
            "balance": self.exchange.get_balance(),
            "pending_counter_orders": self.pending_counter_orders,
        }
        os.makedirs(os.path.dirname(self.config.state_file), exist_ok=True)
        with open(self.config.state_file, "w") as f:
            json.dump(state, f, indent=2)

    def _load_state(self) -> dict | None:
        if not os.path.exists(self.config.state_file):
            return None
        with open(self.config.state_file, "r") as f:
            return json.load(f)

    def _restore_state(self, state: dict):
        self.base_price = state["base_price"]
        self.last_price = state.get("last_price", 0.0)
        self.total_bought_btc = state["total_bought_btc"]
        self.total_spent_usdt = state["total_spent_usdt"]
        self.total_sold_btc = state["total_sold_btc"]
        self.total_received_usdt = state["total_received_usdt"]
        self.realized_pnl = state["realized_pnl"]
        self.trade_count = state.get("trade_count", 0)
        self.paused = state.get("paused", False)
        self.pending_counter_orders = state.get("pending_counter_orders", [])

        for oid, entry in state.get("buy_orders", {}).items():
            order = Order.from_dict(entry["order"])
            self.buy_orders[oid] = {"order": order, "level_price": entry["level_price"]}
        for oid, entry in state.get("sell_orders", {}).items():
            order = Order.from_dict(entry["order"])
            self.sell_orders[oid] = {"order": order, "level_price": entry["level_price"]}

        # Restore paper exchange balance
        if "balance" in state and hasattr(self.exchange, "balance"):
            self.exchange.balance = state["balance"]
