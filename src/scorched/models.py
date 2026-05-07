from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, CheckConstraint, Date, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func


# Use JSONB on Postgres for indexability/performance, fall back to JSON (TEXT)
# on SQLite so the in-memory test database can mirror the schema.
JSON_OR_JSONB = JSON().with_variant(JSONB(), "postgresql")

from .database import Base


class Portfolio(Base):
    __tablename__ = "portfolio"

    id: Mapped[int] = mapped_column(primary_key=True)
    cash_balance: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    starting_capital: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    # Benchmark starting prices — captured once on first benchmark request and never changed.
    # Used to compute % return since simulation inception vs. each index.
    spy_start_price: Mapped[Decimal | None] = mapped_column(Numeric(15, 4), nullable=True)
    qqq_start_price: Mapped[Decimal | None] = mapped_column(Numeric(15, 4), nullable=True)
    dji_start_price: Mapped[Decimal | None] = mapped_column(Numeric(15, 4), nullable=True)
    rsp_start_price: Mapped[Decimal | None] = mapped_column(Numeric(15, 4), nullable=True)
    mtum_start_price: Mapped[Decimal | None] = mapped_column(Numeric(15, 4), nullable=True)
    spmo_start_price: Mapped[Decimal | None] = mapped_column(Numeric(15, 4), nullable=True)
    peak_portfolio_value: Mapped[Decimal | None] = mapped_column(Numeric(15, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(10), nullable=False, unique=True)
    shares: Mapped[Decimal] = mapped_column(Numeric(15, 6), nullable=False)
    avg_cost_basis: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    first_purchase_date: Mapped[date] = mapped_column(Date, nullable=False)
    trailing_stop_price: Mapped[Decimal | None] = mapped_column(Numeric(15, 4), nullable=True)
    high_water_mark: Mapped[Decimal | None] = mapped_column(Numeric(15, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class RecommendationSession(Base):
    __tablename__ = "recommendation_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    raw_research: Mapped[str | None] = mapped_column(Text)
    claude_response: Mapped[str | None] = mapped_column(Text)
    analysis_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    recommendations: Mapped[list["TradeRecommendation"]] = relationship(
        back_populates="session", lazy="selectin", cascade="all, delete-orphan"
    )


class TradeRecommendation(Base):
    __tablename__ = "trade_recommendations"
    __table_args__ = (
        CheckConstraint("action IN ('buy', 'sell')", name="ck_rec_action"),
        CheckConstraint("status IN ('pending', 'submitted', 'confirmed', 'rejected')", name="ck_rec_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("recommendation_sessions.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    action: Mapped[str] = mapped_column(String(4), nullable=False)
    suggested_price: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(15, 6), nullable=False)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[str] = mapped_column(String(10), nullable=False, default="medium")
    key_risks: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    session: Mapped["RecommendationSession"] = relationship(back_populates="recommendations")


class Playbook(Base):
    """Living strategy document — single row, updated by Claude each morning."""
    __tablename__ = "playbook"

    id: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    version: Mapped[int] = mapped_column(nullable=False, default=1)


class TradeHistory(Base):
    __tablename__ = "trade_history"
    __table_args__ = (
        CheckConstraint("action IN ('buy', 'sell')", name="ck_hist_action"),
        CheckConstraint(
            "tax_category IN ('short_term', 'long_term') OR tax_category IS NULL",
            name="ck_hist_tax_category",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    recommendation_id: Mapped[int | None] = mapped_column(
        ForeignKey("trade_recommendations.id"), nullable=True, unique=True
    )
    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    action: Mapped[str] = mapped_column(String(4), nullable=False)
    shares: Mapped[Decimal] = mapped_column(Numeric(15, 6), nullable=False)
    execution_price: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    total_value: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(server_default=func.now())
    realized_gain: Mapped[Decimal | None] = mapped_column(Numeric(15, 4))
    tax_category: Mapped[str | None] = mapped_column(String(10))


class TokenUsage(Base):
    __tablename__ = "token_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("recommendation_sessions.id"), nullable=True
    )
    call_type: Mapped[str] = mapped_column(String(20), nullable=False)
    model: Mapped[str] = mapped_column(String(50), nullable=False)
    input_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    thinking_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PendingFill(Base):
    """Tracks submitted Alpaca orders awaiting fill confirmation.

    Written BEFORE submitting to Alpaca (with client_order_id, no order_id yet).
    Updated with real order_id after Alpaca accepts.  Removed once the fill is
    recorded in TradeHistory via apply_buy/apply_sell.
    """
    __tablename__ = "pending_fills"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[str | None] = mapped_column(String(50), nullable=True, unique=True)
    client_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    action: Mapped[str] = mapped_column(String(4), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(15, 6), nullable=False)
    limit_price: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    recommendation_id: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class GateDecision(Base):
    """Persisted record of every gate evaluation in the trade pipeline.

    Captures Phase 1 candidate filters, Phase 1.5 circuit-breaker outcomes, and
    Phase 2 confirm-time gates. Each row is one (gate, symbol, decision) so we
    can attribute "which gate blocked this buy" without parsing logs.

    `recommendation_id` is nullable: pre-rec phase 1 rejections happen before a
    `TradeRecommendation` row exists. `session_id` may also be NULL when a
    decision is recorded outside a Phase 1 run (e.g. cron-driven Phase 1.5).
    """
    __tablename__ = "gate_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("recommendation_sessions.id", ondelete="CASCADE"), nullable=True
    )
    recommendation_id: Mapped[int | None] = mapped_column(
        ForeignKey("trade_recommendations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    action: Mapped[str] = mapped_column(String(4), nullable=False)
    phase: Mapped[str] = mapped_column(String(20), nullable=False)
    gate: Mapped[str] = mapped_column(String(40), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON_OR_JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False, index=True
    )


class ApiCallLog(Base):
    __tablename__ = "api_call_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    service: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    endpoint: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(15), nullable=False)
    response_time_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
