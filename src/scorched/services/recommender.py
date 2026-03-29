"""Core recommendation engine: two-call architecture with extended thinking."""
import asyncio
import json
import logging
from datetime import date, datetime
from decimal import Decimal

from ..api_tracker import ApiCallTracker
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..cost import record_usage
from ..models import Portfolio, Position, RecommendationSession, TokenUsage, TradeHistory, TradeRecommendation
from ..schemas import PortfolioSummary, RecommendationItem, RecommendationsResponse
from .claude_client import MODEL, call_analysis, call_decision, call_risk_review, parse_json_response
from .playbook import get_playbook, update_playbook
from .risk_review import build_risk_review_prompt, parse_risk_review_response
from .portfolio import get_portfolio_summary
from .strategy import load_analyst_guidance, load_strategy
from .technicals import compute_technicals
from .finnhub_data import fetch_analyst_consensus_sync, build_analyst_context
from .research import (
    WATCHLIST,
    build_options_context,
    build_research_context,
    fetch_av_technicals,
    fetch_earnings_surprise,
    fetch_edgar_insider,
    fetch_fred_macro,
    fetch_market_context,
    fetch_momentum_screener,
    fetch_news,
    fetch_options_data,
    fetch_polygon_news,
    fetch_price_data,
)

logger = logging.getLogger(__name__)


async def _get_recent_sell(
    db: AsyncSession, symbol: str, as_of: date, days: int = 30
) -> TradeHistory | None:
    """Return the most recent sell of *symbol* within *days* of *as_of*, or None."""
    from datetime import timedelta
    cutoff = datetime.combine(as_of - timedelta(days=days), datetime.min.time())
    result = await db.execute(
        select(TradeHistory)
        .where(
            TradeHistory.symbol == symbol,
            TradeHistory.action == "sell",
            TradeHistory.executed_at >= cutoff,
        )
        .order_by(TradeHistory.executed_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def _get_existing_session(db: AsyncSession, session_date: date) -> RecommendationSession | None:
    return (
        await db.execute(
            select(RecommendationSession).where(RecommendationSession.session_date == session_date)
        )
    ).scalars().first()


async def list_sessions(
    db: AsyncSession,
    session_date: date | None = None,
    limit: int = 10,
) -> list[RecommendationSession]:
    """Return recommendation sessions, optionally filtered by date."""
    q = (
        select(RecommendationSession)
        .order_by(RecommendationSession.session_date.desc())
        .limit(limit)
    )
    if session_date:
        q = q.where(RecommendationSession.session_date == session_date)
    return list((await db.execute(q)).scalars().all())


async def get_session(db: AsyncSession, session_id: int) -> RecommendationSession | None:
    """Return a single session by ID, or None."""
    return (
        await db.execute(
            select(RecommendationSession).where(RecommendationSession.id == session_id)
        )
    ).scalars().first()


async def _build_cached_response(
    session: RecommendationSession,
    portfolio_summary: PortfolioSummary,
) -> RecommendationsResponse:
    research_summary = ""
    if session.claude_response:
        try:
            research_summary = json.loads(session.claude_response).get("research_summary", "")
        except Exception:
            pass
    recs = [
        RecommendationItem(
            id=r.id,
            symbol=r.symbol,
            action=r.action,
            suggested_price=r.suggested_price,
            quantity=r.quantity,
            estimated_cost=(r.suggested_price * r.quantity).quantize(Decimal("0.01")),
            reasoning=r.reasoning,
            confidence=r.confidence,
            key_risks=r.key_risks,
        )
        for r in session.recommendations
    ]
    return RecommendationsResponse(
        session_id=session.id,
        date=session.session_date,
        portfolio_summary=portfolio_summary,
        recommendations=recs,
        research_summary=research_summary,
    )


def _is_market_open(session_date: date) -> bool:
    """Return True if the NYSE is open on session_date (excludes weekends and holidays)."""
    import pandas_market_calendars as mcal
    nyse = mcal.get_calendar("NYSE")
    schedule = nyse.valid_days(start_date=session_date, end_date=session_date)
    return len(schedule) > 0


async def generate_recommendations(
    db: AsyncSession,
    session_date: date | None = None,
    force: bool = False,
) -> RecommendationsResponse:
    if session_date is None:
        session_date = date.today()

    if not _is_market_open(session_date):
        logger.info("Market closed on %s — skipping recommendation generation", session_date)
        portfolio_summary = await get_portfolio_summary(db)
        return RecommendationsResponse(
            session_id=0,
            date=session_date,
            portfolio_summary=portfolio_summary,
            recommendations=[],
            research_summary="Market closed today.",
            market_closed=True,
        )

    portfolio_summary = await get_portfolio_summary(db)

    existing = await _get_existing_session(db, session_date)
    if existing is not None and not force:
        logger.info("Returning cached recommendations for %s", session_date)
        return await _build_cached_response(existing, portfolio_summary)
    if existing is not None and force:
        logger.info("force=True — deleting cached session %s and regenerating", session_date)
        # token_usage.session_id is nullable — detach rows before deleting session
        # so we don't violate the FK constraint (cost history is preserved, just unlinked)
        await db.execute(
            update(TokenUsage)
            .where(TokenUsage.session_id == existing.id)
            .values(session_id=None)
        )
        await db.delete(existing)
        await db.flush()

    logger.info("Generating new recommendations for %s", session_date)

    tracker = ApiCallTracker()

    # Playbook update happens before Call 1 so analysis is informed by learnings
    playbook = await update_playbook(db, session_date)

    # Load the user's declared strategy and analyst signal guidance
    strategy = load_strategy()
    guidance = load_analyst_guidance()

    current_positions = (await db.execute(select(Position))).scalars().all()
    current_symbols = [p.symbol for p in current_positions]

    # Initialize Finnhub client (None if no API key)
    finnhub_client = None
    if settings.finnhub_api_key:
        import finnhub
        finnhub_client = finnhub.Client(api_key=settings.finnhub_api_key)

    # Run momentum screener first so screener_symbols is available for AV call and gather
    screener_symbols = await fetch_momentum_screener(n=60, tracker=tracker)
    logger.info("Momentum screener added %d symbols: %s", len(screener_symbols), screener_symbols)
    research_symbols = list(set(WATCHLIST + current_symbols + screener_symbols))
    logger.info("Total research universe: %d symbols", len(research_symbols))

    # Phase 1 parallel fetch — everything that doesn't depend on Claude's output
    (
        price_data, news_data, earnings_surprise, insider_activity,
        market_context, fred_macro, polygon_news, av_technicals
    ) = await asyncio.gather(
        fetch_price_data(research_symbols, tracker=tracker),
        fetch_news(research_symbols, tracker=tracker),
        fetch_earnings_surprise(research_symbols, tracker=tracker),
        fetch_edgar_insider(research_symbols, tracker=tracker),
        fetch_market_context(session_date, research_symbols, tracker=tracker),
        fetch_fred_macro(settings.fred_api_key, tracker=tracker),
        fetch_polygon_news(research_symbols, settings.polygon_api_key, tracker=tracker),
        fetch_av_technicals(screener_symbols, settings.alpha_vantage_api_key, tracker=tracker),
    )

    # Compute technical indicators from price history (pure math, no I/O)
    technicals = compute_technicals(price_data)
    logger.info("Computed technicals for %d symbols", len(technicals))

    # Finnhub analyst consensus (sync SDK, run in executor)
    analyst_consensus = await asyncio.get_event_loop().run_in_executor(
        None, lambda: fetch_analyst_consensus_sync(research_symbols, finnhub_client, tracker=tracker)
    )
    logger.info("Fetched analyst consensus for %d symbols", len(analyst_consensus))

    portfolio = (await db.execute(select(Portfolio))).scalars().first()
    portfolio_dict = {
        "cash_balance": float(portfolio.cash_balance),
        "total_value": float(portfolio.cash_balance + sum(
            p.avg_cost_basis * p.shares for p in current_positions
        )),
        "positions": [
            {
                "symbol": p.symbol,
                "shares": float(p.shares),
                "avg_cost_basis": float(p.avg_cost_basis),
                "current_price": float(
                    price_data.get(p.symbol, {}).get("current_price", float(p.avg_cost_basis))
                ),
                "unrealized_gain": float(
                    (Decimal(str(
                        price_data.get(p.symbol, {}).get("current_price", float(p.avg_cost_basis))
                    )) - p.avg_cost_basis) * p.shares
                ),
                "days_held": (session_date - p.first_purchase_date).days,
                "tax_category": (
                    "long_term" if (session_date - p.first_purchase_date).days >= 365
                    else "short_term"
                ),
            }
            for p in current_positions
        ],
    }

    research_context = build_research_context(
        portfolio_dict,
        price_data,
        news_data,
        current_symbols,
        earnings_surprise=earnings_surprise,
        insider_activity=insider_activity,
        fred_macro=fred_macro,
        polygon_news=polygon_news,
        av_technicals=av_technicals,
        technicals=technicals,
        analyst_consensus=analyst_consensus,
    )

    # Persist session row early so we have an ID for token_usage FK
    session_row = RecommendationSession(
        session_date=session_date,
        raw_research=f"{market_context}\n\n{research_context}",
    )
    db.add(session_row)
    await db.flush()

    # ── Call 1: Analysis with extended thinking ────────────────────────────
    logger.info("Call 1: analysis with extended thinking")
    call1_user = f"Today's date: {session_date}\n\n{market_context}\n\n{research_context}"
    call1_response, analysis_text, analysis_thinking, candidates = call_analysis(
        strategy, guidance, call1_user, tracker=tracker,
    )

    # Record Call 1 token usage — API reports thinking tokens in usage object
    usage1 = call1_response.usage
    await record_usage(
        db,
        session_id=session_row.id,
        call_type="analysis",
        model=MODEL,
        input_tokens=usage1.input_tokens,
        output_tokens=usage1.output_tokens,
        thinking_tokens=getattr(usage1, "thinking_tokens", 0),
    )

    # Store analysis text (thinking + analysis) on the session row
    thinking_prefix = f"[THINKING]\n{analysis_thinking}\n\n[ANALYSIS]\n" if analysis_thinking else ""
    session_row.analysis_text = thinking_prefix + analysis_text

    logger.info("Call 1 candidates: %s", candidates)

    # ── Phase 2 fetch: options data for candidates only ────────────────────
    options_data = {}
    if candidates:
        logger.info("Fetching options data for candidates: %s", candidates)
        options_data = await fetch_options_data(candidates, tracker=tracker)

    # ── Call 2: Decision (standard, no extended thinking) ─────────────────
    logger.info("Call 2: trade decision")
    min_cash_pct = int(settings.min_cash_reserve_pct * 100)

    options_context = build_options_context(options_data) if options_data else ""
    call2_user = (
        f"Today's date: {session_date}\n\n"
        f"## Your Analysis\n{analysis_text}\n\n"
        f"{options_context}\n\n"
        f"## Current Portfolio\n"
        f"Cash available: ${portfolio_dict['cash_balance']:,.2f}\n"
        f"Total value: ${portfolio_dict['total_value']:,.2f}\n"
    )
    if portfolio_dict["positions"]:
        call2_user += "Held positions:\n"
        for pos in portfolio_dict["positions"]:
            call2_user += (
                f"  {pos['symbol']}: {pos['shares']} shares, "
                f"cost ${pos['avg_cost_basis']:.2f}, "
                f"now ${pos['current_price']:.2f}, "
                f"{pos['days_held']}d ({pos['tax_category']})\n"
            )

    call2_response, decision_raw, parsed = call_decision(
        strategy, guidance, playbook.content, min_cash_pct, call2_user, tracker=tracker,
    )

    usage2 = call2_response.usage
    await record_usage(
        db,
        session_id=session_row.id,
        call_type="decision",
        model=MODEL,
        input_tokens=usage2.input_tokens,
        output_tokens=usage2.output_tokens,
    )

    session_row.claude_response = decision_raw

    research_summary = parsed.get("research_summary", "")
    raw_recs = parsed.get("recommendations", [])[:3]

    # ── Call 3: Risk committee review (adversarial) ──────────────────────────
    if raw_recs:
        logger.info("Call 3: risk committee review of %d recommendations", len(raw_recs))
        playbook_excerpt = playbook.content[:500] if playbook else ""
        risk_prompt = build_risk_review_prompt(raw_recs, portfolio_dict, analysis_text, playbook_excerpt)

        call3_response, risk_raw = call_risk_review(risk_prompt, tracker=tracker)

        usage3 = call3_response.usage
        await record_usage(
            db,
            session_id=session_row.id,
            call_type="risk_review",
            model=MODEL,
            input_tokens=usage3.input_tokens,
            output_tokens=usage3.output_tokens,
        )

        risk_decisions = parse_risk_review_response(risk_raw)
        rejected_symbols = {
            d["symbol"].upper()
            for d in risk_decisions
            if d.get("verdict") == "reject" and d.get("action", "").lower() == "buy"
        }
        if rejected_symbols:
            logger.info("Risk committee rejected buys: %s", rejected_symbols)
            for d in risk_decisions:
                if d.get("verdict") == "reject":
                    logger.info("  %s %s: %s", d.get("action"), d.get("symbol"), d.get("reason"))

        # Filter out rejected buy recommendations (sells always pass through)
        raw_recs = [
            r for r in raw_recs
            if not (r.get("action", "").lower() == "buy" and r.get("symbol", "").upper() in rejected_symbols)
        ]

    recommendation_rows = []
    for rec in raw_recs:
        action = rec.get("action", "").lower()
        symbol = rec.get("symbol", "").upper()
        suggested_price = Decimal(str(rec.get("suggested_price", 0)))
        quantity = Decimal(str(rec.get("quantity", 0)))

        if action not in ("buy", "sell"):
            continue
        if quantity <= 0 or suggested_price <= 0:
            continue

        # Override suggested_price with the live price we actually fetched —
        # Claude's price output is based on what we sent it and may be stale.
        live_price = price_data.get(symbol, {}).get("current_price")
        if live_price and live_price > 0:
            suggested_price = Decimal(str(round(live_price, 4)))

        if action == "buy":
            estimated_cost = suggested_price * quantity
            min_cash = portfolio.cash_balance * settings.min_cash_reserve_pct
            if portfolio.cash_balance - estimated_cost < min_cash:
                logger.warning("Skipping %s buy — would violate cash reserve", symbol)
                continue

        key_risks = rec.get("key_risks") or ""

        # Wash sale warning: flag if this is a BUY of something sold within the last 30 days.
        # IRC §1091 disallows the loss deduction if you repurchase within 30 days of a loss sale.
        if action == "buy":
            recent_sell = await _get_recent_sell(db, symbol, session_date)
            if recent_sell is not None:
                sell_date = recent_sell.executed_at.date()
                gain = recent_sell.realized_gain
                loss_flag = gain is not None and gain < 0
                gain_str = f"${abs(gain):,.2f} {'loss' if loss_flag else 'gain'}" if gain is not None else "unknown P&L"
                wash_warning = (
                    f"⚠️ WASH SALE WARNING: {symbol} was sold on {sell_date} ({gain_str}). "
                    f"Repurchasing within 30 days"
                    + (" of a loss sale disallows the tax deduction (IRC §1091)." if loss_flag else " — no loss to disallow, but note the recent sale.")
                )
                logger.info("Wash sale flag on %s (sold %s, gain=%s)", symbol, sell_date, gain)
                key_risks = (wash_warning + "  " + key_risks).strip() if key_risks else wash_warning

        row = TradeRecommendation(
            session_id=session_row.id,
            symbol=symbol,
            action=action,
            suggested_price=suggested_price,
            quantity=quantity,
            reasoning=rec.get("reasoning", ""),
            confidence=rec.get("confidence", "medium"),
            key_risks=key_risks or None,
            status="pending",
        )
        db.add(row)
        recommendation_rows.append(row)

    await tracker.flush(db)
    await db.commit()
    for row in recommendation_rows:
        await db.refresh(row)

    rec_items = [
        RecommendationItem(
            id=row.id,
            symbol=row.symbol,
            action=row.action,
            suggested_price=row.suggested_price,
            quantity=row.quantity,
            estimated_cost=(row.suggested_price * row.quantity).quantize(Decimal("0.01")),
            reasoning=row.reasoning,
            confidence=row.confidence,
            key_risks=row.key_risks,
        )
        for row in recommendation_rows
    ]

    return RecommendationsResponse(
        session_id=session_row.id,
        date=session_date,
        portfolio_summary=portfolio_summary,
        recommendations=rec_items,
        research_summary=research_summary,
    )
