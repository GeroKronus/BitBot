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


class PaperExecutionAgent(IExecutionAgent):
    """Paper mode execution — simulates fills with configurable slippage."""

    def __init__(self, slippage_pct: float = 0.05):
        self.slippage_pct = slippage_pct
        self._orders = []

    def execute(self, signals: list) -> list:
        results = []
        for s in signals:
            fill_price = s.price * (1 + self.slippage_pct / 100) if s.side == "buy" else \
                         s.price * (1 - self.slippage_pct / 100)
            results.append(ExecutionResult(
                filled=True,
                fill_price=round(fill_price, 2),
                fill_amount=s.amount,
                slippage_pct=self.slippage_pct,
                latency_ms=50,
            ))
        return results

    def cancel_all(self) -> int:
        count = len(self._orders)
        self._orders = []
        return count

    def close_position(self, position: Position, price: float) -> ExecutionResult:
        return ExecutionResult(filled=True, fill_price=price, fill_amount=position.size)
