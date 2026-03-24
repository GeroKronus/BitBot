"""BitBot main entry point — orchestrates the grid trading loop."""

import asyncio
import os
import sys

# Ensure project root is in path when running as module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gridbot.config import load_config
from gridbot.exchange import create_exchange
from gridbot.grid import GridManager
from gridbot.risk import RiskManager
from gridbot.notifier import Notifier
from gridbot.commands import CommandHandler
from gridbot.reporter import Reporter
from gridbot.status_server import StatusServer
from gridbot.analyst import MarketAnalyst


async def run():
    config = load_config("config.json")
    exchange = create_exchange(config)
    notifier = Notifier(config)

    print(f"BitBot starting in {config.mode} mode...")
    print(f"Symbol: {config.symbol}")
    print(f"Capital: ${config.capital_usdt:,.2f} USDC")
    print(f"Grid: {config.grid_levels} levels, {config.grid_spacing_pct}% spacing")
    print(f"Order size: ${config.order_size_usdt:.2f} per level")
    print(f"Leverage: {config.leverage}x")

    grid = GridManager(config, exchange)
    risk = RiskManager(config, grid, notifier)
    reporter = Reporter(config, grid, notifier)
    analyst = MarketAnalyst(config, grid, notifier)
    cmd_handler = CommandHandler(config, grid, risk, notifier, reporter)
    status_server = StatusServer(config, grid, risk, reporter, analyst)

    # Start HTTP status server
    await status_server.start()
    print(f"Status server running on http://0.0.0.0:{config.http_port}")

    # Initialize grid (loads state or creates new grid)
    try:
        await grid.initialize()
        price = grid.last_price or await exchange.fetch_price(config.symbol)
        print(f"Grid initialized at base price: ${grid.base_price:,.2f}")
        print(f"Current BTC price: ${price:,.2f}")
        print(f"Buy levels: {len(grid.buy_orders)} | Sell levels: {len(grid.sell_orders)}")
    except Exception as e:
        print(f"Grid initialization error (will retry in loop): {e}")
        notifier.send(f"Grid init error: {e}")
    print("AI Market Analyst: enabled (analysis every 2h)")

    notifier.send(
        f"Bot started in {config.mode} mode\n"
        f"Exchange: {config.exchange} | Leverage: {config.leverage}x\n"
        f"Base price: ${grid.base_price:,.2f}\n"
        f"Grid: {config.grid_levels} levels, {config.grid_spacing_pct}% spacing\n"
        f"Capital: ${config.capital_usdt:,.2f} USDC\n"
        f"AI Analyst: enabled"
    )

    # Main trading loop
    while True:
        try:
            current_price = await exchange.fetch_price(config.symbol)
            filled = await grid.check_fills(current_price)

            for trade in filled:
                msg = notifier.format_trade(trade)
                notifier.send(msg)
                print(f"  Trade: {msg}")

            await risk.check(current_price)
            await analyst.analyze(current_price)
            await cmd_handler.poll()
            reporter.check_schedule()

            await asyncio.sleep(config.tick_interval)

        except KeyboardInterrupt:
            print("\nBot stopped by user.")
            notifier.send("Bot stopped by user.")
            break
        except Exception as e:
            error_msg = f"Error: {e}"
            print(error_msg)
            notifier.send(error_msg)
            await asyncio.sleep(30)  # backoff on error


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
