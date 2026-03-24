"""BacktestEngine — Replay historical candles with slippage and latency simulation.

Runs the full pipeline (Features → Regime → Orchestrator → Risk → Execution)
against historical data to validate strategy changes before deploy.

Supports:
- Deterministic replay of OHLCV candles
- Configurable slippage and latency
- Tracks: PnL, drawdown, win rate, profit factor, Sharpe
- Outputs trade log for comparison with live results
"""

import math
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from ..core.interfaces import (
    Features, RegimeState, Position, GovernorDecision, Signal, ExecutionResult
)
from ..agents.feature_engine import FeatureEngine
from ..agents.regime import RegimeAgent
from ..strategies.grid import GridStrategy
from ..strategies.trend import TrendStrategy
from ..strategies.no_trade import NoTradeStrategy
from ..engine.orchestrator import StrategyOrchestrator
from ..engine.risk import RiskEngine


@dataclass
class BacktestTrade:
    timestamp: str
    side: str
    price: float
    amount: float
    pnl: float = 0.0
    source: str = ""
    slippage: float = 0.0


@dataclass
class BacktestResult:
    # Performance
    total_pnl: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0

    # Risk
    max_drawdown_pct: float = 0.0
    max_drawdown_usd: float = 0.0
    sharpe_ratio: float = 0.0
    max_exposure_pct: float = 0.0

    # Regime
    time_in_range_pct: float = 0.0
    time_in_trend_pct: float = 0.0
    time_in_chaos_pct: float = 0.0
    regime_changes: int = 0

    # Strategy
    grid_trades: int = 0
    trend_trades: int = 0
    grid_pnl: float = 0.0
    trend_pnl: float = 0.0

    # Meta
    total_candles: int = 0
    start_date: str = ""
    end_date: str = ""
    initial_capital: float = 0.0
    final_capital: float = 0.0

    # Detail
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)
    daily_pnl: dict = field(default_factory=dict)


class BacktestEngine:
    """Replays candles through the full v4 pipeline."""

    def __init__(self, config: dict):
        """
        Config keys:
            initial_capital: float (default 130)
            slippage_pct: float (default 0.05)
            fee_pct: float (default 0.05) — taker + maker avg
            grid_levels: int
            order_size_usdt: float
            leverage: int
        """
        self.initial_capital = config.get("initial_capital", 130.0)
        self.slippage_pct = config.get("slippage_pct", 0.05)
        self.fee_pct = config.get("fee_pct", 0.05)

        # Initialize components
        self.feature_engine = FeatureEngine()
        self.regime_agent = RegimeAgent()
        self.risk_engine = RiskEngine({
            "capital": self.initial_capital,
            "max_exposure_normal_pct": 80,
            "max_exposure_high_vol_pct": 50,
        })

        grid_config = {
            "grid_levels": config.get("grid_levels", 5),
            "order_size_usdt": config.get("order_size_usdt", 20),
        }
        trend_config = {
            "trend_size_pct": config.get("trend_size_pct", 15),
        }

        self.orchestrator = StrategyOrchestrator({
            "RANGE": GridStrategy(grid_config),
            "TREND": TrendStrategy(trend_config),
            "NO_TRADE": NoTradeStrategy(),
        })

        # Simulation state
        self._capital = self.initial_capital
        self._position = Position()
        self._regime_state = RegimeState()
        self._governor = GovernorDecision()
        self._peak_capital = self.initial_capital
        self._daily_returns = []

    def run(self, candles: list) -> BacktestResult:
        """Run backtest on list of OHLCV candle dicts.

        Each candle: {timestamp, open, high, low, close, volume}
        Minimum 50 candles needed for indicators.
        """
        result = BacktestResult(
            initial_capital=self.initial_capital,
            total_candles=len(candles),
        )

        if len(candles) < 50:
            return result

        result.start_date = str(candles[0].get("timestamp", ""))
        result.end_date = str(candles[-1].get("timestamp", ""))

        # Regime time tracking
        regime_ticks = {"RANGE": 0, "TREND_STRONG": 0, "TREND_WEAK": 0,
                        "BREAKOUT": 0, "CHAOS": 0}
        prev_regime = "RANGE"
        last_daily_capital = self.initial_capital
        current_day = ""

        # Process each candle
        for i in range(50, len(candles)):
            candle_window = candles[max(0, i - 72):i + 1]
            current = candles[i]
            price = current["close"]
            ts = str(current.get("timestamp", i))

            # Day tracking
            day = ts[:10] if len(ts) >= 10 else str(i)
            if day != current_day:
                if current_day:
                    daily_ret = self._capital - last_daily_capital
                    self._daily_returns.append(daily_ret)
                    result.daily_pnl[current_day] = round(daily_ret, 4)
                current_day = day
                last_daily_capital = self._capital

            # 1. Compute features
            from ..core.interfaces import MarketSnapshot
            snapshot = MarketSnapshot(
                timestamp=datetime.now(timezone.utc),
                price=price,
                bid=price,
                ask=price,
                spread=price * 0.001,
            )
            features = self.feature_engine.compute(snapshot, candle_window)

            # 2. Detect regime
            self._regime_state = self.regime_agent.detect(features, self._regime_state)
            regime_ticks[self._regime_state.current] = regime_ticks.get(self._regime_state.current, 0) + 1
            if self._regime_state.current != prev_regime:
                result.regime_changes += 1
                prev_regime = self._regime_state.current

            # 3. Update position valuation
            if self._position.side != "flat" and self._position.size > 0:
                if self._position.side == "long":
                    self._position.unrealized_pnl = (price - self._position.entry_price) * self._position.size
                else:
                    self._position.unrealized_pnl = (self._position.entry_price - price) * self._position.size
                self._position.notional = self._position.size * price

            # 4. Generate signals
            signals = self.orchestrator.select_and_run(
                features, self._regime_state, self._position, self._governor
            )

            # 5. Risk filter
            approved = self.risk_engine.evaluate(
                signals, self._position, features, self._regime_state, self._governor
            )

            # 6. Simulate execution
            for signal in approved:
                trade = self._simulate_execution(signal, price, ts)
                if trade:
                    result.trades.append(trade)

            # Track equity
            equity = self._capital + (self._position.unrealized_pnl if self._position.side != "flat" else 0)
            result.equity_curve.append(round(equity, 2))

            # Track drawdown
            if equity > self._peak_capital:
                self._peak_capital = equity
            dd = (self._peak_capital - equity) / self._peak_capital * 100 if self._peak_capital > 0 else 0
            if dd > result.max_drawdown_pct:
                result.max_drawdown_pct = round(dd, 2)
                result.max_drawdown_usd = round(self._peak_capital - equity, 2)

            # Track max exposure
            exp = (self._position.notional / self._capital * 100) if self._capital > 0 else 0
            if exp > result.max_exposure_pct:
                result.max_exposure_pct = round(exp, 1)

        # Final daily return
        if current_day:
            daily_ret = self._capital - last_daily_capital
            self._daily_returns.append(daily_ret)
            result.daily_pnl[current_day] = round(daily_ret, 4)

        # Compute final metrics
        result.final_capital = round(self._capital, 2)
        result.total_pnl = round(self._capital - self.initial_capital, 2)
        self._compute_metrics(result)

        # Regime time
        total_ticks = sum(regime_ticks.values()) or 1
        result.time_in_range_pct = round((regime_ticks.get("RANGE", 0) + regime_ticks.get("TREND_WEAK", 0)) / total_ticks * 100, 1)
        result.time_in_trend_pct = round((regime_ticks.get("TREND_STRONG", 0) + regime_ticks.get("BREAKOUT", 0)) / total_ticks * 100, 1)
        result.time_in_chaos_pct = round(regime_ticks.get("CHAOS", 0) / total_ticks * 100, 1)

        return result

    def _simulate_execution(self, signal: Signal, market_price: float, ts: str):
        """Simulate order execution with slippage and fees."""

        if signal.side == "close":
            if self._position.side == "flat":
                return None
            # Close position
            slip = market_price * self.slippage_pct / 100
            if self._position.side == "long":
                fill_price = market_price - slip
                pnl = (fill_price - self._position.entry_price) * self._position.size
            else:
                fill_price = market_price + slip
                pnl = (self._position.entry_price - fill_price) * self._position.size

            fee = self._position.size * fill_price * self.fee_pct / 100
            net_pnl = pnl - fee
            self._capital += net_pnl

            source = signal.source
            trade = BacktestTrade(
                timestamp=ts, side="close", price=round(fill_price, 2),
                amount=self._position.size, pnl=round(net_pnl, 4),
                source=source, slippage=self.slippage_pct,
            )
            self._position = Position()
            return trade

        elif signal.side == "buy":
            if self._position.side == "short":
                return None  # don't flip, need close first

            slip = market_price * self.slippage_pct / 100
            fill_price = market_price + slip
            fee = signal.amount * fill_price * self.fee_pct / 100
            cost = signal.amount * fill_price + fee

            if cost > self._capital * 0.9:
                return None  # not enough capital

            # Update position (add to existing or new)
            if self._position.side == "long":
                total_cost = (self._position.entry_price * self._position.size +
                              fill_price * signal.amount)
                total_size = self._position.size + signal.amount
                self._position.entry_price = total_cost / total_size if total_size > 0 else fill_price
                self._position.size = total_size
            else:
                self._position = Position(
                    side="long", size=signal.amount,
                    entry_price=fill_price, leverage=4,
                )

            self._position.notional = self._position.size * fill_price
            self._capital -= fee

            return BacktestTrade(
                timestamp=ts, side="buy", price=round(fill_price, 2),
                amount=signal.amount, pnl=round(-fee, 4),
                source=signal.source, slippage=self.slippage_pct,
            )

        elif signal.side == "sell":
            if self._position.side == "long" and signal.reduce_only:
                # Partial or full close
                return self._simulate_execution(
                    Signal(side="close", price=signal.price, amount=signal.amount,
                           source=signal.source, reduce_only=True),
                    market_price, ts
                )

            # New short or add to short
            slip = market_price * self.slippage_pct / 100
            fill_price = market_price - slip
            fee = signal.amount * fill_price * self.fee_pct / 100

            if self._position.side == "flat":
                self._position = Position(
                    side="short", size=signal.amount,
                    entry_price=fill_price, leverage=4,
                )
            elif self._position.side == "short":
                total_cost = (self._position.entry_price * self._position.size +
                              fill_price * signal.amount)
                total_size = self._position.size + signal.amount
                self._position.entry_price = total_cost / total_size if total_size > 0 else fill_price
                self._position.size = total_size

            self._position.notional = self._position.size * fill_price
            self._capital -= fee

            return BacktestTrade(
                timestamp=ts, side="sell", price=round(fill_price, 2),
                amount=signal.amount, pnl=round(-fee, 4),
                source=signal.source, slippage=self.slippage_pct,
            )

        return None

    def _compute_metrics(self, result: BacktestResult):
        """Compute final performance metrics."""
        trades = result.trades

        # Count wins/losses (only closed trades with pnl)
        closed = [t for t in trades if t.side == "close"]
        wins = [t for t in closed if t.pnl > 0]
        losses = [t for t in closed if t.pnl < 0]

        result.total_trades = len(trades)
        result.winning_trades = len(wins)
        result.losing_trades = len(losses)
        result.win_rate = round(len(wins) / len(closed) * 100, 1) if closed else 0

        # Avg win/loss
        result.avg_win = round(sum(t.pnl for t in wins) / len(wins), 4) if wins else 0
        result.avg_loss = round(sum(t.pnl for t in losses) / len(losses), 4) if losses else 0

        # Profit factor
        gross_profit = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        result.profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 999

        # Sharpe ratio (annualized from daily returns)
        if len(self._daily_returns) >= 2:
            avg_ret = sum(self._daily_returns) / len(self._daily_returns)
            std_ret = math.sqrt(sum((r - avg_ret) ** 2 for r in self._daily_returns) / len(self._daily_returns))
            if std_ret > 0:
                result.sharpe_ratio = round(avg_ret / std_ret * math.sqrt(365), 2)

        # Strategy breakdown
        result.grid_trades = len([t for t in trades if t.source == "GRID"])
        result.trend_trades = len([t for t in trades if t.source == "TREND"])
        result.grid_pnl = round(sum(t.pnl for t in trades if t.source == "GRID"), 4)
        result.trend_pnl = round(sum(t.pnl for t in trades if t.source == "TREND"), 4)

    def print_report(self, result: BacktestResult):
        """Print human-readable report."""
        print("\n" + "=" * 60)
        print("BACKTEST REPORT")
        print("=" * 60)
        print(f"  Period: {result.start_date} to {result.end_date}")
        print(f"  Candles: {result.total_candles}")
        print(f"  Capital: ${result.initial_capital} -> ${result.final_capital}")
        print(f"  P&L: ${result.total_pnl:+.2f} ({result.total_pnl / result.initial_capital * 100:+.1f}%)")
        print(f"\n  Trades: {result.total_trades}")
        print(f"  Win rate: {result.win_rate}%")
        print(f"  Profit factor: {result.profit_factor}")
        print(f"  Avg win: ${result.avg_win:+.4f}")
        print(f"  Avg loss: ${result.avg_loss:.4f}")
        print(f"  Sharpe: {result.sharpe_ratio}")
        print(f"\n  Max drawdown: {result.max_drawdown_pct}% (${result.max_drawdown_usd})")
        print(f"  Max exposure: {result.max_exposure_pct}%")
        print(f"\n  Regime: {result.time_in_range_pct}% range, "
              f"{result.time_in_trend_pct}% trend, "
              f"{result.time_in_chaos_pct}% chaos")
        print(f"  Regime changes: {result.regime_changes}")
        print(f"\n  Grid: {result.grid_trades} trades, ${result.grid_pnl:+.4f}")
        print(f"  Trend: {result.trend_trades} trades, ${result.trend_pnl:+.4f}")
        print("=" * 60)
