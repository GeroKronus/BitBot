"""BitBot v4 — Main Loop.

Pipeline: Price → Features → Kill Switch → Regime → Governor → Strategy → Risk → Execution → Log

Authority hierarchy (deterministic, no ambiguity):
    Kill Switch > Risk Engine > Governor > Strategy

ChatGPT test #4: "Strategy: BUY, Governor: HOLD, Risk: CLOSE — who wins?"
Answer: Risk CLOSE wins. Always. Then Governor. Then Strategy.
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from v4.core.interfaces import RegimeState, Position, GovernorDecision, Features
from v4.agents.market_data import HyperliquidMarketData, PaperMarketData
from v4.agents.feature_engine import FeatureEngine
from v4.agents.regime import RegimeAgent, PositionCore
from v4.agents.governor import AIGovernor
from v4.strategies.grid import GridStrategy
from v4.strategies.trend import TrendStrategy
from v4.strategies.no_trade import NoTradeStrategy
from v4.engine.orchestrator import StrategyOrchestrator
from v4.engine.risk import RiskEngine
from v4.engine.execution import HyperliquidExecutionAgent, PaperExecutionAgent
from v4.engine.kill_switch import KillSwitch


async def run():
    # Config
    mode = os.environ.get("BITBOT_MODE", "paper")
    symbol = "BTC/USDC:USDC"
    capital = float(os.environ.get("BITBOT_CAPITAL", "130"))
    tick_interval = 5

    print("BitBot v4.0 starting...")
    print(f"  Mode: {mode}")
    print(f"  Capital: ${capital:,.2f}")

    # Initialize components
    if mode == "real":
        pk = os.environ.get("HYPERLIQUID_PRIVATE_KEY", "")
        wa = os.environ.get("HYPERLIQUID_WALLET_ADDRESS", "")
        market_data = HyperliquidMarketData(symbol, pk, wa)
        execution = HyperliquidExecutionAgent(market_data.ccxt, symbol)
    else:
        market_data = PaperMarketData(symbol)
        execution = PaperExecutionAgent()

    feature_engine = FeatureEngine()
    regime_agent = RegimeAgent()
    kill_switch = KillSwitch()
    governor = AIGovernor(interval_minutes=30)
    risk_engine = RiskEngine({"capital": capital})

    orchestrator = StrategyOrchestrator({
        "RANGE": GridStrategy({"grid_levels": 7, "order_size_usdt": 15}),
        "TREND": TrendStrategy({}),
        "NO_TRADE": NoTradeStrategy(),
    })

    # State
    regime_state = RegimeState()
    governor_decision = GovernorDecision()
    position = Position()

    # Circuit breaker
    consecutive_errors = 0
    max_errors = 10

    # Tick counter
    tick = 0
    candle_cache = []
    candle_refresh_interval = 60  # refresh candles every 60 ticks (~5 min)

    print("  Pipeline: Price → Features → Kill → Regime → Governor → Strategy → Risk → Execution")
    print("  Authority: Kill Switch > Risk > Governor > Strategy")
    print("  Ready.\n")

    while True:
        try:
            tick += 1
            start = time.time()

            # ========== 1. FETCH PRICE ==========
            snapshot = market_data.fetch()
            consecutive_errors = 0  # reset on success

            # Refresh candles periodically
            if tick % candle_refresh_interval == 1 or not candle_cache:
                candle_cache = market_data.get_candles("1h", 72)

            # ========== 2. COMPUTE FEATURES ==========
            features = feature_engine.compute(snapshot, candle_cache)

            # ========== 3. KILL SWITCH (overrides everything) ==========
            kill_result = kill_switch.update(snapshot.price, features)

            if kill_result:
                print(f"  KILL SWITCH: {kill_result['reasons']}")
                execution.cancel_all()
                if position.side != "flat":
                    execution.close_position(position, snapshot.price)
                    position = Position()
                await asyncio.sleep(tick_interval)
                continue

            if kill_switch.is_cooling_down():
                await asyncio.sleep(tick_interval)
                continue

            # ========== 4. REGIME DETECTION ==========
            regime_state = regime_agent.detect(features, regime_state)

            # ========== 5. AI GOVERNOR (every 30 min) ==========
            governor_decision = governor.decide(features, regime_state, position)

            # ========== 6. STRATEGY SIGNALS ==========
            # Only generate new grid signals if order book is empty or needs refresh
            needs_signals = True
            if mode != "real" and hasattr(execution, '_open_orders'):
                if len(execution._open_orders) >= 6:  # more room for 7 levels
                    needs_signals = False

            signals = []
            if needs_signals:
                try:
                    signals = orchestrator.select_and_run(
                        features, regime_state, position, governor_decision
                    )
                except Exception as e:
                    # Auditor: exception in strategy must NOT kill the cycle
                    print(f"  Strategy error (non-fatal): {e}")

            # ========== 7. RISK FILTER ==========
            approved = []
            if signals:
                try:
                    approved = risk_engine.evaluate(
                        signals, position, features, regime_state, governor_decision
                    )
                except Exception as e:
                    print(f"  Risk error (non-fatal): {e}")

            # ========== 8. EXECUTION ==========
            # Place new orders (limit orders go to pending book)
            if approved:
                results = execution.execute(approved)
                for result in results:
                    if result.filled:
                        print(f"  Executed: {result.fill_amount:.5f} BTC @ ${result.fill_price:,.1f} "
                              f"(slip: {result.slippage_pct}%, lat: {result.latency_ms}ms)")

            # Check pending limit orders against current price (paper mode)
            if mode != "real" and hasattr(execution, 'check_fills'):
                fill_results = execution.check_fills(snapshot.price)
                for result in fill_results:
                    if result.filled:
                        print(f"  Fill: {result.fill_amount:.5f} BTC @ ${result.fill_price:,.1f} "
                              f"(slip: {result.slippage_pct}%, lat: {result.latency_ms}ms)")

            # ========== 9. UPDATE STATE ==========
            if mode == "real":
                raw_pos = market_data.get_position()
                position = Position(
                    side=raw_pos.get("side", "flat"),
                    size=raw_pos.get("size", 0),
                    entry_price=raw_pos.get("entry_price", 0),
                    unrealized_pnl=raw_pos.get("unrealized_pnl", 0),
                    notional=raw_pos.get("notional", 0),
                    leverage=raw_pos.get("leverage", 1),
                )
                bal = market_data.get_balance()
                risk_engine.update_capital(bal.get("total", capital))
            else:
                # Paper mode: update from PaperPosition
                position = execution.position.to_position(snapshot.price)
                risk_engine.update_capital(execution.position.capital)

            # ========== 10. MISSED OPPORTUNITY TRACKER ==========
            if regime_state.current in ("TREND_STRONG",) and position.side == "flat":
                if not hasattr(run, '_trend_start_price'):
                    run._trend_start_price = snapshot.price
                    run._trend_start_tick = tick
                move = abs(snapshot.price - run._trend_start_price)
                move_pct = move / run._trend_start_price * 100 if run._trend_start_price > 0 else 0
                ticks_in_trend = tick - run._trend_start_tick
                if ticks_in_trend > 0 and ticks_in_trend % 120 == 0:  # log every 10 min
                    print(f"  MISSED: TREND_STRONG for {ticks_in_trend*5//60}min, "
                          f"move: ${move:,.0f} ({move_pct:.1f}%), position: FLAT")
            else:
                if hasattr(run, '_trend_start_price'):
                    del run._trend_start_price
                    del run._trend_start_tick

            # ========== 11. PERIODIC LOG ==========
            if tick % 60 == 0:  # every ~5 min
                elapsed = time.time() - start
                cap_str = ""
                if mode != "real":
                    cap_str = f" | Cap: ${execution.position.capital:.2f} ({execution.position.trade_count} trades)"
                print(f"  [{tick}] ${snapshot.price:,.0f} | {regime_state.current} "
                      f"({regime_state.confidence:.0%}) | "
                      f"{orchestrator.active_strategy_name} | "
                      f"Pos: {position.side} {position.size:.5f} | "
                      f"{elapsed*1000:.0f}ms{cap_str}")

            # Stale order cleanup (every 5 min)
            if tick % 60 == 0 and mode == "real":
                execution.cancel_stale_orders()

            await asyncio.sleep(tick_interval)

        except KeyboardInterrupt:
            print("\nShutting down...")
            execution.cancel_all()
            break

        except Exception as e:
            consecutive_errors += 1
            print(f"  Error ({consecutive_errors}/{max_errors}): {e}")

            if consecutive_errors >= max_errors:
                print("  CIRCUIT BREAKER — shutting down")
                execution.cancel_all()
                if position.side != "flat":
                    execution.close_position(position, 0)
                break

            await asyncio.sleep(30)


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
