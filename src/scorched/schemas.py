from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field


# ── Recommendations ────────────────────────────────────────────────────────────

class GenerateRecommendationsRequest(BaseModel):
    session_date: date | None = None
    force: bool = False


class RecommendationItem(BaseModel):
    id: int
    symbol: str
    action: str  # 'buy' | 'sell'
    suggested_price: Decimal
    quantity: Decimal
    estimated_cost: Decimal
    reasoning: str
    confidence: str
    key_risks: str | None = None


class PortfolioSummary(BaseModel):
    cash_balance: Decimal
    total_positions_value: Decimal
    total_value: Decimal


class RecommendationsResponse(BaseModel):
    session_id: int
    date: date
    portfolio_summary: PortfolioSummary
    recommendations: list[RecommendationItem]
    research_summary: str
    market_closed: bool = False  # True on NYSE holidays — no trades generated


# ── Trades ─────────────────────────────────────────────────────────────────────

class ConfirmTradeRequest(BaseModel):
    recommendation_id: int
    # Advisory only — server uses stored recommendation values (quantity, price).
    # Accepted for backwards compatibility with old cron payloads but ignored.
    execution_price: Decimal | None = None
    shares: Decimal | None = None


class PositionDetail(BaseModel):
    symbol: str
    shares: Decimal
    avg_cost_basis: Decimal
    first_purchase_date: date


class ConfirmTradeResponse(BaseModel):
    trade_id: int
    symbol: str
    action: str
    shares: Decimal
    execution_price: Decimal
    total_value: Decimal
    new_cash_balance: Decimal
    position: PositionDetail | None  # None if position was fully sold
    realized_gain: Decimal | None = None
    tax_category: str | None = None
    estimated_tax: Decimal | None = None
    estimated_post_tax_gain: Decimal | None = None


class RejectTradeRequest(BaseModel):
    reason: str | None = None


class RejectTradeResponse(BaseModel):
    recommendation_id: int
    status: str


# ── Portfolio ──────────────────────────────────────────────────────────────────

class PositionWithPnL(BaseModel):
    symbol: str
    shares: Decimal
    avg_cost_basis: Decimal
    current_price: Decimal
    market_value: Decimal
    unrealized_gain: Decimal
    unrealized_gain_pct: Decimal
    first_purchase_date: date
    days_held: int
    tax_category: str  # 'short_term' | 'long_term'
    estimated_tax_on_gain: Decimal
    estimated_post_tax_gain: Decimal
    trailing_stop_price: Decimal | None = None
    high_water_mark: Decimal | None = None


class PortfolioResponse(BaseModel):
    cash_balance: Decimal
    starting_capital: Decimal
    positions: list[PositionWithPnL]
    total_positions_value: Decimal
    total_unrealized_gain: Decimal
    total_value: Decimal
    all_time_return_pct: Decimal


class TradeHistoryItem(BaseModel):
    id: int
    recommendation_id: int | None
    symbol: str
    action: str
    shares: Decimal
    execution_price: Decimal
    total_value: Decimal
    executed_at: datetime
    realized_gain: Decimal | None
    tax_category: str | None


class TaxSummaryCategory(BaseModel):
    realized_gain: Decimal
    estimated_tax: Decimal
    estimated_post_tax_gain: Decimal


class TaxSummaryResponse(BaseModel):
    short_term: TaxSummaryCategory
    long_term: TaxSummaryCategory
    total_realized_gain: Decimal
    total_estimated_tax: Decimal
    total_post_tax_gain: Decimal


# ── Portfolio Risk (VaR / CVaR) ────────────────────────────────────────────────

class PortfolioRiskResponse(BaseModel):
    confidence: float          # e.g. 0.95
    lookback_days: int         # number of overlapping daily returns used
    n_positions: int           # positions actually included
    portfolio_value: float     # equity + cash
    var_pct: float             # negative number — fraction of equity at risk
    cvar_pct: float            # negative number — average tail loss fraction
    var_dollars: float         # |var_pct| × equity (dollars at risk on a 1-day VaR event)
    cvar_dollars: float        # average dollar loss conditional on VaR breach


# ── Benchmarks ─────────────────────────────────────────────────────────────────

class BenchmarkItem(BaseModel):
    symbol: str           # e.g. "SPY"
    name: str             # e.g. "S&P 500"
    return_pct: float     # index return since portfolio inception
    beats_portfolio: bool # True if index outperformed portfolio (bot is lagging)


class BenchmarkResponse(BaseModel):
    portfolio_return_pct: float
    since_date: date      # Portfolio inception date (created_at)
    benchmarks: list[BenchmarkItem]
    trade_metrics: dict = {}  # win_rate, profit_factor, avg_win, etc.


# ── Sessions ───────────────────────────────────────────────────────────────────

class SessionListItem(BaseModel):
    id: int
    session_date: date
    recommendation_count: int
    created_at: datetime


class SessionDetail(BaseModel):
    id: int
    session_date: date
    research_summary: str | None
    recommendations: list[RecommendationItem]
    created_at: datetime


# ── Intraday Monitor ──────────────────────────────────────────────────────────

class IntradayTriggerItem(BaseModel):
    symbol: str
    trigger_reasons: list[str]
    current_price: Decimal
    entry_price: Decimal
    today_open: Decimal
    today_high: Decimal
    today_low: Decimal
    days_held: int
    shares: Decimal
    original_reasoning: str = ""
    atr: float | None = None


class IntradayMarketContext(BaseModel):
    spy_change_pct: float = 0.0
    vix_current: float = 0.0


class IntradayEvaluateRequest(BaseModel):
    triggers: list[IntradayTriggerItem]
    market_context: IntradayMarketContext = IntradayMarketContext()


class IntradayDecision(BaseModel):
    symbol: str
    action: str  # 'exit_full' | 'exit_partial' | 'hold'
    reasoning: str
    trade_result: dict | None = None


class IntradayEvaluateResponse(BaseModel):
    decisions: list[IntradayDecision]
