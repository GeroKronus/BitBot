"""AI Market Analyst v3 — Filter only, no trade execution.

The AI serves as a FILTER that influences grid parameters.
It does NOT execute trades directly.

Hierarchy: Kill Switch > Regime Detector > AI Filter > Grid
"""

import json
import math
import os
import urllib.request
from datetime import datetime, timezone, timedelta

import boto3

from .logger import log_trade


class MarketAnalyst:
    """AI analyst that classifies market and adjusts grid parameters.

    Changes from v2:
    - Removed trade execution (long/short/close signals)
    - Simplified prompt (~500 tokens vs ~2500)
    - Only 6 output fields
    - Interval: 30 minutes (was 5)
    - AI as filter, not executor
    """

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
        self.analysis_interval_minutes = 30  # every 30 min (was 5)
        self.last_report = "No analysis yet"
        self.last_recommendation = {}
        self.last_collected_data = {}

        # Dynamic targets
        self.dynamic_stop_loss = 0.0
        self.dynamic_target_1 = 0.0
        self.dynamic_target_2 = 0.0
        self.targets_reason = ""

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
            self._apply_recommendation(recommendation, current_price)
            self.last_analysis_time = datetime.now(timezone.utc)
        except Exception as e:
            self.notifier.send(f"AI Analyst error: {e}")

    def _collect_market_data(self, current_price: float) -> dict:
        data = {
            "current_price": current_price,
            "base_price": self.grid.base_price,
        }
        data["fear_greed"] = self._fetch_fear_greed()
        data["market"] = self._fetch_market_data()

        # Hourly prices for technical indicators
        hourly = self._fetch_hourly_prices()
        data["hourly_prices"] = hourly
        data["technical"] = self._calculate_technical_indicators(hourly)
        data["support_resistance"] = self._find_support_resistance(hourly)
        data["funding_rate"] = self._fetch_funding_rate()
        data["news"] = self._fetch_crypto_news()

        self.last_collected_data = data
        return data

    # --- Data collection (kept from v2, unchanged) ---

    def _fetch_fear_greed(self) -> dict:
        try:
            url = "https://api.alternative.me/fng/?limit=7"
            req = urllib.request.Request(url, headers={"User-Agent": "BitBot/3.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
            entries = result.get("data", [])
            if entries:
                return {
                    "value": int(entries[0]["value"]),
                    "label": entries[0]["value_classification"],
                }
        except Exception:
            pass
        return {"value": 50, "label": "Neutral"}

    def _fetch_market_data(self) -> dict:
        try:
            url = (
                "https://api.coingecko.com/api/v3/coins/bitcoin"
                "?localization=false&tickers=false&community_data=false"
                "&developer_data=false&sparkline=false"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "BitBot/3.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            md = data.get("market_data", {})
            return {
                "price_change_24h_pct": round(md.get("price_change_percentage_24h", 0), 2),
                "price_change_7d_pct": round(md.get("price_change_percentage_7d", 0), 2),
                "high_24h": md.get("high_24h", {}).get("usd", 0),
                "low_24h": md.get("low_24h", {}).get("usd", 0),
            }
        except Exception:
            return {}

    def _fetch_hourly_prices(self) -> list:
        try:
            url = (
                "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
                "?vs_currency=usd&days=3&interval=hourly"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "BitBot/3.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            return [round(p[1], 2) for p in data.get("prices", [])]
        except Exception:
            return []

    def _fetch_funding_rate(self) -> float:
        try:
            if hasattr(self.grid.exchange, "ccxt_client"):
                fr = self.grid.exchange.ccxt_client.fetch_funding_rate("BTC/USDC:USDC")
                return float(fr.get("fundingRate", 0) or 0)
        except Exception:
            pass
        return 0.0

    def _fetch_crypto_news(self) -> list:
        try:
            url = "https://cryptopanic.com/api/free/v1/posts/?auth_token=free&currencies=BTC&kind=news&num_results=5"
            req = urllib.request.Request(url, headers={"User-Agent": "BitBot/3.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            results = []
            for post in data.get("results", [])[:5]:
                votes = post.get("votes", {})
                pos = votes.get("positive", 0)
                neg = votes.get("negative", 0)
                sentiment = "positive" if pos > neg else ("negative" if neg > pos else "neutral")
                results.append({"title": post.get("title", ""), "sentiment": sentiment})
            return results
        except Exception:
            return []

    def _calculate_technical_indicators(self, prices: list) -> dict:
        if len(prices) < 20:
            return {"error": "insufficient data"}

        # RSI (14)
        gains, losses = [], []
        for i in range(1, min(15, len(prices))):
            diff = prices[-i] - prices[-i-1]
            if diff > 0:
                gains.append(diff)
            else:
                losses.append(abs(diff))
        avg_gain = sum(gains) / 14 if gains else 0.001
        avg_loss = sum(losses) / 14 if losses else 0.001
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        # SMA 20 and 50
        sma_20 = sum(prices[-20:]) / 20
        sma_50 = sum(prices[-50:]) / min(50, len(prices)) if len(prices) >= 20 else sma_20

        # Bollinger Bands (20, 2)
        recent_20 = prices[-20:]
        bb_std = (sum((p - sma_20)**2 for p in recent_20) / 20) ** 0.5
        bb_upper = sma_20 + 2 * bb_std
        bb_lower = sma_20 - 2 * bb_std
        bb_bandwidth = (bb_upper - bb_lower) / sma_20 * 100 if sma_20 > 0 else 0

        # ATR (14) — using close-to-close (approximate)
        tr_values = [abs(prices[-i] - prices[-i-1]) for i in range(1, min(15, len(prices)))]
        atr = sum(tr_values) / len(tr_values) if tr_values else 0

        return {
            "rsi": round(rsi, 2),
            "sma_20": round(sma_20, 2),
            "sma_50": round(sma_50, 2),
            "bb_upper": round(bb_upper, 2),
            "bb_lower": round(bb_lower, 2),
            "bb_middle": round(sma_20, 2),
            "bb_bandwidth": round(bb_bandwidth, 2),
            "atr": round(atr, 2),
            "atr_pct": round(atr / prices[-1] * 100, 3) if prices[-1] > 0 else 0,
        }

    def _find_support_resistance(self, prices: list) -> dict:
        if len(prices) < 20:
            return {"supports": [], "resistances": []}
        supports, resistances = [], []
        window = 5
        for i in range(window, len(prices) - window):
            if all(prices[i] <= prices[i-j] for j in range(1, window+1)) and \
               all(prices[i] <= prices[i+j] for j in range(1, window+1)):
                supports.append(prices[i])
            if all(prices[i] >= prices[i-j] for j in range(1, window+1)) and \
               all(prices[i] >= prices[i+j] for j in range(1, window+1)):
                resistances.append(prices[i])

        # Cluster nearby levels
        def cluster(levels, threshold_pct=0.3):
            if not levels:
                return []
            levels.sort()
            clusters = []
            current = [levels[0]]
            for lv in levels[1:]:
                if abs(lv - current[0]) / current[0] * 100 < threshold_pct:
                    current.append(lv)
                else:
                    clusters.append({"price": round(sum(current)/len(current), 2), "touches": len(current)})
                    current = [lv]
            clusters.append({"price": round(sum(current)/len(current), 2), "touches": len(current)})
            return sorted(clusters, key=lambda x: x["touches"], reverse=True)[:3]

        return {"supports": cluster(supports), "resistances": cluster(resistances)}

    # --- AI prompt (SIMPLIFIED — ~500 tokens, 6 fields) ---

    def _ask_claude(self, data: dict) -> dict:
        tech = data.get("technical", {})
        fg = data.get("fear_greed", {})
        mkt = data.get("market", {})
        sr = data.get("support_resistance", {})
        fr = data.get("funding_rate", 0)
        news = data.get("news", [])

        # Summarize news in 1 line
        news_summary = ""
        if news:
            pos = len([n for n in news if n["sentiment"] == "positive"])
            neg = len([n for n in news if n["sentiment"] == "negative"])
            news_summary = f"News: {pos} positive, {neg} negative, {len(news)-pos-neg} neutral"
            top_headline = news[0]["title"] if news else ""
            news_summary += f". Top: {top_headline}"

        supports = ", ".join(f"${s['price']:,.0f}" for s in sr.get("supports", [])[:2])
        resistances = ", ".join(f"${r['price']:,.0f}" for r in sr.get("resistances", [])[:2])

        prompt = f"""BTC grid trading bot analyst. Answer in Portuguese. JSON only.

Price: ${data['current_price']:,.2f} | 24h: {mkt.get('price_change_24h_pct', 0)}% | 7d: {mkt.get('price_change_7d_pct', 0)}%
RSI: {tech.get('rsi', 50)} | SMA20: ${tech.get('sma_20', 0):,.0f} | SMA50: ${tech.get('sma_50', 0):,.0f}
BB: {tech.get('bb_bandwidth', 0):.1f}% width | ATR: ${tech.get('atr', 0):,.0f} ({tech.get('atr_pct', 0):.2f}%)
Fear&Greed: {fg.get('value', 50)} ({fg.get('label', 'Neutral')})
Funding: {fr*100:.4f}% | Supports: {supports or 'none'} | Resistances: {resistances or 'none'}
{news_summary}

Return JSON with exactly 6 fields:
1. "outlook": "bullish", "bearish", or "neutral"
2. "confidence": 1-10
3. "risk_level": "low", "medium", or "high"
4. "grid_spacing_pct": recommended spacing (0.3 to 2.0)
5. "reason": 1-2 sentences in Portuguese explaining your view
6. "dynamic_stop_loss": best stop loss price based on support levels (0 if no position)

Rules:
- RSI < 30 = oversold (potential bounce). RSI > 70 = overbought (potential drop).
- SMA20 > SMA50 = bullish trend. SMA20 < SMA50 = bearish.
- Funding > 0.03% = market overheated, bias bearish.
- Fear&Greed < 25 = extreme fear (contrarian buy opportunity).
- High ATR% (>2%) = high volatility, recommend wider spacing.

JSON only, no other text."""

        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 300,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        })

        response = self.bedrock.invoke_model(
            modelId="us.anthropic.claude-3-5-haiku-20241022-v1:0",
            contentType="application/json",
            accept="application/json",
            body=body,
        )

        result = json.loads(response["body"].read())
        text = result["content"][0]["text"].strip()

        # Extract JSON
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()

        recommendation = json.loads(text)
        self.last_recommendation = recommendation

        self.last_report = (
            f"AI ({datetime.now(timezone.utc).strftime('%H:%M UTC')}): "
            f"{recommendation.get('outlook', '?').upper()} "
            f"({recommendation.get('confidence', '?')}/10) | "
            f"Risk: {recommendation.get('risk_level', '?')} | "
            f"{recommendation.get('reason', '?')}"
        )

        return recommendation

    # --- Apply recommendation (SIMPLIFIED — no trade execution) ---

    def _apply_recommendation(self, rec: dict, current_price: float):
        changes = []

        # 1. Grid spacing
        new_spacing = rec.get("grid_spacing_pct")
        if new_spacing and 0.3 <= new_spacing <= 2.0:
            if abs(new_spacing - self.config.grid_spacing_pct) >= 0.1:
                old = self.config.grid_spacing_pct
                self.config.grid_spacing_pct = new_spacing
                changes.append(f"spacing: {old}→{new_spacing}%")

        # 2. Dynamic stop loss (pass to risk manager, don't place on exchange directly)
        new_sl = rec.get("dynamic_stop_loss", 0)
        if new_sl > 0:
            # Never lower the stop (trailing up only)
            if self.dynamic_stop_loss > 0 and new_sl < self.dynamic_stop_loss:
                new_sl = self.dynamic_stop_loss
            self.dynamic_stop_loss = new_sl

            # Calculate targets from support/resistance
            sr = self.last_collected_data.get("support_resistance", {})
            resistances = sr.get("resistances", [])
            if resistances:
                self.dynamic_target_1 = resistances[0]["price"]
                if len(resistances) > 1:
                    self.dynamic_target_2 = resistances[1]["price"]

        # 3. Notify about analysis
        if changes:
            self.notifier.send(self.last_report + f"\nAjustes: {', '.join(changes)}")
        else:
            # Only notify every other analysis to reduce noise
            if self.last_analysis_time is None:
                self.notifier.send(self.last_report)

    def get_status(self) -> dict:
        return {
            "last_analysis": self.last_analysis_time.isoformat() if self.last_analysis_time else None,
            "last_report": self.last_report,
            "recommendation": self.last_recommendation,
            "next_analysis_in": self._time_to_next(),
            "dynamic_targets": {
                "stop_loss": self.dynamic_stop_loss,
                "target_1": self.dynamic_target_1,
                "target_2": self.dynamic_target_2,
            },
        }

    def _time_to_next(self) -> str:
        if self.last_analysis_time is None:
            return "imminent"
        next_time = self.last_analysis_time + timedelta(minutes=self.analysis_interval_minutes)
        remaining = next_time - datetime.now(timezone.utc)
        if remaining.total_seconds() <= 0:
            return "imminent"
        minutes = int(remaining.total_seconds() // 60)
        return f"{minutes}m"
