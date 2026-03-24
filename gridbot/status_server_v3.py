"""HTTP status endpoint + dashboards for BitBot v3."""

import os
from datetime import datetime, timezone

from aiohttp import web

from .logger import load_trades

_DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))


class StatusServer:
    def __init__(self, config, grid, risk, reporter, analyst=None,
                 kill_switch=None, regime_detector=None, exposure=None, decision_log=None):
        self.config = config
        self.grid = grid
        self.risk = risk
        self.reporter = reporter
        self.analyst = analyst
        self.kill_switch = kill_switch
        self.regime = regime_detector
        self.exposure = exposure
        self.decision_log = decision_log
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
        app.router.add_get("/decisions", self.handle_decisions)
        app.router.add_post("/cmd/{command}", self.handle_command)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.config.http_port)
        await site.start()

    async def handle_dashboard(self, request):
        path = os.path.join(_DASHBOARD_DIR, "dashboard.html")
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
        return web.Response(text=html, content_type="text/html")

    async def handle_status(self, request):
        uptime = str(datetime.now(timezone.utc) - self.start_time).split(".")[0]

        # Get real data from exchange
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

        # Position detail
        pos_side = "long" if exchange_position > 0 else ("short" if exchange_position < 0 else "flat")
        current_price = self.grid.last_price or 0

        # Dynamic targets from analyst
        sl = 0
        t1 = 0
        t2 = 0
        targets_reason = ""
        if self.analyst:
            dt = self.analyst.get_status().get("dynamic_targets", {})
            sl = dt.get("stop_loss", 0)
            t1 = dt.get("target_1", 0)
            t2 = dt.get("target_2", 0)
            targets_reason = dt.get("reason", "")

        # Fallback targets if analyst hasn't set them
        if sl == 0 and exchange_entry > 0:
            if exchange_position > 0:
                sl = exchange_entry * (1 - self.config.stop_loss_pct / 100)
            elif exchange_position < 0:
                sl = exchange_entry * (1 + self.config.stop_loss_pct / 100)

        # P&L %
        pnl_pct = 0
        if exchange_entry > 0:
            if exchange_position > 0:
                pnl_pct = (current_price - exchange_entry) / exchange_entry * 100
            elif exchange_position < 0:
                pnl_pct = (exchange_entry - current_price) / exchange_entry * 100

        data = {
            "mode": self.config.mode,
            "symbol": self.config.symbol,
            "current_price": current_price,
            "base_price": self.grid.base_price,
            "open_buy_orders": len(self.grid.buy_orders),
            "open_sell_orders": len(self.grid.sell_orders),
            "exchange_orders": exchange_orders,
            "position_btc": exchange_position if self.config.mode == "real" else self.grid.get_position_btc(),
            "avg_entry": exchange_entry if exchange_entry > 0 else self.grid.get_avg_entry(),
            "realized_pnl": self.grid.realized_pnl,
            "unrealized_pnl": exchange_unrealized,
            "trade_count": self.grid.trade_count,
            "balance": self.grid.exchange.get_balance(),
            "paused": self.grid.paused,
            "uptime": uptime,
            "config": {
                "grid_levels": self.config.grid_levels,
                "grid_spacing_pct": self.config.grid_spacing_pct,
                "order_size_usdt": self.config.order_size_usdt,
                "stop_loss_pct": self.config.stop_loss_pct,
                "trailing_profit_pct": self.config.trailing_profit_pct,
                "leverage": exchange_leverage,
            },
            "position_detail": {
                "entry_price": exchange_entry,
                "side": pos_side,
                "size_btc": abs(exchange_position),
                "size_usdc": abs(exchange_position) * current_price if current_price else 0,
                "unrealized_pnl": exchange_unrealized,
                "stop_loss": sl,
                "target_1": t1,
                "target_2": t2,
                "targets_reason": targets_reason,
                "pnl_pct": round(pnl_pct, 2),
            },
            # v3 components
            "risk": self.risk.get_status(),
            "ai_analyst": self.analyst.get_status() if self.analyst else None,
            "regime": self.regime.get_status() if self.regime else None,
            "kill_switch": self.kill_switch.get_status() if self.kill_switch else None,
            "exposure": self.exposure.get_status() if self.exposure else None,
        }
        return web.json_response(data)

    async def handle_health(self, request):
        return web.json_response({"status": "ok"})

    async def handle_pnl(self, request):
        return web.json_response({"report": self.reporter.get_pnl_text()})

    async def handle_trades(self, request):
        trades = load_trades(self.config.trade_log)
        return web.json_response({"trades": trades})

    async def handle_ai_data(self, request):
        if not self.analyst:
            return web.json_response({"error": "AI not available"})
        return web.json_response({
            "collected_data": self.analyst.last_collected_data,
            "recommendation": self.analyst.last_recommendation,
            "dynamic_targets": self.analyst.get_status().get("dynamic_targets", {}),
            "last_analysis": self.analyst.last_analysis_time.isoformat() if self.analyst.last_analysis_time else None,
        })

    async def handle_decisions(self, request):
        if not self.decision_log:
            return web.json_response({"decisions": []})
        dtype = request.query.get("type")
        count = int(request.query.get("count", 50))
        decisions = self.decision_log.get_recent(count, dtype)
        return web.json_response({"decisions": decisions})

    async def handle_ai_dashboard(self, request):
        path = os.path.join(_DASHBOARD_DIR, "ai_dashboard.html")
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
        return web.Response(text=html, content_type="text/html")

    async def handle_trades_dashboard(self, request):
        path = os.path.join(_DASHBOARD_DIR, "trades_dashboard.html")
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
        return web.Response(text=html, content_type="text/html")

    async def handle_command(self, request):
        command = request.match_info["command"]
        allowed = {"status", "pause", "resume", "reset", "pnl", "stop"}
        if command not in allowed:
            return web.json_response({"error": f"Unknown command: {command}"}, status=400)
        try:
            os.makedirs(os.path.dirname(self.config.command_file), exist_ok=True)
            with open(self.config.command_file, "a") as f:
                f.write(command + "\n")
            return web.json_response({"ok": True, "command": command})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
