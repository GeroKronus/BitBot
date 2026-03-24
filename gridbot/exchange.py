"""Exchange abstraction: PaperExchange (simulation) and HyperliquidExchange (live)."""

import json
import uuid
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import ccxt


class Order:
    def __init__(self, order_id: str, side: str, amount: float, price: float):
        self.id = order_id
        self.side = side
        self.amount = amount
        self.price = price
        self.status = "open"
        self.created_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "side": self.side,
            "amount": self.amount,
            "price": self.price,
            "status": self.status,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Order":
        order = cls(data["id"], data["side"], data["amount"], data["price"])
        order.status = data.get("status", "open")
        order.created_at = data.get("created_at", "")
        return order


class BaseExchange(ABC):
    @abstractmethod
    async def fetch_price(self, symbol: str) -> float:
        pass

    @abstractmethod
    async def place_limit_buy(self, symbol: str, amount: float, price: float) -> Order:
        pass

    @abstractmethod
    async def place_limit_sell(self, symbol: str, amount: float, price: float) -> Order:
        pass

    @abstractmethod
    async def cancel_order(self, order_id: str) -> None:
        pass

    @abstractmethod
    def get_balance(self) -> dict:
        pass


class PaperExchange(BaseExchange):
    FEE_RATE = 0.0005  # 0.05% simulated fee (Hyperliquid-like)

    def __init__(self, config):
        # Try binanceus first (for US-based servers), fallback to binance
        try:
            self.ccxt_client = ccxt.binanceus()
            self.ccxt_client.fetch_ticker("BTC/USDT")
            self._price_source = "binanceus"
        except Exception:
            try:
                self.ccxt_client = ccxt.binance()
                self.ccxt_client.fetch_ticker("BTC/USDT")
                self._price_source = "binance"
            except Exception:
                self.ccxt_client = None
                self._price_source = "coingecko"
        self.open_orders: dict[str, Order] = {}
        self.balance = {"USDT": config.capital_usdt, "BTC": 0.0}
        self.leverage = getattr(config, "leverage", 1)

    async def fetch_price(self, symbol: str) -> float:
        # Normalize symbol for price fetching
        price_symbol = "BTC/USDT"
        if self.ccxt_client:
            try:
                ticker = self.ccxt_client.fetch_ticker(price_symbol)
                return float(ticker["last"])
            except Exception:
                pass
        # Fallback: CoinGecko free API (no key needed)
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
        req = urllib.request.Request(url, headers={"User-Agent": "BitBot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return float(data["bitcoin"]["usd"])

    async def place_limit_buy(self, symbol: str, amount: float, price: float) -> Order:
        order = Order(str(uuid.uuid4())[:8], "buy", amount, price)
        self.open_orders[order.id] = order
        return order

    async def place_limit_sell(self, symbol: str, amount: float, price: float) -> Order:
        order = Order(str(uuid.uuid4())[:8], "sell", amount, price)
        self.open_orders[order.id] = order
        return order

    async def cancel_order(self, order_id: str) -> None:
        if order_id in self.open_orders:
            del self.open_orders[order_id]

    def execute_fill(self, order: Order) -> float:
        fee = order.amount * order.price * self.FEE_RATE
        if order.side == "buy":
            cost = order.amount * order.price + fee
            self.balance["USDT"] -= cost / self.leverage  # margin only
            self.balance["BTC"] += order.amount
        else:
            revenue = order.amount * order.price - fee
            self.balance["USDT"] += revenue / self.leverage
            self.balance["BTC"] -= order.amount
        if order.id in self.open_orders:
            del self.open_orders[order.id]
        return fee

    def get_balance(self) -> dict:
        return dict(self.balance)


class HyperliquidExchange(BaseExchange):
    def __init__(self, config):
        self.config = config
        self.ccxt_client = ccxt.hyperliquid({
            "privateKey": config.hyperliquid_private_key,
            "walletAddress": config.hyperliquid_wallet_address,
            "enableRateLimit": True,
        })
        self.symbol = config.symbol  # BTC/USDC:USDC
        self.leverage = config.leverage
        self._leverage_set = False
        self._cached_balance = {"USDC": 0.0, "BTC": 0.0}

    def _ensure_leverage(self):
        if not self._leverage_set:
            try:
                self.ccxt_client.set_leverage(self.leverage, self.symbol)
                self._leverage_set = True
            except Exception:
                pass  # some exchanges don't need explicit leverage setting

    async def fetch_price(self, symbol: str) -> float:
        ticker = self.ccxt_client.fetch_ticker(symbol)
        return float(ticker["last"])

    async def place_limit_buy(self, symbol: str, amount: float, price: float) -> Order:
        self._ensure_leverage()
        # Round amount to Hyperliquid's precision (5 significant figures for BTC)
        amount = round(amount, 5)
        price = round(price, 1)
        result = self.ccxt_client.create_limit_buy_order(
            symbol, amount, price,
            params={"leverage": self.leverage}
        )
        return Order(str(result["id"]), "buy", amount, price)

    async def place_limit_sell(self, symbol: str, amount: float, price: float) -> Order:
        self._ensure_leverage()
        amount = round(amount, 5)
        price = round(price, 1)
        result = self.ccxt_client.create_limit_sell_order(
            symbol, amount, price,
            params={"leverage": self.leverage}
        )
        return Order(str(result["id"]), "sell", amount, price)

    async def cancel_order(self, order_id: str) -> None:
        try:
            self.ccxt_client.cancel_order(order_id, self.symbol)
        except Exception:
            pass  # order may already be filled or cancelled

    def execute_fill(self, order: Order) -> float:
        return 0.0  # real exchange handles fees

    def get_balance(self) -> dict:
        try:
            balance = self.ccxt_client.fetch_balance()
            usdc_data = balance.get("USDC", {})
            usdc_free = float(usdc_data.get("free", 0) or 0)
            usdc_total = float(usdc_data.get("total", 0) or 0)
            self._cached_balance = {
                "USDC": usdc_total,
                "USDC_free": usdc_free,
                "BTC": 0.0,
            }
        except Exception:
            pass
        return dict(self._cached_balance)


def create_exchange(config) -> BaseExchange:
    if config.mode == "paper":
        return PaperExchange(config)
    elif config.mode == "real":
        if config.exchange == "hyperliquid":
            if not config.hyperliquid_private_key:
                raise ValueError("Real mode requires hyperliquid_private_key")
            return HyperliquidExchange(config)
        else:
            raise ValueError(f"Unknown exchange: {config.exchange}")
    else:
        raise ValueError(f"Unknown mode: {config.mode}")
