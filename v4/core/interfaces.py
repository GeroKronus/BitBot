"""Interfaces — All modules must implement these. No exceptions."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass
class MarketSnapshot:
    """Single point-in-time view of the market."""
    timestamp: datetime
    price: float
    bid: float = 0.0
    ask: float = 0.0
    spread: float = 0.0
    volume_24h: float = 0.0
    funding_rate: float = 0.0
    latency_ms: int = 0


@dataclass
class Features:
    """Computed features from raw market data."""
    # Trend
    sma_20: float = 0.0
    sma_50: float = 0.0
    sma_slope_20: float = 0.0     # % change per candle
    price_vs_sma20_pct: float = 0.0
    price_vs_sma50_pct: float = 0.0
    sma_aligned_bullish: bool = False
    sma_aligned_bearish: bool = False

    # Volatility
    atr: float = 0.0
    atr_pct: float = 0.0          # ATR as % of price
    atr_expanding: bool = False    # ATR now > ATR 3h ago
    bb_upper: float = 0.0
    bb_lower: float = 0.0
    bb_middle: float = 0.0
    bb_bandwidth_pct: float = 0.0
    bb_position_pct: float = 0.0  # 0=lower, 100=upper

    # Momentum
    rsi: float = 50.0
    momentum_1h: float = 0.0      # % change last hour
    momentum_4h: float = 0.0      # % change last 4 hours
    speed_5m: float = 0.0         # % change last 5 min (fast)

    # Liquidity
    spread_pct: float = 0.0
    volume_ratio: float = 1.0     # current vs 20-period avg

    # Acceleration (for predictive kill switch)
    vol_acceleration: float = 0.0  # rate of change of ATR
    price_acceleration: float = 0.0  # 2nd derivative of price


@dataclass
class RegimeState:
    """Full regime state with history and confidence."""
    current: str = "RANGE"
    previous: str = "RANGE"
    confidence: float = 0.5
    time_in_regime_seconds: float = 0.0
    regime_changes_1h: int = 0
    last_change_at: Optional[datetime] = None

    # Cooldowns
    strategy_disabled_until: Optional[datetime] = None
    consecutive_stops: int = 0


@dataclass
class Signal:
    """A trading signal from a strategy."""
    side: str              # "buy", "sell", "close"
    price: float
    amount: float
    order_type: str = "limit"   # "limit", "market"
    reduce_only: bool = False
    source: str = ""       # which strategy generated this
    confidence: float = 1.0
    metadata: dict = field(default_factory=dict)


@dataclass
class Position:
    """Single source of truth for current position."""
    side: str = "flat"     # "long", "short", "flat"
    size: float = 0.0      # BTC
    entry_price: float = 0.0  # VWAP
    unrealized_pnl: float = 0.0
    notional: float = 0.0  # size * current_price
    leverage: int = 1
    open_since: Optional[datetime] = None


@dataclass
class ExecutionResult:
    """Result of an order execution."""
    filled: bool = False
    fill_price: float = 0.0
    fill_amount: float = 0.0
    slippage_pct: float = 0.0
    latency_ms: int = 0
    partial: bool = False
    error: str = ""
    order_id: str = ""


@dataclass
class GovernorDecision:
    """AI Governor output — controls the entire system."""
    allow_trading: bool = True
    max_exposure_pct: float = 80.0
    mode: str = "normal"        # "normal", "conservative", "aggressive", "shutdown"
    reason: str = ""
    grid_enabled: bool = True
    trend_enabled: bool = True


# ============================================================
# INTERFACES
# ============================================================

class IMarketDataAgent(ABC):
    """Fetches raw market data from exchange."""

    @abstractmethod
    def fetch(self) -> MarketSnapshot:
        """Fetch current market snapshot."""
        ...

    @abstractmethod
    def get_candles(self, timeframe: str, limit: int) -> list[dict]:
        """Fetch OHLCV candles."""
        ...


class IFeatureEngine(ABC):
    """Computes features from raw market data."""

    @abstractmethod
    def compute(self, snapshot: MarketSnapshot, candles: list[dict]) -> Features:
        """Compute all features from market data."""
        ...


class IRegimeAgent(ABC):
    """Detects market regime."""

    @abstractmethod
    def detect(self, features: Features, state: RegimeState) -> RegimeState:
        """Update regime state based on features."""
        ...


class IStrategy(ABC):
    """Generates trading signals."""

    @abstractmethod
    def generate_signals(self, features: Features, regime: RegimeState,
                         position: Position, governor: GovernorDecision) -> list[Signal]:
        """Generate signals based on current context."""
        ...

    @abstractmethod
    def name(self) -> str:
        """Strategy identifier."""
        ...


class IRiskEngine(ABC):
    """Evaluates and filters signals."""

    @abstractmethod
    def evaluate(self, signals: list[Signal], position: Position,
                 features: Features, regime: RegimeState,
                 governor: GovernorDecision) -> list[Signal]:
        """Filter/modify signals based on risk rules. May block or resize."""
        ...


class IExecutionAgent(ABC):
    """Executes orders on the exchange."""

    @abstractmethod
    def execute(self, signals: list[Signal]) -> list[ExecutionResult]:
        """Execute a list of signals. Returns results."""
        ...

    @abstractmethod
    def cancel_all(self) -> int:
        """Cancel all open orders. Returns count cancelled."""
        ...

    @abstractmethod
    def close_position(self, position: Position, price: float) -> ExecutionResult:
        """Close entire position."""
        ...


class IPositionCore(ABC):
    """Single source of truth for position state."""

    @abstractmethod
    def sync(self) -> Position:
        """Sync with exchange and return current position."""
        ...

    @abstractmethod
    def get(self) -> Position:
        """Get cached position (no API call)."""
        ...


class IAIGovernor(ABC):
    """AI-powered system governor."""

    @abstractmethod
    def decide(self, features: Features, regime: RegimeState,
               position: Position) -> GovernorDecision:
        """Make governance decision."""
        ...
