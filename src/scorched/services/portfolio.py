"""Portfolio state management: reads, buys, sells."""
import asyncio
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Portfolio, Position, TradeHistory, TradeRecommendation
from ..schemas import (
    BenchmarkItem,
    BenchmarkResponse,
    ConfirmTradeResponse,
    PortfolioResponse,
    PortfolioSummary,
    PositionDetail,
    PositionWithPnL,
    TaxSummaryCategory,
    TaxSummaryResponse,
)
from ..tax import classify_gain, estimate_tax, post_tax_gain


async def _get_current_price(symbol: str) -> Decimal:
    """Fetch latest price via yfinance in a thread (yfinance is sync)."""
    import yfinance as yf

    def _fetch():
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="1d")
        if hist.empty:
            return Decimal("0")
        return Decimal(str(hist["Close"].iloc[-1]))

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch)


async def get_portfolio_state(db: AsyncSession) -> PortfolioResponse:
    portfolio = (await db.execute(select(Portfolio))).scalars().first()
    if portfolio is None:
        raise ValueError("Portfolio not initialized")

    positions_rows = (await db.execute(select(Position))).scalars().all()
    today = date.today()
    positions_out = []
    total_positions_value = Decimal("0")
    total_unrealized_gain = Decimal("0")

    for pos in positions_rows:
        current_price = await _get_current_price(pos.symbol)
        market_value = (current_price * pos.shares).quantize(Decimal("0.01"))
        cost_basis_total = (pos.avg_cost_basis * pos.shares).quantize(Decimal("0.01"))
        unrealized_gain = market_value - cost_basis_total
        unrealized_gain_pct = (
            (unrealized_gain / cost_basis_total * 100).quantize(Decimal("0.01"))
            if cost_basis_total != 0
            else Decimal("0")
        )
        days_held = (today - pos.first_purchase_date).days
        tax_cat = classify_gain(pos.first_purchase_date, today)
        est_tax = estimate_tax(unrealized_gain, tax_cat)
        est_post_tax = post_tax_gain(unrealized_gain, tax_cat)

        positions_out.append(
            PositionWithPnL(
                symbol=pos.symbol,
                shares=pos.shares,
                avg_cost_basis=pos.avg_cost_basis,
                current_price=current_price,
                market_value=market_value,
                unrealized_gain=unrealized_gain,
                unrealized_gain_pct=unrealized_gain_pct,
                first_purchase_date=pos.first_purchase_date,
                days_held=days_held,
                tax_category=tax_cat,
                estimated_tax_on_gain=est_tax,
                estimated_post_tax_gain=est_post_tax,
            )
        )
        total_positions_value += market_value
        total_unrealized_gain += unrealized_gain

    total_value = portfolio.cash_balance + total_positions_value
    all_time_return_pct = (
        ((total_value - portfolio.starting_capital) / portfolio.starting_capital * 100).quantize(
            Decimal("0.01")
        )
        if portfolio.starting_capital != 0
        else Decimal("0")
    )

    return PortfolioResponse(
        cash_balance=portfolio.cash_balance,
        starting_capital=portfolio.starting_capital,
        positions=positions_out,
        total_positions_value=total_positions_value,
        total_unrealized_gain=total_unrealized_gain,
        total_value=total_value,
        all_time_return_pct=all_time_return_pct,
    )


async def get_portfolio_summary(db: AsyncSession) -> PortfolioSummary:
    """Lightweight summary without fetching live prices for every position."""
    portfolio = (await db.execute(select(Portfolio))).scalars().first()
    if portfolio is None:
        raise ValueError("Portfolio not initialized")
    positions = (await db.execute(select(Position))).scalars().all()
    # Approximate: use cost basis as position value (faster than live prices)
    total_positions_value = sum(p.avg_cost_basis * p.shares for p in positions)
    return PortfolioSummary(
        cash_balance=portfolio.cash_balance,
        total_positions_value=total_positions_value,
        total_value=portfolio.cash_balance + total_positions_value,
    )


async def apply_buy(
    db: AsyncSession,
    recommendation_id: int | None,
    symbol: str,
    shares: Decimal,
    execution_price: Decimal,
    executed_at: datetime,
) -> ConfirmTradeResponse:
    portfolio = (await db.execute(select(Portfolio))).scalars().first()
    total_cost = (shares * execution_price).quantize(Decimal("0.01"))

    if portfolio.cash_balance < total_cost:
        raise ValueError(
            f"Insufficient cash: have {portfolio.cash_balance}, need {total_cost}"
        )

    # Upsert position
    pos = (
        await db.execute(select(Position).where(Position.symbol == symbol))
    ).scalars().first()

    if pos is None:
        pos = Position(
            symbol=symbol,
            shares=shares,
            avg_cost_basis=execution_price,
            first_purchase_date=executed_at.date(),
        )
        db.add(pos)
    else:
        # Recalculate weighted avg cost basis
        total_existing_value = pos.shares * pos.avg_cost_basis
        new_total_value = total_existing_value + (shares * execution_price)
        new_total_shares = pos.shares + shares
        pos.avg_cost_basis = (new_total_value / new_total_shares).quantize(Decimal("0.0001"))
        pos.shares = new_total_shares

    # Deduct cash
    portfolio.cash_balance = (portfolio.cash_balance - total_cost).quantize(Decimal("0.01"))

    # Mark recommendation confirmed
    if recommendation_id is not None:
        rec = (
            await db.execute(
                select(TradeRecommendation).where(TradeRecommendation.id == recommendation_id)
            )
        ).scalars().first()
        if rec:
            rec.status = "confirmed"

    # Append to history
    history = TradeHistory(
        recommendation_id=recommendation_id,
        symbol=symbol,
        action="buy",
        shares=shares,
        execution_price=execution_price,
        total_value=total_cost,
        executed_at=executed_at,
    )
    db.add(history)
    await db.commit()
    await db.refresh(history)
    await db.refresh(pos)

    return ConfirmTradeResponse(
        trade_id=history.id,
        symbol=symbol,
        action="buy",
        shares=shares,
        execution_price=execution_price,
        total_value=total_cost,
        new_cash_balance=portfolio.cash_balance,
        position=PositionDetail(
            symbol=pos.symbol,
            shares=pos.shares,
            avg_cost_basis=pos.avg_cost_basis,
            first_purchase_date=pos.first_purchase_date,
        ),
    )


async def apply_sell(
    db: AsyncSession,
    recommendation_id: int | None,
    symbol: str,
    shares: Decimal,
    execution_price: Decimal,
    executed_at: datetime,
) -> ConfirmTradeResponse:
    portfolio = (await db.execute(select(Portfolio))).scalars().first()
    pos = (
        await db.execute(select(Position).where(Position.symbol == symbol))
    ).scalars().first()

    if pos is None:
        raise ValueError(f"No position found for {symbol}")
    if pos.shares < shares:
        raise ValueError(
            f"Cannot sell {shares} shares of {symbol}; only hold {pos.shares}"
        )

    total_proceeds = (shares * execution_price).quantize(Decimal("0.01"))
    realized_gain = ((execution_price - pos.avg_cost_basis) * shares).quantize(Decimal("0.01"))
    tax_cat = classify_gain(pos.first_purchase_date, executed_at.date())
    est_tax = estimate_tax(realized_gain, tax_cat)
    est_post_tax = post_tax_gain(realized_gain, tax_cat)

    # Update or remove position
    pos.shares -= shares
    remaining_position: PositionDetail | None = None
    if pos.shares <= Decimal("0"):
        await db.delete(pos)
    else:
        remaining_position = PositionDetail(
            symbol=pos.symbol,
            shares=pos.shares,
            avg_cost_basis=pos.avg_cost_basis,
            first_purchase_date=pos.first_purchase_date,
        )

    # Add proceeds to cash
    portfolio.cash_balance = (portfolio.cash_balance + total_proceeds).quantize(Decimal("0.01"))

    # Mark recommendation confirmed
    if recommendation_id is not None:
        rec = (
            await db.execute(
                select(TradeRecommendation).where(TradeRecommendation.id == recommendation_id)
            )
        ).scalars().first()
        if rec:
            rec.status = "confirmed"

    history = TradeHistory(
        recommendation_id=recommendation_id,
        symbol=symbol,
        action="sell",
        shares=shares,
        execution_price=execution_price,
        total_value=total_proceeds,
        executed_at=executed_at,
        realized_gain=realized_gain,
        tax_category=tax_cat,
    )
    db.add(history)
    await db.commit()
    await db.refresh(history)

    return ConfirmTradeResponse(
        trade_id=history.id,
        symbol=symbol,
        action="sell",
        shares=shares,
        execution_price=execution_price,
        total_value=total_proceeds,
        new_cash_balance=portfolio.cash_balance,
        position=remaining_position,
        realized_gain=realized_gain,
        tax_category=tax_cat,
        estimated_tax=est_tax,
        estimated_post_tax_gain=est_post_tax,
    )


_BENCHMARKS = [
    ("SPY", "S&P 500"),
    ("QQQ", "Nasdaq 100"),
    ("^DJI", "Dow Jones"),
]
# Maps each benchmark symbol to the Portfolio column that stores its inception price.
_BENCHMARK_COLUMNS = ["spy_start_price", "qqq_start_price", "dji_start_price"]


async def get_benchmark_comparison(db: AsyncSession) -> BenchmarkResponse:
    """Compare portfolio return since inception against major indexes.

    Benchmark starting prices are captured once (lazily, on the first call) and stored
    on the portfolio row so the zero point never drifts across yfinance calls.
    """
    portfolio = (await db.execute(select(Portfolio))).scalars().first()
    if portfolio is None:
        raise ValueError("Portfolio not initialized")

    # Current portfolio return
    positions_rows = (await db.execute(select(Position))).scalars().all()
    total_positions_value = Decimal("0")
    for pos in positions_rows:
        price = await _get_current_price(pos.symbol)
        total_positions_value += (price * pos.shares).quantize(Decimal("0.01"))

    total_value = portfolio.cash_balance + total_positions_value
    portfolio_return_pct = float(
        ((total_value - portfolio.starting_capital) / portfolio.starting_capital * 100).quantize(
            Decimal("0.01")
        )
        if portfolio.starting_capital != 0
        else Decimal("0")
    )

    since_date = portfolio.created_at.date()

    # If any start price is missing, capture all three now and lock them in.
    if any(getattr(portfolio, col) is None for col in _BENCHMARK_COLUMNS):
        start_prices = await asyncio.gather(
            *[_get_current_price(sym) for sym, _ in _BENCHMARKS]
        )
        for col, price in zip(_BENCHMARK_COLUMNS, start_prices):
            setattr(portfolio, col, price)
        await db.commit()
        await db.refresh(portfolio)

    # Fetch current (live) prices and compute return vs. stored baselines.
    current_prices = await asyncio.gather(
        *[_get_current_price(sym) for sym, _ in _BENCHMARKS]
    )

    benchmarks = []
    for (symbol, name), col, current_price in zip(_BENCHMARKS, _BENCHMARK_COLUMNS, current_prices):
        start_price = getattr(portfolio, col)
        if not start_price or start_price == 0:
            continue
        ret = round(float((current_price - start_price) / start_price * 100), 2)
        benchmarks.append(BenchmarkItem(
            symbol=symbol,
            name=name,
            return_pct=ret,
            beats_portfolio=ret > portfolio_return_pct,
        ))

    return BenchmarkResponse(
        portfolio_return_pct=portfolio_return_pct,
        since_date=since_date,
        benchmarks=benchmarks,
    )


async def get_tax_summary(db: AsyncSession) -> TaxSummaryResponse:
    history = (await db.execute(select(TradeHistory).where(TradeHistory.action == "sell"))).scalars().all()

    st_gain = sum((h.realized_gain or Decimal(0)) for h in history if h.tax_category == "short_term")
    lt_gain = sum((h.realized_gain or Decimal(0)) for h in history if h.tax_category == "long_term")

    st_tax = estimate_tax(st_gain, "short_term")
    lt_tax = estimate_tax(lt_gain, "long_term")

    return TaxSummaryResponse(
        short_term=TaxSummaryCategory(
            realized_gain=st_gain,
            estimated_tax=st_tax,
            estimated_post_tax_gain=st_gain - st_tax,
        ),
        long_term=TaxSummaryCategory(
            realized_gain=lt_gain,
            estimated_tax=lt_tax,
            estimated_post_tax_gain=lt_gain - lt_tax,
        ),
        total_realized_gain=st_gain + lt_gain,
        total_estimated_tax=st_tax + lt_tax,
        total_post_tax_gain=(st_gain + lt_gain) - (st_tax + lt_tax),
    )
