"""MarketDataAgent — Fetches real-time data from Hyperliquid via ccxt."""

import time
from datetime import datetime, timezone

import ccxt

from ..core.interfaces import IMarketDataAgent, MarketSnapshot


class HyperliquidMarketData(IMarketDataAgent):
    """Fetches price, spread, orderbook depth, and latency from Hyperliquid."""

    def __init__(self, symbol: str = "BTC/USDC:USDC",
                 private_key: str = "", wallet_address: str = ""):
        self.symbol = symbol
        self.ccxt = ccxt.hyperliquid({
            "privateKey": private_key,
            "walletAddress": wallet_address,
            "enableRateLimit": True,
        }) if private_key else ccxt.hyperliquid({"enableRateLimit": True})

        self._last_snapshot = None

    def fetch(self) -> MarketSnapshot:
        """Fetch current market snapshot with timing."""
        start = time.time()

        ticker = self.ccxt.fetch_ticker(self.symbol)
        latency = int((time.time() - start) * 1000)

        price = float(ticker.get("last", 0))
        bid = float(ticker.get("bid", 0) or price)
        ask = float(ticker.get("ask", 0) or price)
        spread = ask - bid if ask > 0 and bid > 0 else 0

        # Funding rate
        funding_rate = 0.0
        try:
            fr = self.ccxt.fetch_funding_rate(self.symbol)
            funding_rate = float(fr.get("fundingRate", 0) or 0)
        except Exception:
            pass

        snapshot = MarketSnapshot(
            timestamp=datetime.now(timezone.utc),
            price=price,
            bid=bid,
            ask=ask,
            spread=round(spread, 2),
            volume_24h=float(ticker.get("quoteVolume", 0) or 0),
            funding_rate=funding_rate,
            latency_ms=latency,
        )
        self._last_snapshot = snapshot
        return snapshot

    def get_candles(self, timeframe: str = "1h", limit: int = 72) -> list[dict]:
        """Fetch OHLCV candles from Hyperliquid.

        Returns list of dicts with: timestamp, open, high, low, close, volume
        """
        try:
            ohlcv = self.ccxt.fetch_ohlcv(self.symbol, timeframe, limit=limit)
            return [
                {
                    "timestamp": c[0],
                    "open": c[1],
                    "high": c[2],
                    "low": c[3],
                    "close": c[4],
                    "volume": c[5],
                }
                for c in ohlcv
            ]
        except Exception:
            return []

    def get_balance(self) -> dict:
        """Fetch account balance."""
        try:
            bal = self.ccxt.fetch_balance()
            return {
                "total": float(bal.get("USDC", {}).get("total", 0) or 0),
                "free": float(bal.get("USDC", {}).get("free", 0) or 0),
            }
        except Exception:
            return {"total": 0, "free": 0}

    def get_position(self) -> dict:
        """Fetch current position from exchange."""
        try:
            positions = self.ccxt.fetch_positions([self.symbol])
            for p in positions:
                contracts = float(p.get("contracts", 0) or 0)
                if contracts > 0:
                    return {
                        "side": p.get("side", "flat"),
                        "size": contracts,
                        "entry_price": float(p.get("entryPrice", 0) or 0),
                        "unrealized_pnl": float(p.get("unrealizedPnl", 0) or 0),
                        "leverage": int(float(p.get("leverage", 1) or 1)),
                        "notional": float(p.get("notional", 0) or 0),
                    }
            return {"side": "flat", "size": 0, "entry_price": 0,
                    "unrealized_pnl": 0, "leverage": 1, "notional": 0}
        except Exception:
            return {"side": "flat", "size": 0, "entry_price": 0,
                    "unrealized_pnl": 0, "leverage": 1, "notional": 0}


class PaperMarketData(IMarketDataAgent):
    """Paper mode — uses real prices but no authentication."""

    def __init__(self, symbol: str = "BTC/USDC:USDC"):
        self.symbol = symbol
        # Try binanceus first (US server), fallback to coingecko
        try:
            self.ccxt = ccxt.binanceus()
            self.ccxt.fetch_ticker("BTC/USDT")
            self._source = "binanceus"
            self._price_symbol = "BTC/USDT"
        except Exception:
            self.ccxt = None
            self._source = "coingecko"
            self._price_symbol = None

    def fetch(self) -> MarketSnapshot:
        start = time.time()
        price = 0.0

        if self.ccxt:
            try:
                ticker = self.ccxt.fetch_ticker(self._price_symbol)
                price = float(ticker.get("last", 0))
            except Exception:
                pass

        if price == 0:
            import json
            import urllib.request
            url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
            req = urllib.request.Request(url, headers={"User-Agent": "BitBot/4.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            price = float(data["bitcoin"]["usd"])

        latency = int((time.time() - start) * 1000)

        return MarketSnapshot(
            timestamp=datetime.now(timezone.utc),
            price=price,
            bid=price,
            ask=price,
            spread=0,
            volume_24h=0,
            funding_rate=0,
            latency_ms=latency,
        )

    def get_candles(self, timeframe: str = "1h", limit: int = 72) -> list[dict]:
        try:
            import json
            import urllib.request
            days = 3 if limit <= 72 else 7
            url = (f"https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
                   f"?vs_currency=usd&days={days}&interval=hourly")
            req = urllib.request.Request(url, headers={"User-Agent": "BitBot/4.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            return [
                {"timestamp": p[0], "open": p[1], "high": p[1],
                 "low": p[1], "close": p[1], "volume": 0}
                for p in data.get("prices", [])
            ]
        except Exception:
            return []
