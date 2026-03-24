"""HTTP status endpoint + web dashboard on port 8099."""

import os
from datetime import datetime, timezone

from aiohttp import web

from .logger import load_trades

# Load dashboard HTML at import time
_DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
_DASHBOARD_PATH = os.path.join(_DASHBOARD_DIR, "dashboard.html")


class StatusServer:
    def __init__(self, config, grid, risk, reporter, analyst=None):
        self.config = config
        self.grid = grid
        self.risk = risk
        self.reporter = reporter
        self.analyst = analyst
        self.start_time = datetime.now(timezone.utc)

    async def start(self):
        app = web.Application()
        app.router.add_get("/", self.handle_dashboard)
        app.router.add_get("/api", self.handle_status)
        app.router.add_get("/health", self.handle_health)
        app.router.add_get("/pnl", self.handle_pnl)
        app.router.add_get("/trades", self.handle_trades)
        app.router.add_get("/ai-data", self.handle_ai_data)
        app.router.add_get("/ai-dashboard", self.handle_ai_dashboard)
        app.router.add_get("/trades-dashboard", self.handle_trades_dashboard)
        app.router.add_post("/cmd/{command}", self.handle_command)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.config.http_port)
        await site.start()

    async def handle_dashboard(self, request):
        with open(_DASHBOARD_PATH, "r", encoding="utf-8") as f:
            html = f.read()
        return web.Response(text=html, content_type="text/html")

    async def handle_status(self, request):
        uptime = str(datetime.now(timezone.utc) - self.start_time).split(".")[0]

        # Get real data from exchange when possible
        exchange_orders = 0
        exchange_position = 0.0
        exchange_entry = 0.0
        exchange_leverage = getattr(self.config, "leverage", 1)
        exchange_unrealized = self.grid.get_unrealized_pnl()

        if self.config.mode == "real" and hasattr(self.grid.exchange, "ccxt_client"):
            try:
                ex = self.grid.exchange.ccxt_client
                open_orders = ex.fetch_open_orders(self.config.symbol)
                exchange_orders = len(open_orders)
                positions = ex.fetch_positions([self.config.symbol])
                for p in positions:
                    # Always read leverage from position data
                    pos_lev = p.get("leverage")
                    if pos_lev:
                        exchange_leverage = int(float(pos_lev))
                    contracts = float(p.get("contracts", 0) or 0)
                    if contracts != 0:
                        exchange_position = contracts if p.get("side") == "long" else -contracts
                        exchange_entry = float(p.get("entryPrice", 0) or 0)
                        exchange_unrealized = float(p.get("unrealizedPnl", 0) or 0)
            except Exception:
                pass

        bot_buys = len(self.grid.buy_orders)
        bot_sells = len(self.grid.sell_orders)

        data = {
            "mode": self.config.mode,
            "symbol": self.config.symbol,
            "current_price": self.grid.last_price,
            "base_price": self.grid.base_price,
            "open_buy_orders": bot_buys,
            "open_sell_orders": bot_sells,
            "exchange_orders": exchange_orders,
            "position_btc": exchange_position if self.config.mode == "real" else self.grid.get_position_btc(),
            "avg_entry": exchange_entry if exchange_entry > 0 else self.grid.get_avg_entry(),
            "realized_pnl": self.grid.realized_pnl,
            "unrealized_pnl": exchange_unrealized,
            "trade_count": self.grid.trade_count,
            "balance": self.grid.exchange.get_balance(),
            "paused": self.grid.paused,
            "uptime": uptime,
            "risk": self.risk.get_status(),
            "config": {
                "grid_levels": self.config.grid_levels,
                "grid_spacing_pct": self.config.grid_spacing_pct,
                "order_size_usdt": self.config.order_size_usdt,
                "stop_loss_pct": self.config.stop_loss_pct,
                "trailing_profit_pct": self.config.trailing_profit_pct,
                "leverage": exchange_leverage,
            },
            "ai_analyst": self.analyst.get_status() if self.analyst else None,
            "position_detail": {
                "entry_price": exchange_entry,
                "side": "long" if exchange_position > 0 else ("short" if exchange_position < 0 else "flat"),
                "size_btc": abs(exchange_position),
                "size_usdc": abs(exchange_position) * self.grid.last_price if self.grid.last_price else 0,
                "unrealized_pnl": exchange_unrealized,
                "stop_loss": self.analyst.dynamic_stop_loss if self.analyst and self.analyst.dynamic_stop_loss > 0 else (exchange_entry * (1 - self.config.stop_loss_pct / 100) if exchange_entry > 0 and exchange_position > 0 else 0),
                "target_1": self.analyst.dynamic_target_1 if self.analyst and self.analyst.dynamic_target_1 > 0 else (exchange_entry * 1.046 if exchange_entry > 0 else 0),
                "target_2": self.analyst.dynamic_target_2 if self.analyst and self.analyst.dynamic_target_2 > 0 else (exchange_entry * 1.094 if exchange_entry > 0 else 0),
                "targets_reason": self.analyst.targets_reason if self.analyst else "",
                "pnl_pct": ((self.grid.last_price - exchange_entry) / exchange_entry * 100) if exchange_entry > 0 and exchange_position > 0 else ((exchange_entry - self.grid.last_price) / exchange_entry * 100 if exchange_entry > 0 and exchange_position < 0 else 0),
            },
        }
        return web.json_response(data)

    async def handle_health(self, request):
        return web.json_response({"status": "ok"})

    async def handle_pnl(self, request):
        return web.json_response({"report": self.reporter.get_pnl_text()})

    async def handle_trades(self, request):
        trades = load_trades(self.config.trade_log)
        return web.json_response({"trades": trades})

    async def handle_command(self, request):
        command = request.match_info["command"]
        allowed = {"status", "pause", "resume", "reset", "pnl", "stop"}
        if command not in allowed:
            return web.json_response({"error": f"Unknown command: {command}"}, status=400)
        # Write command to the command file (same mechanism as Calila)
        try:
            os.makedirs(os.path.dirname(self.config.command_file), exist_ok=True)
            with open(self.config.command_file, "a") as f:
                f.write(command + "\n")
            return web.json_response({"ok": True, "command": command})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def handle_ai_data(self, request):
        if not self.analyst:
            return web.json_response({"error": "AI not available"})
        data = {
            "collected_data": self.analyst.last_collected_data,
            "recommendation": self.analyst.last_recommendation,
            "dynamic_targets": {
                "stop_loss": self.analyst.dynamic_stop_loss,
                "target_1": self.analyst.dynamic_target_1,
                "target_2": self.analyst.dynamic_target_2,
                "reason": self.analyst.targets_reason,
            },
            "last_analysis": self.analyst.last_analysis_time.isoformat() if self.analyst.last_analysis_time else None,
            "last_signal": self.analyst.last_signal,
            "ai_trade_count": self.analyst.ai_trade_count,
        }
        return web.json_response(data)

    async def handle_ai_dashboard(self, request):
        ai_html_path = os.path.join(_DASHBOARD_DIR, "ai_dashboard.html")
        with open(ai_html_path, "r", encoding="utf-8") as f:
            html = f.read()
        return web.Response(text=html, content_type="text/html")

    async def handle_trades_dashboard(self, request):
        trades_html_path = os.path.join(_DASHBOARD_DIR, "trades_dashboard.html")
        with open(trades_html_path, "r", encoding="utf-8") as f:
            html = f.read()
        return web.Response(text=html, content_type="text/html")
