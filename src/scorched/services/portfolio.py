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

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch)


async def _get_current_prices(symbols: list[str]) -> dict[str, Decimal]:
    """Batch-fetch current prices for all symbols via yfinance."""
    if not symbols:
        return {}
    import yfinance as yf

    def _fetch():
        result = {}
        for symbol in symbols:
            try:
                ticker = yf.Ticker(symbol)
                try:
                    result[symbol] = Decimal(str(round(float(ticker.fast_info["last_price"]), 4)))
                except (KeyError, IndexError):
                    hist = ticker.history(period="1d")
                    if not hist.empty:
                        result[symbol] = Decimal(str(round(float(hist["Close"].iloc[-1]), 4)))
            except Exception:
                pass  # Will fall back to avg_cost_basis in caller
        return result

    loop = asyncio.get_running_loop()
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

    symbols = [pos.symbol for pos in positions_rows]
    prices = await _get_current_prices(symbols)

    for pos in positions_rows:
        current_price = prices.get(pos.symbol, pos.avg_cost_basis)
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
                trailing_stop_price=pos.trailing_stop_price,
                high_water_mark=pos.high_water_mark,
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
        initial_stop = (execution_price * Decimal("0.95")).quantize(Decimal("0.0001"))
        pos = Position(
            symbol=symbol,
            shares=shares,
            avg_cost_basis=execution_price,
            first_purchase_date=executed_at.date(),
            high_water_mark=execution_price,
            trailing_stop_price=initial_stop,
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
    ("RSP", "S&P 500 Equal Wt"),
    ("MTUM", "Momentum Factor"),
    ("SPMO", "S&P 500 Momentum"),
]
# Maps each benchmark symbol to the Portfolio column that stores its inception price.
_BENCHMARK_COLUMNS = [
    "spy_start_price", "qqq_start_price", "rsp_start_price",
    "mtum_start_price", "spmo_start_price",
]


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
    position_symbols = [pos.symbol for pos in positions_rows]
    prices = await _get_current_prices(position_symbols)
    total_positions_value = sum(
        (prices.get(pos.symbol, pos.avg_cost_basis) * pos.shares).quantize(Decimal("0.01"))
        for pos in positions_rows
    )

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

    # ── Trade performance metrics ────────────────────────────────────────
    sell_trades = (
        await db.execute(select(TradeHistory).where(TradeHistory.action == "sell"))
    ).scalars().all()

    trade_metrics = {}
    if sell_trades:
        gains = [float(t.realized_gain) for t in sell_trades if t.realized_gain is not None]
        wins = [g for g in gains if g > 0]
        losses = [g for g in gains if g < 0]

        total_closed = len(gains)
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = round(win_count / total_closed * 100, 1) if total_closed else 0

        avg_win = round(sum(wins) / win_count, 2) if wins else 0
        avg_loss = round(sum(losses) / loss_count, 2) if losses else 0
        profit_factor = round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else None

        # Expectancy = (win% * avg_win) + (loss% * avg_loss)
        win_pct = win_count / total_closed if total_closed else 0
        loss_pct = loss_count / total_closed if total_closed else 0
        expectancy = round(win_pct * avg_win + loss_pct * avg_loss, 2) if total_closed else 0

        # Average holding period (for sells that have a matching buy)
        holding_days = []
        buy_trades = (
            await db.execute(select(TradeHistory).where(TradeHistory.action == "buy"))
        ).scalars().all()
        buy_dates = {}
        for bt in buy_trades:
            buy_dates.setdefault(bt.symbol, []).append(bt.executed_at)
        for st in sell_trades:
            sym_buys = buy_dates.get(st.symbol, [])
            if sym_buys:
                # Use the earliest buy before this sell
                relevant = [b for b in sym_buys if b <= st.executed_at]
                if relevant:
                    held = (st.executed_at - relevant[0]).days
                    holding_days.append(held)

        avg_holding_days = round(sum(holding_days) / len(holding_days), 1) if holding_days else None

        # Max drawdown — approximate from trade-by-trade cumulative P&L
        sorted_sells = sorted(sell_trades, key=lambda t: t.executed_at)
        cum_pnl = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in sorted_sells:
            if t.realized_gain is not None:
                cum_pnl += float(t.realized_gain)
            if cum_pnl > peak:
                peak = cum_pnl
            dd = peak - cum_pnl
            if dd > max_dd:
                max_dd = dd
        max_drawdown = round(max_dd, 2)

        # Max consecutive losses
        max_consec_losses = 0
        current_streak = 0
        for g in [float(t.realized_gain) for t in sorted_sells if t.realized_gain is not None]:
            if g < 0:
                current_streak += 1
                max_consec_losses = max(max_consec_losses, current_streak)
            else:
                current_streak = 0

        trade_metrics = {
            "total_closed": total_closed,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": profit_factor,
            "expectancy": expectancy,
            "avg_holding_days": avg_holding_days,
            "max_drawdown": max_drawdown,
            "max_consecutive_losses": max_consec_losses,
        }

    return BenchmarkResponse(
        portfolio_return_pct=portfolio_return_pct,
        since_date=since_date,
        benchmarks=benchmarks,
        trade_metrics=trade_metrics,
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
