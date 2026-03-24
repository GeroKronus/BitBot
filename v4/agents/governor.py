"""AI Governor — Controls the entire system via simple decisions.

Output: allow_trading, max_exposure, mode, grid_enabled, trend_enabled
NOT an executor — a governor that permits or restricts.

Uses Claude Haiku 3.5 via AWS Bedrock.
Runs every 30 minutes.
Prompt: ~300 tokens, 5 output fields.
"""

import json
import os
from datetime import datetime, timezone, timedelta

import boto3

from ..core.interfaces import IAIGovernor, Features, RegimeState, Position, GovernorDecision


class AIGovernor(IAIGovernor):

    def __init__(self, interval_minutes: int = 30):
        self.interval_minutes = interval_minutes
        self._last_decision_time = None
        self._last_decision = GovernorDecision()
        self._news_cache = []
        self._fear_greed = 50

        try:
            self.bedrock = boto3.client(
                "bedrock-runtime",
                region_name=os.environ.get("AWS_REGION", "us-east-1"),
                aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", ""),
                aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
            )
        except Exception:
            self.bedrock = None

    def decide(self, features: Features, regime: RegimeState,
               position: Position) -> GovernorDecision:
        """Make governance decision. Uses cached decision if not time yet."""

        # Return cached if not time for new analysis
        if not self._should_analyze():
            return self._last_decision

        # Fetch external data
        self._fetch_external_data()

        # If no Bedrock, use deterministic fallback
        if self.bedrock is None:
            return self._deterministic_decision(features, regime, position)

        try:
            decision = self._ask_claude(features, regime, position)
            self._last_decision = decision
            self._last_decision_time = datetime.now(timezone.utc)
            return decision
        except Exception:
            return self._deterministic_decision(features, regime, position)

    def _should_analyze(self) -> bool:
        if self._last_decision_time is None:
            return True
        elapsed = datetime.now(timezone.utc) - self._last_decision_time
        return elapsed >= timedelta(minutes=self.interval_minutes)

    def _fetch_external_data(self):
        """Fetch Fear & Greed and news."""
        import urllib.request
        # Fear & Greed
        try:
            url = "https://api.alternative.me/fng/?limit=1"
            req = urllib.request.Request(url, headers={"User-Agent": "BitBot/4.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            self._fear_greed = int(data["data"][0]["value"])
        except Exception:
            pass

        # News headlines
        try:
            url = "https://cryptopanic.com/api/free/v1/posts/?auth_token=free&currencies=BTC&kind=news&num_results=5"
            req = urllib.request.Request(url, headers={"User-Agent": "BitBot/4.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            self._news_cache = []
            for post in data.get("results", [])[:5]:
                votes = post.get("votes", {})
                pos = votes.get("positive", 0)
                neg = votes.get("negative", 0)
                sent = "+" if pos > neg else ("-" if neg > pos else "~")
                self._news_cache.append(f"[{sent}] {post.get('title', '')}")
        except Exception:
            pass

    def _ask_claude(self, features: Features, regime: RegimeState,
                    position: Position) -> GovernorDecision:
        """Ask Claude for governance decision. Minimal prompt."""

        news_str = " | ".join(self._news_cache[:3]) if self._news_cache else "No news"
        pos_str = f"{position.side} {position.size:.5f} BTC" if position.side != "flat" else "FLAT"

        prompt = f"""BTC trading bot governor. JSON only. Portuguese reason.

Price: ${features.sma_20:,.0f} | RSI: {features.rsi:.0f} | ATR: {features.atr_pct:.1f}%
Regime: {regime.current} (conf: {regime.confidence:.0%}) | Position: {pos_str}
Fear&Greed: {self._fear_greed} | Funding: {features.spread_pct:.3f}%
News: {news_str}

Decide:
1. "allow_trading": true/false
2. "max_exposure_pct": 30-80 (lower in high risk)
3. "mode": "normal"/"conservative"/"shutdown"
4. "reason": 1 sentence in Portuguese

Rules:
- Fear&Greed < 20: conservative mode
- ATR > 2.5%: reduce max_exposure to 40
- Negative news dominant: conservative
- CHAOS regime: shutdown"""

        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 200,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        })

        response = self.bedrock.invoke_model(
            modelId="us.anthropic.claude-3-5-haiku-20241022-v1:0",
            contentType="application/json",
            accept="application/json",
            body=body,
        )

        result = json.loads(response["body"].read())
        text = result["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()

        rec = json.loads(text)

        return GovernorDecision(
            allow_trading=rec.get("allow_trading", True),
            max_exposure_pct=min(80, max(10, rec.get("max_exposure_pct", 80))),
            mode=rec.get("mode", "normal"),
            reason=rec.get("reason", ""),
            grid_enabled=rec.get("mode") != "shutdown",
            trend_enabled=rec.get("mode") not in ("shutdown", "conservative"),
        )

    def _deterministic_decision(self, features: Features, regime: RegimeState,
                                position: Position) -> GovernorDecision:
        """Fallback when Bedrock unavailable. Pure rules."""

        if regime.current == "CHAOS":
            return GovernorDecision(
                allow_trading=False, max_exposure_pct=30, mode="shutdown",
                reason="Regime CHAOS detectado", grid_enabled=False, trend_enabled=False,
            )

        if features.atr_pct > 2.5:
            return GovernorDecision(
                allow_trading=True, max_exposure_pct=40, mode="conservative",
                reason="Alta volatilidade", grid_enabled=True, trend_enabled=False,
            )

        if self._fear_greed < 20:
            return GovernorDecision(
                allow_trading=True, max_exposure_pct=50, mode="conservative",
                reason="Medo extremo no mercado", grid_enabled=True, trend_enabled=False,
            )

        return GovernorDecision(
            allow_trading=True, max_exposure_pct=80, mode="normal",
            reason="Condicoes normais", grid_enabled=True, trend_enabled=True,
        )

    def get_status(self) -> dict:
        return {
            "last_decision": self._last_decision_time.isoformat() if self._last_decision_time else None,
            "decision": {
                "allow_trading": self._last_decision.allow_trading,
                "max_exposure_pct": self._last_decision.max_exposure_pct,
                "mode": self._last_decision.mode,
                "reason": self._last_decision.reason,
            },
            "fear_greed": self._fear_greed,
            "news_count": len(self._news_cache),
        }
