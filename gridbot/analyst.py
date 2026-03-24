"""AI Market Analyst — uses Claude via AWS Bedrock to analyze market and adjust strategy."""

import json
import math
import os
import urllib.request
from datetime import datetime, timezone, timedelta

import boto3

from .logger import log_trade


class MarketAnalyst:
    # Maximum position size as percentage of total balance
    MAX_POSITION_PCT = 30
    # Minimum AI confidence to execute a trade signal
    MIN_SIGNAL_CONFIDENCE = 7

    def __init__(self, config, grid, notifier):
        self.config = config
        self.grid = grid
        self.notifier = notifier
        self.bedrock = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", ""),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
        )
        self.last_analysis_time = None
        self.analysis_interval_minutes = 5
        self.last_report = "No analysis yet"
        self.last_recommendation = {}
        self.last_signal = "hold"
        self.ai_trade_count = 0
        # Dynamic targets — updated every analysis
        self.dynamic_stop_loss = 0.0
        self.dynamic_target_1 = 0.0
        self.dynamic_target_2 = 0.0
        self.targets_reason = ""
        # Store last collected data for AI dashboard
        self.last_collected_data = {}

    def should_analyze(self) -> bool:
        if self.last_analysis_time is None:
            return True
        elapsed = datetime.now(timezone.utc) - self.last_analysis_time
        return elapsed >= timedelta(minutes=self.analysis_interval_minutes)

    async def analyze(self, current_price: float):
        if not self.should_analyze():
            return

        try:
            data = self._collect_market_data(current_price)
            recommendation = self._ask_claude(data)
            await self._apply_recommendation(recommendation, current_price)
            self.last_analysis_time = datetime.now(timezone.utc)
        except Exception as e:
            self.notifier.send(f"AI Analyst error: {e}")

    def _collect_market_data(self, current_price: float) -> dict:
        data = {
            "current_price": current_price,
            "base_price": self.grid.base_price,
            "price_deviation_pct": round(
                ((current_price - self.grid.base_price) / self.grid.base_price) * 100, 2
            ) if self.grid.base_price > 0 else 0,
            "position_btc": self.grid.get_position_btc(),
            "realized_pnl": self.grid.realized_pnl,
            "unrealized_pnl": self.grid.get_unrealized_pnl(),
            "trade_count": self.grid.trade_count,
            "open_buys": len(self.grid.buy_orders),
            "open_sells": len(self.grid.sell_orders),
            "pending_counter_orders": len(getattr(self.grid, "pending_counter_orders", [])),
            "current_config": {
                "grid_spacing_pct": self.config.grid_spacing_pct,
                "grid_levels": self.config.grid_levels,
                "order_size_usdt": self.config.order_size_usdt,
                "leverage": self.config.leverage,
                "stop_loss_pct": self.config.stop_loss_pct,
            },
        }

        # Fetch Fear & Greed Index
        data["fear_greed"] = self._fetch_fear_greed()

        # Fetch BTC market data from CoinGecko
        data["market"] = self._fetch_market_data()

        # Fetch price history (daily for 7 days)
        data["price_history"] = self._fetch_price_history()

        # Fetch hourly price history (for technical indicators)
        hourly = self._fetch_hourly_prices()
        data["hourly_prices"] = hourly

        # Calculate technical indicators from hourly data
        data["technical"] = self._calculate_technical_indicators(hourly)

        # Identify support/resistance levels
        data["support_resistance"] = self._find_support_resistance(hourly)

        # Volume data
        data["volume_history"] = self._fetch_volume_history()

        # Fetch Hyperliquid funding rate
        data["funding_rate"] = self._fetch_funding_rate()

        # Upcoming macro events
        data["macro_events"] = self._get_macro_events()

        # Available margin info
        data["available_margin"] = self._get_margin_info()

        # Current exchange position (from exchange, not just grid tracking)
        data["exchange_position"] = self._get_exchange_position()

        # Real-time crypto news
        data["news"] = self._fetch_crypto_news()

        # Store for AI dashboard
        self.last_collected_data = data

        return data

    def _get_exchange_position(self) -> dict:
        """Get current position from the exchange directly."""
        try:
            if hasattr(self.grid.exchange, "ccxt_client") and self.config.mode == "real":
                positions = self.grid.exchange.ccxt_client.fetch_positions([self.config.symbol])
                for pos in positions:
                    size = float(pos.get("contracts", 0) or 0)
                    side = pos.get("side", "")
                    if size > 0:
                        return {
                            "size": size,
                            "side": side,
                            "entry_price": float(pos.get("entryPrice", 0) or 0),
                            "unrealized_pnl": float(pos.get("unrealizedPnl", 0) or 0),
                            "notional": float(pos.get("notional", 0) or 0),
                        }
            # Paper mode or no position
            pos_btc = self.grid.get_position_btc()
            return {
                "size": abs(pos_btc),
                "side": "long" if pos_btc > 0 else ("short" if pos_btc < 0 else "none"),
                "entry_price": self.grid.get_avg_entry(),
                "unrealized_pnl": self.grid.get_unrealized_pnl(),
                "notional": 0,
            }
        except Exception:
            return {"size": 0, "side": "none", "entry_price": 0, "unrealized_pnl": 0, "notional": 0}

    def _fetch_fear_greed(self) -> dict:
        try:
            url = "https://api.alternative.me/fng/?limit=7"
            req = urllib.request.Request(url, headers={"User-Agent": "BitBot/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
            entries = result.get("data", [])
            if entries:
                return {
                    "value": int(entries[0]["value"]),
                    "label": entries[0]["value_classification"],
                    "history_7d": [
                        {"value": int(e["value"]), "label": e["value_classification"]}
                        for e in entries
                    ],
                }
        except Exception:
            pass
        return {"value": 50, "label": "Neutral", "history_7d": []}

    def _fetch_market_data(self) -> dict:
        try:
            url = (
                "https://api.coingecko.com/api/v3/coins/bitcoin"
                "?localization=false&tickers=false&community_data=false"
                "&developer_data=false&sparkline=false"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "BitBot/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            md = data.get("market_data", {})
            return {
                "price_change_24h_pct": md.get("price_change_percentage_24h", 0),
                "price_change_7d_pct": md.get("price_change_percentage_7d", 0),
                "price_change_30d_pct": md.get("price_change_percentage_30d", 0),
                "market_cap_usd": md.get("market_cap", {}).get("usd", 0),
                "total_volume_24h": md.get("total_volume", {}).get("usd", 0),
                "ath": md.get("ath", {}).get("usd", 0),
                "ath_change_pct": md.get("ath_change_percentage", {}).get("usd", 0),
                "high_24h": md.get("high_24h", {}).get("usd", 0),
                "low_24h": md.get("low_24h", {}).get("usd", 0),
            }
        except Exception:
            return {}

    def _fetch_price_history(self) -> list:
        try:
            url = (
                "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
                "?vs_currency=usd&days=7&interval=daily"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "BitBot/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            prices = data.get("prices", [])
            return [{"date": p[0], "price": round(p[1], 2)} for p in prices]
        except Exception:
            return []

    def _fetch_crypto_news(self) -> list:
        """Fetch latest crypto news from CryptoPanic (free API)."""
        try:
            url = "https://cryptopanic.com/api/free/v1/posts/?auth_token=free&currencies=BTC&kind=news&num_results=10"
            req = urllib.request.Request(url, headers={"User-Agent": "BitBot/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            results = []
            for post in data.get("results", [])[:10]:
                votes = post.get("votes", {})
                positive = votes.get("positive", 0)
                negative = votes.get("negative", 0)
                sentiment = "positive" if positive > negative else ("negative" if negative > positive else "neutral")
                results.append({
                    "title": post.get("title", ""),
                    "source": post.get("source", {}).get("title", ""),
                    "published": post.get("published_at", "")[:16],
                    "sentiment": sentiment,
                    "votes_positive": positive,
                    "votes_negative": negative,
                })
            return results
        except Exception:
            # Fallback: try alternative free news source
            try:
                url = "https://min-api.cryptocompare.com/data/v2/news/?categories=BTC&extraParams=BitBot"
                req = urllib.request.Request(url, headers={"User-Agent": "BitBot/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                results = []
                for item in data.get("Data", [])[:10]:
                    results.append({
                        "title": item.get("title", ""),
                        "source": item.get("source", ""),
                        "published": item.get("published_on", ""),
                        "sentiment": "neutral",
                        "votes_positive": 0,
                        "votes_negative": 0,
                    })
                return results
            except Exception:
                return []

    def _fetch_funding_rate(self) -> dict:
        try:
            if hasattr(self.grid.exchange, "ccxt_client"):
                fr = self.grid.exchange.ccxt_client.fetch_funding_rate("BTC/USDC:USDC")
                return {
                    "rate": float(fr.get("fundingRate", 0) or 0),
                    "next_timestamp": str(fr.get("fundingDatetime", "")),
                }
        except Exception:
            pass
        return {"rate": 0.0, "next_timestamp": ""}

    def _fetch_hourly_prices(self) -> list:
        """Fetch hourly prices for the last 3 days (72 data points) for TA calculations."""
        try:
            url = (
                "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
                "?vs_currency=usd&days=3"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "BitBot/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            prices = data.get("prices", [])
            return [round(p[1], 2) for p in prices]
        except Exception:
            return []

    def _fetch_volume_history(self) -> list:
        """Fetch volume data for 7 days."""
        try:
            url = (
                "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
                "?vs_currency=usd&days=7&interval=daily"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "BitBot/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            volumes = data.get("total_volumes", [])
            return [{"date": v[0], "volume": round(v[1], 0)} for v in volumes]
        except Exception:
            return []

    def _calculate_technical_indicators(self, prices: list) -> dict:
        """Calculate RSI(14), SMA(20), SMA(50), Bollinger Bands, ATR from price data."""
        result = {}
        if len(prices) < 14:
            return {"error": "insufficient data"}

        # RSI (14-period)
        gains = []
        losses = []
        for i in range(1, len(prices)):
            change = prices[i] - prices[i - 1]
            gains.append(max(change, 0))
            losses.append(max(-change, 0))

        if len(gains) >= 14:
            avg_gain = sum(gains[-14:]) / 14
            avg_loss = sum(losses[-14:]) / 14
            if avg_loss == 0:
                result["rsi_14"] = 100.0
            else:
                rs = avg_gain / avg_loss
                result["rsi_14"] = round(100 - (100 / (1 + rs)), 2)

        # SMA 20
        if len(prices) >= 20:
            result["sma_20"] = round(sum(prices[-20:]) / 20, 2)

        # SMA 50
        if len(prices) >= 50:
            result["sma_50"] = round(sum(prices[-50:]) / 50, 2)

        # Bollinger Bands (20-period, 2 std dev)
        if len(prices) >= 20:
            sma20 = sum(prices[-20:]) / 20
            variance = sum((p - sma20) ** 2 for p in prices[-20:]) / 20
            std_dev = math.sqrt(variance)
            result["bollinger"] = {
                "upper": round(sma20 + 2 * std_dev, 2),
                "middle": round(sma20, 2),
                "lower": round(sma20 - 2 * std_dev, 2),
                "bandwidth_pct": round((4 * std_dev / sma20) * 100, 3) if sma20 > 0 else 0,
            }

        # ATR (Average True Range) — approximate from hourly closes
        if len(prices) >= 15:
            true_ranges = []
            for i in range(1, min(len(prices), 15)):
                tr = abs(prices[i] - prices[i - 1])
                true_ranges.append(tr)
            result["atr_14"] = round(sum(true_ranges) / len(true_ranges), 2)
            # ATR as percentage of current price
            if prices[-1] > 0:
                result["atr_pct"] = round((result["atr_14"] / prices[-1]) * 100, 4)

        # Current price relative to Bollinger
        if "bollinger" in result and prices:
            bb = result["bollinger"]
            bb_range = bb["upper"] - bb["lower"]
            if bb_range > 0:
                result["bb_position"] = round(
                    (prices[-1] - bb["lower"]) / bb_range * 100, 1
                )  # 0 = at lower band, 100 = at upper band

        return result

    def _find_support_resistance(self, prices: list) -> dict:
        """Identify support and resistance levels from price pivots."""
        if len(prices) < 10:
            return {"supports": [], "resistances": []}

        supports = []
        resistances = []

        # Find local minima (supports) and maxima (resistances)
        window = 5
        for i in range(window, len(prices) - window):
            local_slice = prices[i - window:i + window + 1]
            if prices[i] == min(local_slice):
                supports.append(prices[i])
            if prices[i] == max(local_slice):
                resistances.append(prices[i])

        # Cluster nearby levels (within 0.3%)
        def cluster_levels(levels: list, threshold_pct: float = 0.3) -> list:
            if not levels:
                return []
            levels.sort()
            clusters = [[levels[0]]]
            for lvl in levels[1:]:
                if (lvl - clusters[-1][-1]) / clusters[-1][-1] * 100 < threshold_pct:
                    clusters[-1].append(lvl)
                else:
                    clusters.append([lvl])
            # Return average of each cluster, sorted by how many times tested
            result = [(round(sum(c) / len(c), 2), len(c)) for c in clusters]
            result.sort(key=lambda x: -x[1])  # most tested first
            return [{"price": r[0], "touches": r[1]} for r in result[:5]]

        return {
            "supports": cluster_levels(supports),
            "resistances": cluster_levels(resistances),
        }

    def _get_macro_events(self) -> list:
        """Return known upcoming macro events for 2026."""
        # Hardcoded FOMC and CPI dates for 2026
        events_2026 = [
            # FOMC meetings (announcement dates)
            {"date": "2026-01-28", "event": "FOMC Decision", "impact": "high"},
            {"date": "2026-03-18", "event": "FOMC Decision", "impact": "high"},
            {"date": "2026-04-29", "event": "FOMC Decision", "impact": "high"},
            {"date": "2026-06-17", "event": "FOMC Decision", "impact": "high"},
            {"date": "2026-07-29", "event": "FOMC Decision", "impact": "high"},
            {"date": "2026-09-16", "event": "FOMC Decision", "impact": "high"},
            {"date": "2026-10-28", "event": "FOMC Decision", "impact": "high"},
            {"date": "2026-12-16", "event": "FOMC Decision", "impact": "high"},
            # CPI releases (estimated dates)
            {"date": "2026-01-14", "event": "CPI Release", "impact": "high"},
            {"date": "2026-02-11", "event": "CPI Release", "impact": "high"},
            {"date": "2026-03-11", "event": "CPI Release", "impact": "high"},
            {"date": "2026-04-14", "event": "CPI Release", "impact": "high"},
            {"date": "2026-05-12", "event": "CPI Release", "impact": "high"},
            {"date": "2026-06-10", "event": "CPI Release", "impact": "high"},
            {"date": "2026-07-14", "event": "CPI Release", "impact": "high"},
            {"date": "2026-08-12", "event": "CPI Release", "impact": "high"},
            {"date": "2026-09-15", "event": "CPI Release", "impact": "high"},
            {"date": "2026-10-13", "event": "CPI Release", "impact": "high"},
            {"date": "2026-11-10", "event": "CPI Release", "impact": "high"},
            {"date": "2026-12-10", "event": "CPI Release", "impact": "high"},
            # Non-Farm Payrolls (estimated first Fridays)
            {"date": "2026-01-09", "event": "Non-Farm Payrolls", "impact": "medium"},
            {"date": "2026-02-06", "event": "Non-Farm Payrolls", "impact": "medium"},
            {"date": "2026-03-06", "event": "Non-Farm Payrolls", "impact": "medium"},
            {"date": "2026-04-03", "event": "Non-Farm Payrolls", "impact": "medium"},
            {"date": "2026-05-08", "event": "Non-Farm Payrolls", "impact": "medium"},
            {"date": "2026-06-05", "event": "Non-Farm Payrolls", "impact": "medium"},
            {"date": "2026-07-02", "event": "Non-Farm Payrolls", "impact": "medium"},
            {"date": "2026-08-07", "event": "Non-Farm Payrolls", "impact": "medium"},
            {"date": "2026-09-04", "event": "Non-Farm Payrolls", "impact": "medium"},
            {"date": "2026-10-02", "event": "Non-Farm Payrolls", "impact": "medium"},
            {"date": "2026-11-06", "event": "Non-Farm Payrolls", "impact": "medium"},
            {"date": "2026-12-04", "event": "Non-Farm Payrolls", "impact": "medium"},
            # Bitcoin-specific events
            {"date": "2026-04-15", "event": "US Tax Deadline", "impact": "medium"},
        ]

        today = datetime.now(timezone.utc).date()
        upcoming = []
        for evt in events_2026:
            try:
                evt_date = datetime.strptime(evt["date"], "%Y-%m-%d").date()
                days_until = (evt_date - today).days
                if 0 <= days_until <= 14:  # Next 14 days
                    upcoming.append({
                        **evt,
                        "days_until": days_until,
                    })
            except Exception:
                continue

        upcoming.sort(key=lambda x: x["days_until"])
        return upcoming

    def _get_margin_info(self) -> dict:
        """Get current margin/balance info."""
        try:
            balance = self.grid.exchange.get_balance()
            stable = balance.get("USDT", balance.get("USDC", 0.0))
            leverage = getattr(self.config, "leverage", 1)
            return {
                "available_usdc": round(stable, 2),
                "leverage": leverage,
                "buying_power": round(stable * leverage, 2),
            }
        except Exception:
            return {"available_usdc": 0, "leverage": 1, "buying_power": 0}

    def _ask_claude(self, data: dict) -> dict:
        # Build technical analysis section
        tech = data.get("technical", {})
        tech_section = ""
        if "error" not in tech:
            tech_section = f"""## Technical Indicators (from hourly data)
- RSI (14): {tech.get('rsi_14', 'N/A')}
- SMA 20: ${tech.get('sma_20', 'N/A')}
- SMA 50: ${tech.get('sma_50', 'N/A')}
- Bollinger Bands: Upper=${tech.get('bollinger', {}).get('upper', 'N/A')}, Middle=${tech.get('bollinger', {}).get('middle', 'N/A')}, Lower=${tech.get('bollinger', {}).get('lower', 'N/A')}
- Bollinger Bandwidth: {tech.get('bollinger', {}).get('bandwidth_pct', 'N/A')}%
- BB Position: {tech.get('bb_position', 'N/A')}% (0=lower band, 100=upper band)
- ATR (14): ${tech.get('atr_14', 'N/A')} ({tech.get('atr_pct', 'N/A')}% of price)
"""
        else:
            tech_section = "## Technical Indicators\nInsufficient data for calculation.\n"

        # Build S/R section
        sr = data.get("support_resistance", {})
        sr_section = "## Support & Resistance Levels\n"
        supports = sr.get("supports", [])
        resistances = sr.get("resistances", [])
        if supports:
            sr_section += "Supports:\n"
            for s in supports[:3]:
                sr_section += f"  - ${s['price']:,.2f} (tested {s['touches']}x)\n"
        if resistances:
            sr_section += "Resistances:\n"
            for r in resistances[:3]:
                sr_section += f"  - ${r['price']:,.2f} (tested {r['touches']}x)\n"
        if not supports and not resistances:
            sr_section += "No clear levels identified.\n"

        # Build macro events section
        macro = data.get("macro_events", [])
        macro_section = "## Upcoming Macro Events (next 14 days)\n"
        if macro:
            for evt in macro:
                macro_section += (
                    f"- {evt['event']} on {evt['date']} "
                    f"(in {evt['days_until']} days, impact: {evt['impact']})\n"
                )
        else:
            macro_section += "No major events in the next 14 days.\n"

        # Build volume section
        vol_history = data.get("volume_history", [])
        vol_section = "## Volume History (7 days)\n"
        if vol_history:
            vol_section += json.dumps(vol_history, indent=2) + "\n"
        else:
            vol_section += "No volume data available.\n"

        # Margin info
        margin = data.get("available_margin", {})

        # Exchange position info
        ex_pos = data.get("exchange_position", {})
        pos_section = f"""## Current Exchange Position
- Side: {ex_pos.get('side', 'none')}
- Size: {ex_pos.get('size', 0)} BTC
- Entry Price: ${ex_pos.get('entry_price', 0):,.2f}
- Unrealized P&L: ${ex_pos.get('unrealized_pnl', 0):,.2f}
"""

        prompt = f"""You are an expert crypto trading AI brain for a Bitcoin Grid Trading bot running on Hyperliquid.
You are NOT just a parameter advisor — you are the TRADING BRAIN that makes actual trade decisions.

## Current Bot State
- BTC Price: ${data['current_price']:,.2f}
- Grid Base Price: ${data['base_price']:,.2f}
- Price deviation from base: {data.get('price_deviation_pct', 0)}%
- Position: {data['position_btc']:.6f} BTC
- Realized P&L: ${data['realized_pnl']:,.2f}
- Unrealized P&L: ${data['unrealized_pnl']:,.2f}
- Trades executed: {data['trade_count']}
- Open buy orders: {data['open_buys']}
- Open sell orders: {data['open_sells']}
- Pending counter-orders (margin queued): {data.get('pending_counter_orders', 0)}

## Current Config
- Grid Spacing: {data['current_config']['grid_spacing_pct']}%
- Grid Levels: {data['current_config']['grid_levels']}
- Order Size: ${data['current_config']['order_size_usdt']}
- Leverage: {data['current_config']['leverage']}x
- Stop Loss: {data['current_config']['stop_loss_pct']}%

## Margin / Balance
- Available USDC: ${margin.get('available_usdc', 0):.2f}
- Current Leverage: {margin.get('leverage', 1)}x
- Buying Power: ${margin.get('buying_power', 0):.2f}

{pos_section}
{tech_section}
{sr_section}
## Fear & Greed Index
- Current: {data['fear_greed']['value']} ({data['fear_greed']['label']})
- 7-day history: {json.dumps(data['fear_greed'].get('history_7d', []))}

## Market Data (BTC)
{json.dumps(data.get('market', {}), indent=2)}

## 7-Day Price History
{json.dumps(data.get('price_history', []), indent=2)}

{vol_section}
{macro_section}
## Hyperliquid Funding Rate
{json.dumps(data.get('funding_rate', {}), indent=2)}

## Latest Crypto News (real-time)
{json.dumps(data.get('news', []), indent=2)}

IMPORTANT: Factor news sentiment into your decision. Positive news (ETF approval, adoption, institutional buying) = bullish bias. Negative news (hack, regulation, ban, lawsuit) = bearish bias. Weight recent news heavily for short-term signals.

## Your Task
You have TWO responsibilities:

### A) Grid Parameter Adjustments (existing)
Analyze all data and provide grid parameter recommendations.

### B) Direct Trade Signals (NEW — AI Trader Brain)
Based on your analysis, decide whether to execute a direct trade signal.
Use technical indicators, support/resistance, trend, fear & greed, and volume to decide.

**Signal Decision Logic:**
- RSI < 30 (oversold) + price bouncing off support + bullish SMA crossover = potential LONG
- RSI > 70 (overbought) + price rejected at resistance + bearish SMA crossover = potential SHORT
- Extreme Fear (F&G < 20) = contrarian LONG opportunity
- Extreme Greed (F&G > 80) = consider SHORT or CLOSE
- SMA20 > SMA50 = bullish bias (favor longs)
- SMA20 < SMA50 = bearish bias (favor shorts)
- Price at lower Bollinger Band with volume confirmation = LONG
- Price at upper Bollinger Band with volume divergence = SHORT
- If already in a winning position with deteriorating signals = CLOSE
- If no clear signal = HOLD (let the grid work)

Provide a JSON recommendation with ALL these fields:

### Grid Parameters (existing):
1. **market_outlook**: "bullish", "bearish", or "neutral"
2. **confidence**: 1-10 (how confident you are)
3. **grid_spacing_pct**: recommended spacing (0.3 to 2.0)
4. **leverage**: recommended leverage (1 to 5)
5. **stop_loss_pct**: recommended stop loss (3.0 to 10.0)
6. **action**: "continue", "pause", "reset_grid", or "adjust"
7. **rebase_grid**: true/false
8. **reason**: 2-3 sentence explanation in Portuguese
9. **risk_level**: "low", "medium", "high"
10. **technical_summary**: 1-2 sentences about the technical picture

### Trade Signal (NEW):
11. **signal**: "long", "short", "close", or "hold"
12. **signal_size_pct**: percentage of available balance to use for the trade (10 to 50). Set 0 if signal is "hold".
13. **signal_reason**: specific reason for the trade signal in Portuguese (1-2 sentences)
14. **take_profit_pct**: recommended take profit percentage from entry (0.5 to 5.0). Set 0 if signal is "hold".
15. **stop_loss_price**: specific stop loss price for this AI trade signal. Set 0 if signal is "hold".
16. **grid_buy_bias**: number of buy levels (out of total grid_levels) — more buys = bullish bias. Default is half of grid_levels.
17. **grid_sell_bias**: number of sell levels (out of total grid_levels) — more sells = bearish bias. Default is half of grid_levels.

### Dynamic Targets (CRITICAL - update every analysis):
18. **dynamic_stop_loss**: optimal stop loss price RIGHT NOW based on current support levels, ATR, and market conditions. Must be a specific price, not percentage. Use nearest strong support for longs, nearest resistance for shorts. Set 0 if no position.
19. **dynamic_target_1**: conservative take profit price based on nearest resistance (for longs) or support (for shorts). Set 0 if no position.
20. **dynamic_target_2**: aggressive take profit price based on next major resistance/support. Set 0 if no position.
21. **targets_reason**: 1 sentence explaining why these targets were chosen (e.g., "Support at $67,500 based on 7-day low, resistance at $71,200 based on recent rejection")

## Critical Rules:

### Grid Rebase (IMPORTANT)
- If price has moved MORE THAN 3% from grid base price, MUST recommend "reset_grid" with "rebase_grid": true
- Current deviation is {data.get('price_deviation_pct', 0)}%.

### Macro Event Caution
- If ANY high-impact event is within 2 days, recommend "pause" and signal "hold"
- If a high-impact event is 3-7 days away, widen spacing, reduce leverage, signal "hold"

### Leverage Based on Volatility (ATR)
- ATR% > 2%: leverage 1x
- ATR% 1-2%: leverage 2x
- ATR% < 1%: leverage up to 3x
- NEVER leverage > 3x with small capital

### Trade Signal Safety Rules
- NEVER signal "long" or "short" with confidence < 7
- Maximum signal_size_pct is 30% (capital preservation)
- If there is already an open position in the OPPOSITE direction, signal "close" first, never reverse directly
- Current position side: {ex_pos.get('side', 'none')}, size: {ex_pos.get('size', 0)} BTC
- Always set a realistic stop_loss_price (use support/resistance levels)
- If signal is "hold", set signal_size_pct=0, take_profit_pct=0, stop_loss_price=0

### Grid Bias (Smart Grid Positioning)
- signal "long" -> grid_buy_bias should be ~70% of grid_levels (e.g., 7 buys, 3 sells for 10 levels)
- signal "short" -> grid_sell_bias should be ~70% of grid_levels (e.g., 3 buys, 7 sells for 10 levels)
- signal "hold" -> balanced (50/50 split)
- grid_buy_bias + grid_sell_bias MUST equal grid_levels ({data['current_config']['grid_levels']})

### Capital Preservation
- Capital is limited — preservation is #1 priority
- Fear & Greed < 25: buying opportunity
- Fear & Greed > 75: reduce exposure
- If pending counter-orders exist, reduce order size

### Volume Analysis
- Declining volume + rising price = weakening trend, avoid new longs
- Rising volume + price move = strong trend, follow it
- Low volume = choppy, good for grid, signal "hold"

Respond ONLY with valid JSON, no other text."""

        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        })

        response = self.bedrock.invoke_model(
            modelId="us.anthropic.claude-3-5-haiku-20241022-v1:0",
            contentType="application/json",
            accept="application/json",
            body=body,
        )

        result = json.loads(response["body"].read())
        text = result["content"][0]["text"].strip()

        # Extract JSON from response
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()

        recommendation = json.loads(text)
        self.last_recommendation = recommendation
        tech_summary = recommendation.get("technical_summary", "")
        rebase = recommendation.get("rebase_grid", False)
        signal = recommendation.get("signal", "hold")
        self.last_signal = signal
        self.last_report = (
            f"AI Analysis ({datetime.now(timezone.utc).strftime('%H:%M UTC')})\n"
            f"Outlook: {recommendation.get('market_outlook', '?').upper()} "
            f"(confidence: {recommendation.get('confidence', '?')}/10)\n"
            f"Risk: {recommendation.get('risk_level', '?')}\n"
            f"Action: {recommendation.get('action', '?')}"
            f"{' + REBASE' if rebase else ''}\n"
            f"Signal: {signal.upper()}"
            f"{' (' + str(recommendation.get('signal_size_pct', 0)) + '%)' if signal != 'hold' else ''}\n"
            f"Technical: {tech_summary}\n"
            f"Reason: {recommendation.get('reason', '?')}\n"
            f"Signal Reason: {recommendation.get('signal_reason', '-')}\n"
            f"Recommended: spacing={recommendation.get('grid_spacing_pct')}%, "
            f"leverage={recommendation.get('leverage')}x, "
            f"stop_loss={recommendation.get('stop_loss_pct')}%\n"
            f"Grid Bias: {recommendation.get('grid_buy_bias', '?')} buys / "
            f"{recommendation.get('grid_sell_bias', '?')} sells"
        )
        return recommendation

    async def _apply_recommendation(self, rec: dict, current_price: float):
        action = rec.get("action", "continue")
        changes = []

        # ── 0) Update dynamic targets ──
        new_sl = rec.get("dynamic_stop_loss", 0)
        new_t1 = rec.get("dynamic_target_1", 0)
        new_t2 = rec.get("dynamic_target_2", 0)
        new_tr = rec.get("targets_reason", "")

        if new_sl > 0:
            old_sl = self.dynamic_stop_loss
            old_t1 = self.dynamic_target_1

            # TRAILING RULE: never lower the stop loss (only raise it)
            if old_sl > 0 and new_sl < old_sl:
                new_sl = old_sl  # keep the higher SL

            # TRAILING RULE: if price moved up significantly, raise SL to lock profit
            pos = self._get_exchange_position()
            pos_side = pos.get("side", "none")
            entry = pos.get("entry_price", 0)
            if pos_side == "long" and entry > 0 and current_price > entry:
                profit_pct = (current_price - entry) / entry * 100
                # If profit > 1%, set SL at breakeven minimum
                if profit_pct > 1.0 and new_sl < entry:
                    new_sl = round(entry + 10, 1)  # SL at entry + $10 (breakeven)
                # If profit > 2%, trail SL at 1% below current price
                if profit_pct > 2.0:
                    trailing_sl = round(current_price * 0.99, 1)
                    if trailing_sl > new_sl:
                        new_sl = trailing_sl

            self.dynamic_stop_loss = new_sl
            self.dynamic_target_1 = new_t1
            self.dynamic_target_2 = new_t2
            self.targets_reason = new_tr

            # Check if targets actually changed enough to update exchange
            sl_changed = old_sl == 0 or abs(new_sl - old_sl) > 20
            tp_changed = old_t1 == 0 or abs(new_t1 - old_t1) > 20

            if (sl_changed or tp_changed) and self.config.mode == "real" and hasattr(self.grid.exchange, "ccxt_client"):
                ex = self.grid.exchange.ccxt_client
                try:
                    pos_size = pos.get("size", 0)

                    if pos_size > 0:
                        # Cancel existing SL/TP orders
                        try:
                            open_orders = ex.fetch_open_orders(self.config.symbol)
                            cancelled = 0
                            for o in open_orders:
                                if o.get("reduceOnly"):
                                    ex.cancel_order(o["id"], self.config.symbol)
                                    cancelled += 1
                            if cancelled > 0:
                                import time
                                time.sleep(1)
                        except Exception as e:
                            print(f"  Warning: cancel SL/TP failed: {e}")

                        # Place new stop loss
                        if pos_side == "long" and new_sl > 0:
                            try:
                                ex.create_order(
                                    self.config.symbol, "limit", "sell",
                                    pos_size, new_sl,
                                    {"reduceOnly": True, "triggerPrice": new_sl}
                                )
                            except Exception as e:
                                print(f"  Warning: place SL failed: {e}")

                        # Place new take profit (half at T1, half at T2 if available)
                        if pos_side == "long" and new_t1 > 0:
                            if new_t2 > 0 and new_t2 > new_t1:
                                # Split: half at T1, half at T2
                                half = round(pos_size / 2, 5)
                                remainder = round(pos_size - half, 5)
                                try:
                                    ex.create_limit_sell_order(
                                        self.config.symbol, half, new_t1,
                                        {"reduceOnly": True}
                                    )
                                    ex.create_limit_sell_order(
                                        self.config.symbol, remainder, new_t2,
                                        {"reduceOnly": True}
                                    )
                                except Exception as e:
                                    # Fallback: full size at T1
                                    try:
                                        ex.create_limit_sell_order(
                                            self.config.symbol, pos_size, new_t1,
                                            {"reduceOnly": True}
                                        )
                                    except Exception as e2:
                                        print(f"  Warning: place TP failed: {e2}")
                            else:
                                try:
                                    ex.create_limit_sell_order(
                                        self.config.symbol, pos_size, new_t1,
                                        {"reduceOnly": True}
                                    )
                                except Exception as e:
                                    print(f"  Warning: place TP failed: {e}")

                        changes.append(f"SL: ${new_sl:,.0f} T1: ${new_t1:,.0f} T2: ${new_t2:,.0f} (exchange updated)")
                except Exception as e:
                    changes.append(f"SL/TP update failed: {e}")

            if sl_changed or tp_changed:
                self.notifier.send(
                    f"Targets atualizados:\n"
                    f"Stop Loss: ${new_sl:,.2f}"
                    f"{' (trailing up!)' if old_sl > 0 and new_sl > old_sl else ''}\n"
                    f"Target 1: ${new_t1:,.2f} (50%)\n"
                    f"Target 2: ${new_t2:,.2f} (50%)\n"
                    f"Motivo: {new_tr}"
                )

        # ── A) Apply grid parameter adjustments (existing functionality) ──

        # Apply spacing change
        new_spacing = rec.get("grid_spacing_pct")
        if new_spacing and 0.3 <= new_spacing <= 2.0:
            if abs(new_spacing - self.config.grid_spacing_pct) >= 0.1:
                self.config.grid_spacing_pct = new_spacing
                changes.append(f"spacing={new_spacing}%")

        # Apply leverage change
        new_leverage = rec.get("leverage")
        if new_leverage and 1 <= new_leverage <= 5:
            if new_leverage != self.config.leverage:
                old_lev = self.config.leverage
                self.config.leverage = new_leverage
                # Sync leverage to exchange
                if self.config.mode == "real" and hasattr(self.grid.exchange, "ccxt_client"):
                    try:
                        self.grid.exchange.ccxt_client.set_leverage(new_leverage, self.config.symbol)
                        changes.append(f"leverage={old_lev}x→{new_leverage}x (exchange updated)")
                    except Exception as e:
                        changes.append(f"leverage={new_leverage}x (exchange FAILED: {e})")
                else:
                    changes.append(f"leverage={new_leverage}x")

        # Apply stop loss change
        new_sl = rec.get("stop_loss_pct")
        if new_sl and 3.0 <= new_sl <= 10.0:
            if abs(new_sl - self.config.stop_loss_pct) >= 0.5:
                self.config.stop_loss_pct = new_sl
                changes.append(f"stop_loss={new_sl}%")

        # Apply action
        if action == "pause" and not self.grid.paused:
            self.grid.paused = True
            changes.append("PAUSED")
        elif action == "reset_grid":
            await self.grid.reset()
            changes.append("GRID RESET (rebase to current price)")
        elif action == "adjust" and rec.get("rebase_grid"):
            # Rebase grid to current price without full reset of P&L tracking
            await self.grid.cancel_all()
            self.grid.base_price = current_price
            await self.grid._place_grid()
            changes.append(f"GRID REBASED to ${current_price:,.2f}")
        elif action == "continue" and self.grid.paused:
            # Don't auto-resume, only AI pause should be manual resume
            pass

        # ── B) Apply trade signal (NEW AI trader brain) ──

        signal = rec.get("signal", "hold")
        confidence = rec.get("confidence", 0)
        signal_size_pct = rec.get("signal_size_pct", 0)
        signal_reason = rec.get("signal_reason", "")
        take_profit_pct = rec.get("take_profit_pct", 0)
        stop_loss_price = rec.get("stop_loss_price", 0)

        if signal != "hold" and confidence >= self.MIN_SIGNAL_CONFIDENCE:
            # Enforce max position size
            if signal_size_pct > self.MAX_POSITION_PCT:
                signal_size_pct = self.MAX_POSITION_PCT

            signal_result = await self._execute_signal(
                signal=signal,
                size_pct=signal_size_pct,
                current_price=current_price,
                take_profit_pct=take_profit_pct,
                stop_loss_price=stop_loss_price,
                reason=signal_reason,
            )
            if signal_result:
                changes.append(signal_result)
        elif signal != "hold" and confidence < self.MIN_SIGNAL_CONFIDENCE:
            changes.append(
                f"Signal {signal.upper()} SKIPPED (confidence {confidence}/10 < "
                f"minimum {self.MIN_SIGNAL_CONFIDENCE})"
            )

        # ── C) Apply grid bias (smart grid positioning) ──

        grid_buy_bias = rec.get("grid_buy_bias")
        grid_sell_bias = rec.get("grid_sell_bias")
        if grid_buy_bias is not None and grid_sell_bias is not None:
            total = grid_buy_bias + grid_sell_bias
            expected_levels = self.config.grid_levels
            # Only rebalance grid if bias changed significantly
            current_buys = len(self.grid.buy_orders)
            current_sells = len(self.grid.sell_orders)
            if total == expected_levels and (
                abs(current_buys - grid_buy_bias) >= 2
                or abs(current_sells - grid_sell_bias) >= 2
            ):
                # Need to rebalance grid with new bias
                bias_result = await self._apply_grid_bias(
                    grid_buy_bias, grid_sell_bias, current_price
                )
                if bias_result:
                    changes.append(bias_result)

        # Notify about analysis
        msg = self.last_report
        if changes:
            msg += f"\nChanges applied: {', '.join(changes)}"
        else:
            msg += "\nNo changes needed."
        self.notifier.send(msg)

    async def _execute_signal(
        self,
        signal: str,
        size_pct: float,
        current_price: float,
        take_profit_pct: float,
        stop_loss_price: float,
        reason: str,
    ) -> str | None:
        """Execute an AI trade signal. Returns a description string or None."""
        symbol = self.config.symbol
        is_paper = self.config.mode != "real"

        # Get available balance
        try:
            balance = self.grid.exchange.get_balance()
            available = float(balance.get("USDC", balance.get("USDT", 0.0)))
        except Exception:
            available = 0.0

        if available <= 0:
            return "Signal SKIPPED (no available balance)"

        # Get current position from exchange
        ex_pos = self._get_exchange_position()
        pos_side = ex_pos.get("side", "none")
        pos_size = ex_pos.get("size", 0)

        # ── CLOSE signal ──
        if signal == "close":
            if pos_side == "none" or pos_size == 0:
                return "CLOSE signal — no position to close"
            return await self._close_position(symbol, pos_side, pos_size, current_price, reason, is_paper)

        # ── LONG signal ──
        if signal == "long":
            # Safety: if we have a short position, close it first
            if pos_side == "short" and pos_size > 0:
                close_msg = await self._close_position(
                    symbol, "short", pos_size, current_price,
                    "Closing short before opening long", is_paper
                )
                if close_msg:
                    self.notifier.send(f"AI Trade: {close_msg}")

            # Check position limit (max 30% of balance)
            trade_usdc = available * size_pct / 100
            max_allowed = available * self.MAX_POSITION_PCT / 100
            if trade_usdc > max_allowed:
                trade_usdc = max_allowed

            size_btc = round(trade_usdc / current_price, 5)
            if size_btc <= 0 or trade_usdc < 10:
                return "LONG signal — order too small (< $10)"

            entry_price = round(current_price * 0.999, 1)  # slight discount
            tp_price = round(current_price * (1 + take_profit_pct / 100), 1) if take_profit_pct > 0 else 0

            return await self._place_ai_order(
                symbol=symbol,
                side="buy",
                size_btc=size_btc,
                price=entry_price,
                stop_loss_price=stop_loss_price,
                tp_price=tp_price,
                reason=reason,
                is_paper=is_paper,
                trade_usdc=trade_usdc,
            )

        # ── SHORT signal ──
        if signal == "short":
            # Safety: if we have a long position, close it first
            if pos_side == "long" and pos_size > 0:
                close_msg = await self._close_position(
                    symbol, "long", pos_size, current_price,
                    "Closing long before opening short", is_paper
                )
                if close_msg:
                    self.notifier.send(f"AI Trade: {close_msg}")

            trade_usdc = available * size_pct / 100
            max_allowed = available * self.MAX_POSITION_PCT / 100
            if trade_usdc > max_allowed:
                trade_usdc = max_allowed

            size_btc = round(trade_usdc / current_price, 5)
            if size_btc <= 0 or trade_usdc < 10:
                return "SHORT signal — order too small (< $10)"

            entry_price = round(current_price * 1.001, 1)  # slight premium
            tp_price = round(current_price * (1 - take_profit_pct / 100), 1) if take_profit_pct > 0 else 0

            return await self._place_ai_order(
                symbol=symbol,
                side="sell",
                size_btc=size_btc,
                price=entry_price,
                stop_loss_price=stop_loss_price,
                tp_price=tp_price,
                reason=reason,
                is_paper=is_paper,
                trade_usdc=trade_usdc,
            )

        return None

    async def _place_ai_order(
        self,
        symbol: str,
        side: str,
        size_btc: float,
        price: float,
        stop_loss_price: float,
        tp_price: float,
        reason: str,
        is_paper: bool,
        trade_usdc: float,
    ) -> str:
        """Place an AI-initiated order (buy or sell) with stop loss."""
        signal_label = "LONG" if side == "buy" else "SHORT"

        if is_paper:
            # Paper mode: simulate the trade
            trade = {
                "side": side,
                "price": price,
                "amount": size_btc,
                "fee": size_btc * price * 0.0005,
                "pnl": 0.0,
                "mode": f"paper_ai_{signal_label.lower()}",
            }
            log_trade(trade, self.config.trade_log)
            self.ai_trade_count += 1

            msg = (
                f"AI {signal_label} (PAPER): {size_btc:.5f} BTC @ ${price:,.2f} "
                f"(${trade_usdc:.2f} USDC)"
            )
            if stop_loss_price > 0:
                msg += f" | SL: ${stop_loss_price:,.2f}"
            if tp_price > 0:
                msg += f" | TP: ${tp_price:,.2f}"
            msg += f" | Reason: {reason}"
            return msg

        # Real mode: place order via ccxt
        try:
            ccxt_client = self.grid.exchange.ccxt_client

            if side == "buy":
                order = ccxt_client.create_limit_buy_order(symbol, size_btc, price)
            else:
                order = ccxt_client.create_limit_sell_order(symbol, size_btc, price)

            order_id = order.get("id", "unknown")
            self.ai_trade_count += 1

            # Place stop loss order if specified
            sl_msg = ""
            if stop_loss_price > 0:
                try:
                    sl_side = "sell" if side == "buy" else "buy"
                    sl_params = {"reduceOnly": True, "triggerPrice": stop_loss_price}
                    ccxt_client.create_order(
                        symbol, "stop", sl_side, size_btc, stop_loss_price, sl_params
                    )
                    sl_msg = f" | SL placed: ${stop_loss_price:,.2f}"
                except Exception as e:
                    sl_msg = f" | SL FAILED: {e}"

            # Place take profit order if specified
            tp_msg = ""
            if tp_price > 0:
                try:
                    tp_side = "sell" if side == "buy" else "buy"
                    tp_params = {"reduceOnly": True}
                    if tp_side == "sell":
                        ccxt_client.create_limit_sell_order(
                            symbol, size_btc, tp_price, tp_params
                        )
                    else:
                        ccxt_client.create_limit_buy_order(
                            symbol, size_btc, tp_price, tp_params
                        )
                    tp_msg = f" | TP placed: ${tp_price:,.2f}"
                except Exception as e:
                    tp_msg = f" | TP FAILED: {e}"

            trade = {
                "side": side,
                "price": price,
                "amount": size_btc,
                "fee": size_btc * price * 0.0005,
                "pnl": 0.0,
                "mode": f"real_ai_{signal_label.lower()}",
            }
            log_trade(trade, self.config.trade_log)

            msg = (
                f"AI {signal_label}: {size_btc:.5f} BTC @ ${price:,.2f} "
                f"(${trade_usdc:.2f} USDC) [order {order_id}]"
                f"{sl_msg}{tp_msg}"
                f" | Reason: {reason}"
            )
            return msg

        except Exception as e:
            return f"AI {signal_label} FAILED: {e}"

    async def _close_position(
        self,
        symbol: str,
        pos_side: str,
        pos_size: float,
        current_price: float,
        reason: str,
        is_paper: bool,
    ) -> str:
        """Close an existing position."""
        close_side = "sell" if pos_side == "long" else "buy"

        if is_paper:
            trade = {
                "side": close_side,
                "price": current_price,
                "amount": pos_size,
                "fee": pos_size * current_price * 0.0005,
                "pnl": 0.0,
                "mode": "paper_ai_close",
            }
            log_trade(trade, self.config.trade_log)
            self.ai_trade_count += 1
            return (
                f"AI CLOSE (PAPER): {close_side} {pos_size:.5f} BTC @ ${current_price:,.2f}"
                f" | Reason: {reason}"
            )

        # Real mode
        try:
            ccxt_client = self.grid.exchange.ccxt_client
            params = {"reduceOnly": True}
            if close_side == "sell":
                order = ccxt_client.create_market_sell_order(symbol, pos_size, params)
            else:
                order = ccxt_client.create_market_buy_order(symbol, pos_size, params)

            order_id = order.get("id", "unknown")
            self.ai_trade_count += 1

            trade = {
                "side": close_side,
                "price": current_price,
                "amount": pos_size,
                "fee": pos_size * current_price * 0.0005,
                "pnl": 0.0,
                "mode": "real_ai_close",
            }
            log_trade(trade, self.config.trade_log)
            return (
                f"AI CLOSE: {close_side} {pos_size:.5f} BTC @ ~${current_price:,.2f}"
                f" [order {order_id}] | Reason: {reason}"
            )

        except Exception as e:
            return f"AI CLOSE FAILED: {e}"

    async def _apply_grid_bias(
        self, buy_levels: int, sell_levels: int, current_price: float
    ) -> str | None:
        """Rebalance the grid with a buy/sell bias (smart grid positioning)."""
        try:
            # Cancel existing grid orders
            await self.grid.cancel_all()

            spacing = self.config.grid_spacing_pct / 100
            order_usdt = self.config.order_size_usdt

            available_margin = self.grid._get_available_margin()
            margin_per_order = self.grid._margin_needed_for_order(order_usdt)
            usable_margin = available_margin * 0.90
            max_orders = int(usable_margin / margin_per_order) if margin_per_order > 0 else 0

            if max_orders == 0:
                return "Grid bias SKIPPED (no margin)"

            # Place buy levels below current price
            buys_placed = 0
            orders_placed = 0
            for i in range(1, buy_levels + 1):
                if orders_placed >= max_orders:
                    break
                buy_price = round(self.grid.base_price * (1 - spacing * i), 1)
                buy_amount = round(order_usdt / buy_price, 5)
                try:
                    order = await self.grid.exchange.place_limit_buy(
                        self.grid.symbol, buy_amount, buy_price
                    )
                    self.grid.buy_orders[order.id] = {"order": order, "level_price": buy_price}
                    buys_placed += 1
                    orders_placed += 1
                except Exception:
                    pass

            # Place sell levels above current price
            sells_placed = 0
            for i in range(1, sell_levels + 1):
                if orders_placed >= max_orders:
                    break
                sell_price = round(self.grid.base_price * (1 + spacing * i), 1)
                sell_amount = round(order_usdt / sell_price, 5)
                try:
                    order = await self.grid.exchange.place_limit_sell(
                        self.grid.symbol, sell_amount, sell_price
                    )
                    self.grid.sell_orders[order.id] = {"order": order, "level_price": sell_price}
                    sells_placed += 1
                    orders_placed += 1
                except Exception:
                    pass

            return f"Grid REBIASED: {buys_placed} buys / {sells_placed} sells"

        except Exception as e:
            return f"Grid bias FAILED: {e}"

    def get_status(self) -> dict:
        return {
            "last_analysis": self.last_analysis_time.isoformat() if self.last_analysis_time else None,
            "last_report": self.last_report,
            "recommendation": self.last_recommendation,
            "next_analysis_in": self._time_to_next(),
            "last_signal": self.last_signal,
            "ai_trade_count": self.ai_trade_count,
            "dynamic_targets": {
                "stop_loss": self.dynamic_stop_loss,
                "target_1": self.dynamic_target_1,
                "target_2": self.dynamic_target_2,
                "reason": self.targets_reason,
            },
        }

    def _time_to_next(self) -> str:
        if self.last_analysis_time is None:
            return "imminent"
        next_time = self.last_analysis_time + timedelta(minutes=self.analysis_interval_minutes)
        remaining = next_time - datetime.now(timezone.utc)
        if remaining.total_seconds() <= 0:
            return "imminent"
        hours = int(remaining.total_seconds() // 3600)
        minutes = int((remaining.total_seconds() % 3600) // 60)
        return f"{hours}h {minutes}m"
