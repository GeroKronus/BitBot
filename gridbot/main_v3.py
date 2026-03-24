"""BitBot v3.0 — Adaptive Grid Trading with Regime Detection.

Main loop hierarchy:
    1. Fetch price
    2. Kill Switch check (immediate, overrides everything)
    3. Regime Detection (deterministic, every 5 min)
    4. Exposure Manager check (limits, drawdown)
    5. AI Analysis (filter, every 30 min)
    6. Apply regime rules + AI filter
    7. Grid tick (check fills, place counter-orders)
    8. Risk check (dynamic stop loss)
    9. Log decision
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gridbot.config import load_config
from gridbot.exchange import create_exchange
from gridbot.grid import GridManager
from gridbot.risk_v3 import RiskManager
from gridbot.notifier import Notifier
from gridbot.commands import CommandHandler
from gridbot.reporter import Reporter
from gridbot.status_server_v3 import StatusServer
from gridbot.analyst_v3 import MarketAnalyst
from gridbot.kill_switch import KillSwitch
from gridbot.regime_detector import RegimeDetector
from gridbot.exposure_manager import ExposureManager
from gridbot.decision_logger import DecisionLogger


async def run():
    # Support --config argument for parallel instances
    config_path = "config.json"
    for i, arg in enumerate(sys.argv):
        if arg == "--config" and i + 1 < len(sys.argv):
            config_path = sys.argv[i + 1]
    config = load_config(config_path)
    exchange = create_exchange(config)
    notifier = Notifier(config)

    print("BitBot v3.0 starting...")
    print(f"  Mode: {config.mode}")
    print(f"  Symbol: {config.symbol}")
    print(f"  Capital: ${config.capital_usdt:,.2f} USDC")
    print(f"  Grid: {config.grid_levels} levels, {config.grid_spacing_pct}% spacing")
    print(f"  Order size: ${config.order_size_usdt:.2f}")
    print(f"  Leverage: {config.leverage}x")

    # --- Initialize all components ---
    grid = GridManager(config, exchange)
    risk = RiskManager(config, grid, notifier)
    reporter = Reporter(config, grid, notifier)
    analyst = MarketAnalyst(config, grid, notifier)
    kill_switch = KillSwitch(config, notifier)
    regime_detector = RegimeDetector()
    exposure = ExposureManager(config, notifier, initial_balance=config.capital_usdt)
    decision_log_path = config.trade_log.replace("trades", "decisions").replace(".jsonl", ".jsonl")
    decision_log = DecisionLogger(log_path=decision_log_path)
    cmd_handler = CommandHandler(config, grid, risk, notifier, reporter)
    status_server = StatusServer(
        config, grid, risk, reporter, analyst,
        kill_switch=kill_switch,
        regime_detector=regime_detector,
        exposure=exposure,
        decision_log=decision_log,
    )

    # Start HTTP
    await status_server.start()
    print(f"  Status server: http://0.0.0.0:{config.http_port}")

    # Initialize grid
    try:
        await grid.initialize()
        price = grid.last_price or await exchange.fetch_price(config.symbol)
        print(f"  Grid base: ${grid.base_price:,.2f}")
        print(f"  BTC price: ${price:,.2f}")
        print(f"  Orders: {len(grid.buy_orders)} buys, {len(grid.sell_orders)} sells")
    except Exception as e:
        print(f"  Grid init error (will retry): {e}")
        notifier.send(f"Grid init error: {e}")

    print("  Kill Switch: enabled")
    print("  Regime Detector: enabled")
    print("  Exposure Manager: enabled")
    print("  Decision Logger: enabled")
    print(f"  AI Analyst: every 30 min")

    notifier.send(
        f"BitBot v3.0 started ({config.mode})\n"
        f"Leverage: {config.leverage}x\n"
        f"Grid: {config.grid_levels} x ${config.order_size_usdt}\n"
        f"Kill Switch + Regime Detection + Exposure Limits ACTIVE"
    )

    tick_count = 0
    regime_interval = 60    # check regime every 60 ticks (~5 min at 5s tick)
    log_interval = 120      # log tick every 120 ticks (~10 min)

    # --- Main loop ---
    consecutive_errors = 0
    max_consecutive_errors = 10

    while True:
        try:
            tick_count += 1

            # ========== 1. FETCH PRICE ==========
            current_price = await exchange.fetch_price(config.symbol)
            consecutive_errors = 0  # reset on success

            # ========== 2. KILL SWITCH (first, overrides everything) ==========
            kill_switch.update_price(current_price)
            atr = analyst.last_collected_data.get("technical", {}).get("atr", 0)
            kill_result = kill_switch.check(current_price, atr)

            if kill_result:
                # EMERGENCY: close all and pause
                decision_log.log_kill_switch(
                    kill_result["reasons"],
                    kill_result["cooldown_minutes"],
                    current_price,
                    grid.get_position_btc()
                )
                await grid.cancel_all()
                await grid.market_sell_all(current_price)
                grid.paused = True
                await asyncio.sleep(config.tick_interval)
                continue

            if kill_switch.is_cooling_down():
                await asyncio.sleep(config.tick_interval)
                continue

            # Recreate grid if it's empty after cooldown expired
            if len(grid.buy_orders) == 0 and len(grid.sell_orders) == 0 and not grid.paused:
                try:
                    grid.base_price = await exchange.fetch_price(config.symbol)
                    await grid._place_grid()
                    if len(grid.buy_orders) > 0 or len(grid.sell_orders) > 0:
                        notifier.send(
                            f"Grid recriado apos cooldown\n"
                            f"Base: ${grid.base_price:,.2f}\n"
                            f"Buys: {len(grid.buy_orders)} | Sells: {len(grid.sell_orders)}"
                        )
                        decision_log.log("grid_recreated", {
                            "base_price": grid.base_price,
                            "buys": len(grid.buy_orders),
                            "sells": len(grid.sell_orders),
                        })
                except Exception as e:
                    print(f"  Warning: could not recreate grid: {e}")

            # ========== 3. REGIME DETECTION (every ~5 min) ==========
            if tick_count % regime_interval == 0:
                hourly_prices = analyst.last_collected_data.get("hourly_prices", [])
                tech = analyst.last_collected_data.get("technical", {})
                if hourly_prices and len(hourly_prices) >= 10:
                    prices_list = [p["price"] if isinstance(p, dict) else p for p in hourly_prices]
                    old_regime = regime_detector.current_regime
                    regime = regime_detector.detect(
                        prices_list,
                        atr=tech.get("atr", 0),
                        sma_20=tech.get("sma_20", 0),
                        sma_50=tech.get("sma_50", 0),
                    )
                    if regime != old_regime:
                        decision_log.log_regime_change(
                            old_regime, regime,
                            regime_detector.indicators,
                            regime_detector.confidence
                        )
                        notifier.send(
                            f"Regime: {old_regime} → {regime}\n"
                            f"Confianca: {regime_detector.confidence:.0%}\n"
                            f"Regras: {regime_detector.get_rules()['description']}"
                        )

            # ========== 4. EXPOSURE CHECK ==========
            balance = exchange.get_balance()
            current_balance = balance.get("USDC", balance.get("USDT", 0))
            position_btc = grid.get_position_btc()
            position_value = abs(position_btc) * current_price

            exposure_check = exposure.check_can_trade(current_balance, position_value)

            if exposure_check["warnings"]:
                for w in exposure_check["warnings"]:
                    if tick_count % 60 == 0:  # warn every ~5 min, not every tick
                        notifier.send(f"ALERTA: {w}")

            if not exposure_check["allowed"]:
                if tick_count % 60 == 0:
                    decision_log.log_exposure_block(
                        exposure_check["reasons"],
                        exposure_check["warnings"],
                        exposure_check["daily_pnl_pct"]
                    )
                    for r in exposure_check["reasons"]:
                        notifier.send(f"BLOQUEADO: {r}")
                grid.paused = True
                await asyncio.sleep(config.tick_interval)
                continue
            else:
                if grid.paused and not kill_switch.is_cooling_down():
                    grid.paused = False

            # ========== 5. AI ANALYSIS (every ~30 min) ==========
            await analyst.analyze(current_price)

            # Sync analyst targets to risk manager
            if analyst.dynamic_stop_loss > 0:
                risk.set_analyst_stop(analyst.dynamic_stop_loss)
            tech_data = analyst.last_collected_data.get("technical", {})
            if tech_data.get("atr", 0) > 0:
                risk.set_atr(tech_data["atr"])

            # ========== 6. APPLY REGIME + AI FILTER ==========
            # Get base rules from regime
            regime_rules = regime_detector.get_rules()
            # Apply AI filter (conservative only)
            ai_outlook = analyst.last_recommendation.get("market_outlook", "neutral")
            final_rules = regime_detector.apply_ai_filter(ai_outlook, regime_rules)

            # Apply rules to grid config
            if not final_rules.get("grid_active", True):
                if not grid.paused:
                    grid.paused = True
                    notifier.send(f"Grid pausado pelo regime: {regime_detector.current_regime}")

            new_levels = final_rules.get("max_levels", config.grid_levels)
            if new_levels != config.grid_levels:
                config.grid_levels = new_levels

            new_leverage = final_rules.get("max_leverage", config.leverage)
            if new_leverage < config.leverage:
                config.leverage = new_leverage

            spacing_mult = final_rules.get("spacing_multiplier", 1.0)
            adjusted_spacing = round(config.grid_spacing_pct * spacing_mult, 2)
            if abs(adjusted_spacing - config.grid_spacing_pct) > 0.05:
                config.grid_spacing_pct = adjusted_spacing

            # ========== 7. GRID TICK ==========
            filled = await grid.check_fills(current_price)

            for trade in filled:
                msg = notifier.format_trade(trade)
                notifier.send(msg)
                print(f"  Trade: {msg}")
                exposure.record_trade()
                if abs(grid.get_position_btc()) > 0.000001:
                    exposure.record_position_open()
                else:
                    exposure.record_position_close()

            # ========== 8. RISK CHECK ==========
            await risk.check(current_price)

            # ========== 9. COMMANDS & REPORTING ==========
            await cmd_handler.poll()
            reporter.check_schedule()

            # ========== 10. PERIODIC LOGGING ==========
            if tick_count % log_interval == 0:
                decision_log.log_tick(
                    price=current_price,
                    regime=regime_detector.current_regime,
                    ai_outlook=ai_outlook,
                    can_trade=exposure_check["allowed"],
                    position=grid.get_position_btc(),
                    balance=current_balance,
                    buy_orders=len(grid.buy_orders),
                    sell_orders=len(grid.sell_orders),
                )

            await asyncio.sleep(config.tick_interval)

        except KeyboardInterrupt:
            print("\nBot stopped by user.")
            notifier.send("Bot stopped by user.")
            break
        except Exception as e:
            consecutive_errors += 1
            error_msg = f"Error ({consecutive_errors}/{max_consecutive_errors}): {e}"
            print(error_msg)
            notifier.send(error_msg)
            decision_log.log("error", {"error": str(e), "consecutive": consecutive_errors})

            if consecutive_errors >= max_consecutive_errors:
                notifier.send(f"CIRCUIT BREAKER: {consecutive_errors} erros consecutivos. Fechando posicoes e parando.")
                try:
                    await grid.cancel_all()
                    await grid.market_sell_all(grid.last_price or 0)
                except Exception:
                    pass
                grid.paused = True
                break

            await asyncio.sleep(30)


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
