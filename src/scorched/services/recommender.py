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
from .strategy import load_analyst_guidance, load_strategy, load_strategy_json
from .technicals import compute_technicals
from .finnhub_data import fetch_analyst_consensus_sync, build_analyst_context
from ..drawdown_gate import update_peak_and_check
from ..correlation import find_high_correlations
from .research import (
    WATCHLIST,
    build_options_context,
    build_research_context,
    compute_relative_strength,
    fetch_av_technicals,
    fetch_earnings_surprise,
    fetch_edgar_insider,
    fetch_fred_macro,
    fetch_market_context,
    fetch_momentum_screener,
    fetch_news,
    fetch_options_data,
    fetch_polygon_news,
    fetch_premarket_prices,
    fetch_price_data,
    fetch_sector_returns,
)

logger = logging.getLogger(__name__)

_CACHE_DIR = "/tmp"


def _load_research_cache(session_date: date) -> dict | None:
    """Load Phase 0 research cache for today. Returns None on miss or error."""
    import os
    cache_path = os.path.join(_CACHE_DIR, f"tradebot_research_cache_{session_date.isoformat()}.json")
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path) as f:
            cache = json.load(f)
        if cache.get("date") != session_date.isoformat():
            logger.warning("Phase 0 cache date mismatch: %s != %s", cache.get("date"), session_date)
            return None
        return cache
    except (json.JSONDecodeError, OSError, KeyError) as exc:
        logger.warning("Phase 0 cache load failed: %s", exc)
        return None


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

    # ── Try Phase 0 cache first, fall back to inline fetch ────────────────
    cache = _load_research_cache(session_date)

    if cache is not None:
        logger.info("Phase 0 cache HIT — skipping data fetches (%d symbols, fetched at %s)",
                     len(cache["research_symbols"]), cache["created_at"])
        price_data = cache["price_data"]
        news_data = cache["news_data"]
        earnings_surprise = cache["earnings_surprise"]
        insider_activity = cache["insider_activity"]
        market_context = cache["market_context"]
        fred_macro = cache["fred_macro"]
        polygon_news = cache["polygon_news"]
        av_technicals = cache["av_technicals"]
        technicals = cache["technicals"]
        analyst_consensus = cache["analyst_consensus"]
        research_symbols = cache["research_symbols"]
        screener_symbols = cache["screener_symbols"]
        relative_strength = cache.get("relative_strength", {})
        premarket_data = cache.get("premarket_data", {})
    else:
        logger.info("Phase 0 cache MISS — fetching data inline")

        # Initialize Finnhub client (None if no API key)
        finnhub_client = None
        if settings.finnhub_api_key:
            import finnhub
            finnhub_client = finnhub.Client(api_key=settings.finnhub_api_key)

        # Run momentum screener first so screener_symbols is available for AV call and gather
        screener_symbols = await fetch_momentum_screener(n=20, tracker=tracker)
        logger.info("Momentum screener added %d symbols: %s", len(screener_symbols), screener_symbols)
        research_symbols = list(set(WATCHLIST + current_symbols + screener_symbols))
        logger.info("Total research universe: %d symbols", len(research_symbols))

        # Parallel data fetch
        try:
            (
                price_data, news_data, earnings_surprise, insider_activity,
                market_context, fred_macro, polygon_news, av_technicals
            ) = await asyncio.wait_for(
                asyncio.gather(
                    fetch_price_data(research_symbols, tracker=tracker),
                    fetch_news(research_symbols, tracker=tracker),
                    fetch_earnings_surprise(research_symbols, tracker=tracker),
                    fetch_edgar_insider(research_symbols, tracker=tracker),
                    fetch_market_context(session_date, research_symbols, tracker=tracker),
                    fetch_fred_macro(settings.fred_api_key, tracker=tracker),
                    fetch_polygon_news(research_symbols, settings.polygon_api_key, tracker=tracker),
                    fetch_av_technicals(screener_symbols, settings.alpha_vantage_api_key, tracker=tracker),
                ),
                timeout=600,
            )
        except asyncio.TimeoutError:
            logger.warning("Phase 1 parallel data fetch timed out after 600s")
            raise

        # Compute technical indicators from price history (pure math, no I/O)
        technicals = compute_technicals(price_data)
        logger.info("Computed technicals for %d symbols", len(technicals))

        # Sector relative strength
        sector_returns = await fetch_sector_returns(tracker=tracker)
        relative_strength = compute_relative_strength(price_data, sector_returns)
        premarket_data = await fetch_premarket_prices(research_symbols, tracker=tracker)

        # Finnhub analyst consensus (sync SDK, run in executor)
        analyst_consensus = await asyncio.get_running_loop().run_in_executor(
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

    # ── Drawdown gate check ────────────────────────────────────────────────
    strategy_json = load_strategy_json()
    drawdown_config = strategy_json.get("drawdown_gate", {"enabled": True, "max_drawdown_pct": 8.0})
    drawdown_result = await update_peak_and_check(db, price_data, drawdown_config)
    drawdown_blocked = drawdown_result.blocked
    if drawdown_blocked:
        logger.warning(
            "Drawdown gate ACTIVE — buys will be filtered after Claude calls. "
            "Drawdown: %.1f%% (threshold: %.1f%%)",
            drawdown_result.current_drawdown_pct, drawdown_result.threshold_pct,
        )

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
        relative_strength=relative_strength,
        premarket_data=premarket_data,
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

    # Log context size — warn if approaching model limits
    context_chars = len(call1_user)
    context_est_tokens = context_chars // 4  # rough estimate: ~4 chars per token
    logger.info("Call 1 context size: %d chars (~%dk tokens)", context_chars, context_est_tokens // 1000)
    if context_est_tokens > 80_000:
        logger.warning(
            "Call 1 context is very large (%dk est. tokens) — risk of hitting context window limit. "
            "Consider reducing watchlist size or screener scope.",
            context_est_tokens // 1000,
        )

    call1_response, analysis_text, analysis_thinking, candidates = await call_analysis(
        strategy, guidance, call1_user, tracker=tracker,
    )

    # Record Call 1 token usage — with extended thinking, output_tokens includes
    # both thinking and text tokens. Estimate split from actual text length.
    usage1 = call1_response.usage
    text_tokens_est = len(analysis_text) // 4  # rough char-to-token ratio
    total_output = usage1.output_tokens
    thinking_tokens_est = max(0, total_output - text_tokens_est)
    await record_usage(
        db,
        session_id=session_row.id,
        call_type="analysis",
        model=MODEL,
        input_tokens=usage1.input_tokens,
        output_tokens=text_tokens_est,
        thinking_tokens=thinking_tokens_est,
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

    call2_response, decision_raw, parsed = await call_decision(
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

    # ── Drawdown gate: filter buys if portfolio drawdown exceeds threshold ──
    if drawdown_blocked:
        buy_count = sum(1 for r in raw_recs if r.get("action", "").lower() == "buy")
        if buy_count > 0:
            logger.warning(
                "Drawdown gate filtering %d buy recommendation(s) — portfolio down %.1f%% from peak",
                buy_count, drawdown_result.current_drawdown_pct,
            )
            raw_recs = [r for r in raw_recs if r.get("action", "").lower() != "buy"]

    # ── Correlation warnings for buy candidates ─────────────────────────────
    correlation_warnings: list[str] = []
    for rec in raw_recs:
        if rec.get("action", "").lower() != "buy":
            continue
        symbol = rec.get("symbol", "").upper()
        high_corrs = find_high_correlations(symbol, current_symbols, price_data)
        if high_corrs:
            corr_strs = ", ".join(
                f"{c['symbol']} (r={c['correlation']:.2f})" for c in high_corrs
            )
            warning = f"{symbol} is highly correlated with held position(s): {corr_strs}"
            correlation_warnings.append(warning)
            logger.info("Correlation warning: %s", warning)

    # ── Call 3: Risk committee review (adversarial) ──────────────────────────
    if raw_recs:
        logger.info("Call 3: risk committee review of %d recommendations", len(raw_recs))
        playbook_excerpt = playbook.content if playbook else ""
        risk_prompt = build_risk_review_prompt(
            raw_recs, portfolio_dict, analysis_text, playbook_excerpt,
            correlation_warnings=correlation_warnings,
        )

        call3_response, risk_raw = await call_risk_review(risk_prompt, tracker=tracker)

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

        # Correlation warning: flag if this BUY is highly correlated with held positions
        if action == "buy":
            high_corrs = find_high_correlations(symbol, current_symbols, price_data)
            if high_corrs:
                corr_strs = ", ".join(
                    f"{c['symbol']} (r={c['correlation']:.2f})" for c in high_corrs
                )
                corr_warning = (
                    f"⚠️ HIGH CORRELATION: {symbol} has high 20-day return correlation with "
                    f"held position(s): {corr_strs}. These positions may behave as a single concentrated bet."
                )
                key_risks = (corr_warning + "  " + key_risks).strip() if key_risks else corr_warning

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
