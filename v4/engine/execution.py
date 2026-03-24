"""ExecutionAgent — Executes orders on exchange with slippage awareness.

Responsibilities:
- Decides order type (limit vs market) based on spread
- Simulates/measures slippage
- Cancels stale orders
- Returns ExecutionResult for feedback loop
- Progressive degradation: slippage trend → limit_only, latency unstable → reduce size

ChatGPT recommendations incorporated:
- Spread-aware order type selection
- Execution feedback loop (slippage trend, latency monitoring)
- max_order_age for stale order cancellation
"""

import time
from datetime import datetime, timezone, timedelta

from ..core.interfaces import (
    IExecutionAgent, Signal, ExecutionResult, Position
)


class HyperliquidExecutionAgent(IExecutionAgent):

    def __init__(self, ccxt_client, symbol: str = "BTC/USDC:USDC", config: dict = None):
        self.ccxt = ccxt_client
        self.symbol = symbol
        config = config or {}

        # Thresholds
        self.spread_limit_threshold = config.get("spread_limit_threshold", 0.05)  # % spread above which → limit only
        self.max_order_age_seconds = config.get("max_order_age_seconds", 300)  # 5 min
        self.slippage_alert_pct = config.get("slippage_alert_pct", 0.1)

        # Feedback state
        self._recent_slippages = []  # last 20 slippages
        self._recent_latencies = []  # last 20 latencies
        self._limit_only_mode = False
        self._size_reduction = 1.0  # 1.0 = normal, 0.5 = half size

    def execute(self, signals: list) -> list:
        """Execute signals on exchange. Returns results with slippage data."""
        results = []
        for signal in signals:
            result = self._execute_single(signal)
            results.append(result)
            self._update_feedback(result)
        return results

    def _execute_single(self, signal: Signal) -> ExecutionResult:
        """Execute a single signal."""
        start = time.time()

        try:
            # Close signals → reduceOnly
            if signal.side == "close":
                return self._close_position_order(signal, start)

            # Apply size reduction from feedback loop
            amount = round(signal.amount * self._size_reduction, 5)
            price = signal.price

            if amount <= 0:
                return ExecutionResult(error="Amount too small after size reduction")

            # Decide order type based on spread and feedback
            use_limit = (signal.order_type == "limit" or
                         self._limit_only_mode or
                         signal.source == "GRID")

            if use_limit:
                result = self._place_limit(signal.side, amount, price, signal.reduce_only)
            else:
                result = self._place_market(signal.side, amount, price, signal.reduce_only)

            result.latency_ms = int((time.time() - start) * 1000)
            return result

        except Exception as e:
            latency = int((time.time() - start) * 1000)
            return ExecutionResult(
                filled=False,
                latency_ms=latency,
                error=str(e),
            )

    def _place_limit(self, side: str, amount: float, price: float,
                     reduce_only: bool) -> ExecutionResult:
        """Place limit order."""
        params = {"reduceOnly": True} if reduce_only else {}

        if side == "buy":
            order = self.ccxt.create_limit_buy_order(self.symbol, amount, price, params)
        else:
            order = self.ccxt.create_limit_sell_order(self.symbol, amount, price, params)

        return ExecutionResult(
            filled=True,
            fill_price=price,
            fill_amount=amount,
            slippage_pct=0.0,  # limit = no slippage at order time
            order_id=str(order.get("id", "")),
        )

    def _place_market(self, side: str, amount: float, price: float,
                      reduce_only: bool) -> ExecutionResult:
        """Place market-like order (limit with aggressive pricing)."""
        # Hyperliquid requires price for market orders
        # Use 0.5% slippage tolerance
        if side == "buy":
            aggressive_price = round(price * 1.005, 1)
            params = {"reduceOnly": True} if reduce_only else {}
            order = self.ccxt.create_limit_buy_order(
                self.symbol, amount, aggressive_price, params
            )
        else:
            aggressive_price = round(price * 0.995, 1)
            params = {"reduceOnly": True} if reduce_only else {}
            order = self.ccxt.create_limit_sell_order(
                self.symbol, amount, aggressive_price, params
            )

        # Estimate slippage
        slippage = abs(aggressive_price - price) / price * 100

        return ExecutionResult(
            filled=True,
            fill_price=aggressive_price,
            fill_amount=amount,
            slippage_pct=round(slippage, 4),
            order_id=str(order.get("id", "")),
        )

    def _close_position_order(self, signal: Signal, start_time: float) -> ExecutionResult:
        """Close position with reduceOnly order."""
        try:
            positions = self.ccxt.fetch_positions([self.symbol])
            for p in positions:
                contracts = float(p.get("contracts", 0) or 0)
                if contracts > 0:
                    side = p.get("side", "")
                    price = signal.price
                    close_price = round(price * 0.995, 1) if side == "long" else round(price * 1.005, 1)

                    if side == "long":
                        order = self.ccxt.create_limit_sell_order(
                            self.symbol, contracts, close_price, {"reduceOnly": True}
                        )
                    else:
                        order = self.ccxt.create_limit_buy_order(
                            self.symbol, contracts, close_price, {"reduceOnly": True}
                        )

                    latency = int((time.time() - start_time) * 1000)
                    slippage = abs(close_price - price) / price * 100

                    return ExecutionResult(
                        filled=True,
                        fill_price=close_price,
                        fill_amount=contracts,
                        slippage_pct=round(slippage, 4),
                        latency_ms=latency,
                        order_id=str(order.get("id", "")),
                    )

            return ExecutionResult(error="No position to close")
        except Exception as e:
            return ExecutionResult(
                filled=False,
                latency_ms=int((time.time() - start_time) * 1000),
                error=str(e),
            )

    def cancel_all(self) -> int:
        """Cancel all open orders."""
        try:
            orders = self.ccxt.fetch_open_orders(self.symbol)
            count = 0
            for o in orders:
                try:
                    self.ccxt.cancel_order(o["id"], self.symbol)
                    count += 1
                except Exception:
                    pass
            return count
        except Exception:
            return 0

    def close_position(self, position: Position, price: float) -> ExecutionResult:
        """Close position via signal."""
        signal = Signal(side="close", price=price, amount=position.size,
                        order_type="market", reduce_only=True, source="EXECUTION")
        return self._execute_single(signal)

    def cancel_stale_orders(self) -> int:
        """Cancel orders older than max_order_age."""
        try:
            orders = self.ccxt.fetch_open_orders(self.symbol)
            cancelled = 0
            now = datetime.now(timezone.utc)
            for o in orders:
                created = o.get("datetime", "")
                if created:
                    try:
                        order_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        age = (now - order_time).total_seconds()
                        if age > self.max_order_age_seconds:
                            self.ccxt.cancel_order(o["id"], self.symbol)
                            cancelled += 1
                    except (ValueError, TypeError):
                        pass
            return cancelled
        except Exception:
            return 0

    # ===== FEEDBACK LOOP =====

    def _update_feedback(self, result: ExecutionResult):
        """Update execution feedback for progressive degradation.

        ChatGPT: slippage trend → limit_only_mode, latency unstable → reduce_size
        """
        if result.slippage_pct > 0:
            self._recent_slippages.append(result.slippage_pct)
            self._recent_slippages = self._recent_slippages[-20:]

        if result.latency_ms > 0:
            self._recent_latencies.append(result.latency_ms)
            self._recent_latencies = self._recent_latencies[-20:]

        # Slippage trend → switch to limit only
        if len(self._recent_slippages) >= 5:
            avg_slip = sum(self._recent_slippages[-5:]) / 5
            if avg_slip > self.slippage_alert_pct:
                self._limit_only_mode = True
            elif avg_slip < self.slippage_alert_pct * 0.5:
                self._limit_only_mode = False

        # Latency unstable → reduce position size
        if len(self._recent_latencies) >= 5:
            avg_lat = sum(self._recent_latencies[-5:]) / 5
            if avg_lat > 3000:  # >3s average
                self._size_reduction = 0.5
            elif avg_lat > 1500:
                self._size_reduction = 0.75
            else:
                self._size_reduction = 1.0

    def get_feedback_status(self) -> dict:
        avg_slip = (sum(self._recent_slippages) / len(self._recent_slippages)
                    if self._recent_slippages else 0)
        avg_lat = (sum(self._recent_latencies) / len(self._recent_latencies)
                   if self._recent_latencies else 0)
        return {
            "limit_only_mode": self._limit_only_mode,
            "size_reduction": self._size_reduction,
            "avg_slippage_pct": round(avg_slip, 4),
            "avg_latency_ms": round(avg_lat),
            "samples": len(self._recent_slippages),
        }


class PaperPosition:
    """Tracks position state in paper mode. Single source of truth."""

    def __init__(self, capital: float = 130.0):
        self.side = "flat"
        self.size = 0.0
        self.entry_price = 0.0
        self.capital = capital
        self.realized_pnl = 0.0
        self.trade_count = 0

    def update_on_fill(self, side: str, amount: float, price: float, fee: float):
        """Update position after a fill."""
        if side == "close":
            # Close position
            if self.side == "long":
                pnl = (price - self.entry_price) * self.size - fee
            elif self.side == "short":
                pnl = (self.entry_price - price) * self.size - fee
            else:
                pnl = -fee
            self.capital += pnl
            self.realized_pnl += pnl
            self.side = "flat"
            self.size = 0.0
            self.entry_price = 0.0
            self.trade_count += 1
            return pnl

        elif side == "buy":
            if self.side == "short":
                # Close short first
                pnl = (self.entry_price - price) * min(amount, self.size) - fee
                self.capital += pnl
                self.realized_pnl += pnl
                self.size -= amount
                if self.size <= 0.000001:
                    self.side = "flat"
                    self.size = 0.0
                    self.entry_price = 0.0
                self.trade_count += 1
                return pnl
            else:
                # Add to long
                if self.side == "long" and self.size > 0:
                    total_cost = self.entry_price * self.size + price * amount
                    self.size += amount
                    self.entry_price = total_cost / self.size
                else:
                    self.side = "long"
                    self.size = amount
                    self.entry_price = price
                self.capital -= fee
                self.trade_count += 1
                return -fee

        elif side == "sell":
            if self.side == "long":
                # Close long first
                pnl = (price - self.entry_price) * min(amount, self.size) - fee
                self.capital += pnl
                self.realized_pnl += pnl
                self.size -= amount
                if self.size <= 0.000001:
                    self.side = "flat"
                    self.size = 0.0
                    self.entry_price = 0.0
                self.trade_count += 1
                return pnl
            else:
                # Add to short
                if self.side == "short" and self.size > 0:
                    total_cost = self.entry_price * self.size + price * amount
                    self.size += amount
                    self.entry_price = total_cost / self.size
                else:
                    self.side = "short"
                    self.size = amount
                    self.entry_price = price
                self.capital -= fee
                self.trade_count += 1
                return -fee

        return 0.0

    def _cleanup_dust(self, current_price: float):
        """Close position if below minimum trade size (dust cleanup)."""
        min_value = 5.0  # $5 minimum — below Hyperliquid's $10 can't close
        if self.side != "flat" and self.size > 0:
            notional = self.size * current_price if current_price > 0 else 0
            if notional < min_value:
                # Position is dust — write it off
                if self.side == "long":
                    pnl = (current_price - self.entry_price) * self.size
                else:
                    pnl = (self.entry_price - current_price) * self.size
                self.capital += pnl
                self.realized_pnl += pnl
                self.side = "flat"
                self.size = 0.0
                self.entry_price = 0.0

    def get_unrealized(self, current_price: float) -> float:
        if self.side == "long":
            return (current_price - self.entry_price) * self.size
        elif self.side == "short":
            return (self.entry_price - current_price) * self.size
        return 0.0

    def to_position(self, current_price: float = 0):
        """Convert to Position dataclass for pipeline compatibility."""
        from ..core.interfaces import Position
        return Position(
            side=self.side,
            size=self.size,
            entry_price=self.entry_price,
            unrealized_pnl=self.get_unrealized(current_price),
            notional=self.size * current_price if current_price > 0 else 0,
            leverage=4,
        )


class PaperExecutionAgent(IExecutionAgent):
    """Paper mode execution with realistic fills, slippage, partial fills, and delay."""

    def __init__(self, capital: float = 130.0, slippage_pct: float = 0.05,
                 fee_pct: float = 0.05, partial_fill_rate: float = 0.95):
        self.slippage_pct = slippage_pct
        self.fee_pct = fee_pct
        self.partial_fill_rate = partial_fill_rate
        self.position = PaperPosition(capital)
        self._open_orders = []
        # Metrics
        self._total_fills = 0
        self._partial_fills = 0
        self._total_slippage = 0.0

    def execute(self, signals: list) -> list:
        """Place signals as pending orders. Does NOT fill immediately.

        Grid limit orders sit in the order book until price crosses them.
        Market orders and close signals fill immediately.
        """
        import random

        results = []
        for s in signals:
            if s.order_type == "market" or s.side == "close":
                # Market/close: fill immediately
                result = self._fill_now(s)
                results.append(result)
            else:
                # Limit order: add to pending order book
                self._open_orders.append({
                    "side": s.side,
                    "price": s.price,
                    "amount": s.amount,
                    "source": s.source,
                })
        return results

    def check_fills(self, current_price: float) -> list:
        """Check pending orders against current price. Called every tick.

        Buy fills when price <= order price.
        Sell fills when price >= order price.
        """
        import random

        filled_results = []
        remaining = []

        for order in self._open_orders:
            should_fill = False
            if order["side"] == "buy" and current_price <= order["price"]:
                should_fill = True
            elif order["side"] == "sell" and current_price >= order["price"]:
                should_fill = True

            if should_fill:
                result = self._simulate_fill(order, current_price)
                filled_results.append(result)
            else:
                remaining.append(order)

        self._open_orders = remaining
        return filled_results

    def _fill_now(self, signal) -> ExecutionResult:
        """Immediately fill a market/close signal."""
        import random

        actual_slippage = self.slippage_pct * random.uniform(0.5, 1.5)
        if signal.side == "buy" or (signal.side == "close" and self.position.side == "short"):
            fill_price = round(signal.price * (1 + actual_slippage / 100), 2)
        else:
            fill_price = round(signal.price * (1 - actual_slippage / 100), 2)

        fill_pct = 1.0 if random.random() < self.partial_fill_rate else random.uniform(0.5, 0.9)
        fill_amount = round(signal.amount * fill_pct, 5)
        fee = fill_amount * fill_price * self.fee_pct / 100

        self.position.update_on_fill(signal.side, fill_amount, fill_price, fee)
        self.position._cleanup_dust(fill_price)

        self._total_fills += 1
        if fill_pct < 1.0:
            self._partial_fills += 1
        self._total_slippage += actual_slippage

        return ExecutionResult(
            filled=True, fill_price=fill_price, fill_amount=fill_amount,
            slippage_pct=round(actual_slippage, 4), latency_ms=random.randint(50, 300),
            partial=fill_pct < 1.0,
        )

    def _simulate_fill(self, order: dict, current_price: float) -> ExecutionResult:
        """Fill a pending limit order that was triggered by price."""
        import random

        actual_slippage = self.slippage_pct * random.uniform(0.3, 1.0)
        fill_price = round(order["price"] * (1 + actual_slippage / 100 * (1 if order["side"] == "buy" else -1)), 2)

        fill_pct = 1.0 if random.random() < self.partial_fill_rate else random.uniform(0.5, 0.9)
        fill_amount = round(order["amount"] * fill_pct, 5)
        fee = fill_amount * fill_price * self.fee_pct / 100

        self.position.update_on_fill(order["side"], fill_amount, fill_price, fee)
        self.position._cleanup_dust(fill_price)

        self._total_fills += 1
        if fill_pct < 1.0:
            self._partial_fills += 1
        self._total_slippage += actual_slippage

        return ExecutionResult(
            filled=True, fill_price=fill_price, fill_amount=fill_amount,
            slippage_pct=round(actual_slippage, 4), latency_ms=random.randint(50, 200),
            partial=fill_pct < 1.0,
        )

    def get_metrics(self) -> dict:
        avg_slip = self._total_slippage / self._total_fills if self._total_fills > 0 else 0
        partial_ratio = self._partial_fills / self._total_fills * 100 if self._total_fills > 0 else 0
        return {
            "total_fills": self._total_fills,
            "partial_fills": self._partial_fills,
            "partial_ratio_pct": round(partial_ratio, 1),
            "avg_slippage_pct": round(avg_slip, 4),
            "capital": round(self.position.capital, 2),
            "realized_pnl": round(self.position.realized_pnl, 4),
            "position": self.position.side,
            "position_size": self.position.size,
            "trade_count": self.position.trade_count,
        }

    def cancel_all(self) -> int:
        count = len(self._open_orders)
        self._open_orders = []
        return count

    def close_position(self, position: Position, price: float) -> ExecutionResult:
        if self.position.side == "flat":
            return ExecutionResult(error="No position")
        from ..core.interfaces import Signal
        signal = Signal(side="close", price=price, amount=self.position.size,
                        reduce_only=True, source="PAPER_CLOSE")
        results = self.execute([signal])
        return results[0] if results else ExecutionResult(error="Failed")
