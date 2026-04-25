"""Core recommendation engine: two-call architecture with extended thinking."""
import asyncio
import json
import logging
from datetime import date, datetime

from ..tz import market_today
from decimal import Decimal

from ..api_tracker import ApiCallTracker
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..cost import record_usage, check_daily_cost_ceiling
from ..models import Portfolio, Position, RecommendationSession, TokenUsage, TradeHistory, TradeRecommendation
from ..schemas import PortfolioSummary, RecommendationItem, RecommendationsResponse
from .claude_client import MODEL, call_analysis, call_decision, call_risk_review, parse_json_response
from .playbook import get_playbook, update_playbook
from .risk_review import build_risk_review_prompt, parse_risk_review_response
from .portfolio import get_portfolio_summary
from .guidance import load_effective_guidance
from .strategy import load_strategy, load_strategy_json
from .technicals import compute_technicals
from .finnhub_data import fetch_analyst_consensus_sync, build_analyst_context
from ..drawdown_gate import update_peak_and_check
from ..correlation import find_high_correlations
from ..risk_gates import check_cash_floor, check_holdings_cap, check_position_cap
from .telegram import send_telegram
from .research import (
    WATCHLIST,
    build_options_context,
    build_research_context,
    compute_relative_strength,
    fetch_av_technicals,
    fetch_earnings_surprise,
    fetch_edgar_insider,
    fetch_factor_returns,
    fetch_fred_macro,
    fetch_market_context,
    fetch_mean_reversion_screener,
    fetch_momentum_screener,
    fetch_news,
    fetch_options_data,
    fetch_detailed_news,
    fetch_premarket_prices,
    fetch_price_data,
    fetch_sector_returns,
)

logger = logging.getLogger(__name__)

_CACHE_DIR = "/app/logs"


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


async def _wait_for_research_cache(session_date: date, max_wait_s: int = 120) -> dict | None:
    """Poll for the Phase 0 cache, giving a slow Phase 0 up to max_wait_s seconds.

    Returns the loaded cache dict, or None if it never appeared.
    Used by Phase 1 so a slow Phase 0 doesn't trigger a duplicate inline fetch.
    """
    import asyncio as _asyncio

    deadline = max_wait_s
    elapsed = 0
    poll_s = 5
    while elapsed <= deadline:
        cache = _load_research_cache(session_date)
        if cache is not None:
            if elapsed > 0:
                logger.warning("Phase 0 cache arrived after waiting %ds", elapsed)
            return cache
        if elapsed + poll_s > deadline:
            break
        await _asyncio.sleep(poll_s)
        elapsed += poll_s
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


async def _collect_failed_exits(
    db: AsyncSession, session_date: date, held_set: set[str]
) -> list[dict]:
    """Return prior-session SELL recs that didn't fill and whose symbol is still held.

    Walks back up to 5 prior sessions (one trading week) so a Friday expiry is
    still visible on Monday. Returns rows ordered most-recent-first.
    """
    recent_sessions = (await db.execute(
        select(RecommendationSession)
        .where(RecommendationSession.session_date < session_date)
        .order_by(RecommendationSession.session_date.desc())
        .limit(5)
    )).scalars().all()
    if not recent_sessions:
        return []

    session_ids = [s.id for s in recent_sessions]
    recs = (await db.execute(
        select(TradeRecommendation)
        .where(
            TradeRecommendation.session_id.in_(session_ids),
            TradeRecommendation.action == "sell",
            TradeRecommendation.status == "rejected",
        )
    )).scalars().all()

    sessions_by_id = {s.id: s for s in recent_sessions}
    # Most recent failed attempt per symbol only — older ones add noise.
    seen: set[str] = set()
    out: list[dict] = []
    for r in sorted(recs, key=lambda x: sessions_by_id[x.session_id].session_date, reverse=True):
        if r.symbol in seen or r.symbol not in held_set:
            continue
        seen.add(r.symbol)
        out.append({
            "symbol": r.symbol,
            "attempted_date": sessions_by_id[r.session_id].session_date.isoformat(),
            "intended_qty": float(r.quantity),
            "intended_price": float(r.suggested_price),
            "reasoning": r.reasoning,
            "key_risks": r.key_risks or "",
        })
    return out


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


# Reverse-map from sector ETF → human-readable sector name.
# Matches the GICS sectors used in analyst_guidance.md and strategy.json commentary.
_ETF_TO_SECTOR: dict[str, str] = {
    "XLK": "Technology",
    "XLC": "Communication Services",
    "XLY": "Consumer Discretionary",
    "XLF": "Financials",
    "XLV": "Healthcare",
    "XLE": "Energy",
    "XLI": "Industrials",
    "XLRE": "Real Estate",
    "XLU": "Utilities",
    "XLB": "Materials",
    "XLP": "Consumer Staples",
    "SPY": "Diversified",  # catch-all bucket in _SECTOR_ETF_MAP
}


def _get_sector_for_symbol(symbol: str) -> str | None:
    """Return GICS sector for the symbol; uses static ETF map first, Finnhub second.

    The "Diversified" catch-all bucket (returned when _SECTOR_ETF_MAP routes a
    symbol to SPY) is treated as a miss, so the Finnhub fallback resolves the
    actual sector — otherwise COIN/NET/SNOW would silently bypass the 40% sector
    cap because they're bucketed as "Diversified" rather than Financials/Technology.
    """
    from .research import _SECTOR_ETF_MAP  # local import avoids module-level circularity
    from .finnhub_data import fetch_sector_for_symbol

    etf = _SECTOR_ETF_MAP.get(symbol)
    if etf is not None:
        sector = _ETF_TO_SECTOR.get(etf)
        if sector and sector != "Diversified":
            return sector

    # Fallback: ask Finnhub (also handles the Diversified catch-all).
    return fetch_sector_for_symbol(symbol)


def check_sector_exposure(
    proposed_symbol: str,
    proposed_sector: str | None,
    proposed_dollars: Decimal,
    held_positions: list[dict],
    total_value: Decimal,
    max_sector_pct: float,
) -> bool:
    """Return True if the proposed buy keeps sector exposure <= max_sector_pct.

    Args:
        proposed_symbol:  Ticker being considered (used only for logging).
        proposed_sector:  GICS sector name, or None if unknown.
        proposed_dollars: Estimated cost of the proposed buy.
        held_positions:   List of dicts with keys ``sector`` and ``market_value``.
        total_value:      Total portfolio value (cash + all positions).
        max_sector_pct:   Hard cap, e.g. 40.0 for 40%.

    Returns False with a warning when sector is None — fail closed on
    missing metadata (audit M10) so a 40% cap is actually enforced.
    """
    if proposed_sector is None:
        logger.warning(
            "Sector gate REJECT %s: unknown sector — failing closed (audit M10)",
            proposed_symbol,
        )
        return False
    if total_value <= 0:
        return True

    current_sector_value = sum(
        (p.get("market_value") or Decimal("0"))
        for p in held_positions
        if (p.get("sector") or "").lower() == proposed_sector.lower()
    )
    post_buy_value = Decimal(str(current_sector_value)) + proposed_dollars
    post_buy_pct = float(post_buy_value) / float(total_value) * 100

    if post_buy_pct > max_sector_pct:
        logger.info(
            "Sector gate REJECT %s: %s exposure would be %.1f%% > %.1f%% cap",
            proposed_symbol, proposed_sector, post_buy_pct, max_sector_pct,
        )
        return False
    return True


def _compute_portfolio_total_value(
    cash: Decimal, positions, price_data: dict
) -> Decimal:
    """Cash + sum of (live_price * shares), falling back to avg_cost_basis if no live price."""
    total = cash
    for pos in positions:
        live = (price_data or {}).get(pos.symbol, {}).get("current_price")
        price = Decimal(str(live)) if live else Decimal(str(pos.avg_cost_basis))
        total += price * Decimal(str(pos.shares))
    return total


async def generate_recommendations(
    db: AsyncSession,
    session_date: date | None = None,
    force: bool = False,
) -> RecommendationsResponse:
    if session_date is None:
        session_date = market_today()

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

    # Load the user's declared strategy and analyst signal guidance. The
    # guidance helper bakes in any strategy.json.rule_overrides toggles.
    strategy = load_strategy()
    guidance = load_effective_guidance()

    current_positions = (await db.execute(select(Position))).scalars().all()
    current_symbols = [p.symbol for p in current_positions]

    # ── Try Phase 0 cache first, with a short wait for a slow Phase 0 ─────
    # If Phase 0 is still running when Phase 1 fires, silently falling through
    # to an inline fetch duplicates the slowest work in the pipeline. Instead,
    # wait up to 120s for the cache file to appear. Only fall back to an
    # inline fetch if Phase 0 is genuinely absent.
    cache = _load_research_cache(session_date)
    if cache is None:
        logger.warning("Phase 0 cache missing — waiting up to 120s for it to appear")
        cache = await _wait_for_research_cache(session_date, max_wait_s=120)

    if cache is not None:
        logger.info("Phase 0 cache HIT — skipping data fetches (%d symbols, fetched at %s)",
                     len(cache["research_symbols"]), cache["created_at"])
        price_data = cache["price_data"]
        news_data = cache["news_data"]
        earnings_surprise = cache["earnings_surprise"]
        insider_activity = cache["insider_activity"]
        market_context = cache["market_context"]
        fred_macro = cache["fred_macro"]
        detailed_news = cache.get("detailed_news") or cache.get("polygon_news", {})
        av_technicals = cache["av_technicals"]
        technicals = cache["technicals"]
        analyst_consensus = cache["analyst_consensus"]
        research_symbols = cache["research_symbols"]
        screener_symbols = cache["screener_symbols"]
        mean_reversion_symbols = cache.get("mean_reversion_symbols", [])
        relative_strength = cache.get("relative_strength", {})
        premarket_data = cache.get("premarket_data", {})
        factor_returns = cache.get("factor_returns", {})
    else:
        logger.info("Phase 0 cache MISS — fetching data inline")

        # Initialize Finnhub client (None if no API key)
        finnhub_client = None
        if settings.finnhub_api_key:
            import finnhub
            finnhub_client = finnhub.Client(api_key=settings.finnhub_api_key)

        # Run both screeners concurrently so research_symbols includes both
        # momentum and mean-reversion picks before parallel data fetch.
        screener_symbols, mean_reversion_symbols = await asyncio.gather(
            fetch_momentum_screener(n=20, tracker=tracker),
            fetch_mean_reversion_screener(n=10, tracker=tracker),
        )
        mean_reversion_symbols = [s for s in mean_reversion_symbols if s not in set(screener_symbols)]
        logger.info("Momentum screener added %d symbols: %s", len(screener_symbols), screener_symbols)
        logger.info(
            "Mean-reversion screener added %d symbols: %s",
            len(mean_reversion_symbols), mean_reversion_symbols,
        )
        research_symbols = list(
            set(WATCHLIST + current_symbols + screener_symbols + mean_reversion_symbols)
        )
        logger.info("Total research universe: %d symbols", len(research_symbols))

        # Parallel data fetch
        try:
            (
                price_data, news_data, earnings_surprise, insider_activity,
                market_context, fred_macro, detailed_news, av_technicals
            ) = await asyncio.wait_for(
                asyncio.gather(
                    fetch_price_data(research_symbols, tracker=tracker),
                    fetch_news(research_symbols, tracker=tracker),
                    fetch_earnings_surprise(research_symbols, tracker=tracker),
                    fetch_edgar_insider(research_symbols, tracker=tracker),
                    fetch_market_context(session_date, research_symbols, tracker=tracker),
                    fetch_fred_macro(settings.fred_api_key, tracker=tracker),
                    fetch_detailed_news(research_symbols, tracker=tracker),
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

        # Sector relative strength + factor leadership
        sector_returns = await fetch_sector_returns(tracker=tracker)
        relative_strength = compute_relative_strength(price_data, sector_returns)
        factor_returns = await fetch_factor_returns(tracker=tracker)
        premarket_data = await fetch_premarket_prices(research_symbols, tracker=tracker)

        # Finnhub analyst consensus (sync SDK, run in executor)
        analyst_consensus = await asyncio.get_running_loop().run_in_executor(
            None, lambda: fetch_analyst_consensus_sync(research_symbols, finnhub_client, tracker=tracker)
        )
        logger.info("Fetched analyst consensus for %d symbols", len(analyst_consensus))

    portfolio = (await db.execute(select(Portfolio))).scalars().first()
    portfolio_dict = {
        "cash_balance": float(portfolio.cash_balance),
        "total_value": float(_compute_portfolio_total_value(
            Decimal(str(portfolio.cash_balance)), current_positions, price_data
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

    # Twelvedata RSI and economic calendar — from Phase 0 cache (or None on inline fallback)
    twelvedata_rsi = cache.get("twelvedata_rsi") if cache else None
    economic_calendar_context = cache.get("economic_calendar_context") if cache else None

    # Performance snapshot — portfolio return vs benchmarks + trade metrics.
    # Injected at the top of research context so Claude calibrates its risk
    # appetite to its own track record. Best-effort: failure is non-fatal.
    performance_snapshot: dict | None = None
    try:
        from .portfolio import get_benchmark_comparison
        bench = await get_benchmark_comparison(db)
        performance_snapshot = {
            "portfolio_return_pct": bench.portfolio_return_pct,
            "since_date": bench.since_date.isoformat() if bench.since_date else None,
            "benchmarks": [
                {"symbol": b.symbol, "name": b.name, "return_pct": b.return_pct}
                for b in bench.benchmarks
            ],
            "trade_metrics": bench.trade_metrics or {},
        }
    except Exception:  # noqa: BLE001 — snapshot is best-effort
        logger.warning("Failed to build performance snapshot — context will omit it", exc_info=True)

    # Failed-exit retry signal: SELL recs from the prior session whose symbol
    # is still held. Without this, if a sell limit expires unfilled the analyst
    # has no signal on the next session — today's LRCX/GEV earnings-risk exits
    # could drift straight into earnings because Phase 1 doesn't know they
    # were attempted yesterday.
    failed_exits: list[dict] | None = None
    try:
        failed_exits = await _collect_failed_exits(db, session_date, held_set=set(current_symbols))
        if failed_exits:
            logger.info(
                "Failed-exit retry signal: %d prior SELLs still held (%s)",
                len(failed_exits), ", ".join(f["symbol"] for f in failed_exits),
            )
    except Exception:  # noqa: BLE001 — best-effort
        logger.warning("Failed to collect failed-exit signals", exc_info=True)

    research_context = build_research_context(
        portfolio_dict,
        price_data,
        news_data,
        current_symbols,
        earnings_surprise=earnings_surprise,
        insider_activity=insider_activity,
        fred_macro=fred_macro,
        detailed_news=detailed_news,
        av_technicals=av_technicals,
        technicals=technicals,
        analyst_consensus=analyst_consensus,
        relative_strength=relative_strength,
        premarket_data=premarket_data,
        twelvedata_rsi=twelvedata_rsi,
        economic_calendar_context=economic_calendar_context,
        factor_returns=factor_returns,
        performance_snapshot=performance_snapshot,
        failed_exits=failed_exits,
        mean_reversion_symbols=mean_reversion_symbols,
    )

    # Persist session row early so we have an ID for token_usage FK
    session_row = RecommendationSession(
        session_date=session_date,
        raw_research=f"{market_context}\n\n{research_context}",
    )
    db.add(session_row)
    await db.flush()

    # ── Daily cost ceiling check ────────────────────────────────────────────
    await check_daily_cost_ceiling(db)

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

    call1_response, analysis_text, analysis_thinking, candidates, position_actions = await call_analysis(
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

    # Store only the clean analysis prose. Thinking tokens are expensive but
    # noisy — log their length for observability and discard from DB.
    session_row.analysis_text = analysis_text
    if analysis_thinking:
        logger.info("Call 1 thinking: %d chars (not stored)", len(analysis_thinking))

    candidate_symbols = [c.symbol for c in candidates]
    logger.info(
        "Call 1: %d candidates (%s), %d position actions",
        len(candidates), candidate_symbols, len(position_actions),
    )

    # ── Phase 2 fetch: options data for candidates only ────────────────────
    options_data = {}
    if candidate_symbols:
        logger.info("Fetching options data for candidates: %s", candidate_symbols)
        options_data = await fetch_options_data(candidate_symbols, tracker=tracker)

    # ── Call 2: Decision (standard, no extended thinking) ─────────────────
    logger.info("Call 2: trade decision")
    min_cash_pct = int(settings.min_cash_reserve_pct * 100)

    # Structured handoff from Analysis — give Decision the pre-screened shortlist
    # and position actions as explicit JSON rather than requiring it to re-parse prose.
    handoff = {
        "candidates": [c.model_dump() for c in candidates],
        "position_actions": [p.model_dump() for p in position_actions],
    }
    options_context = build_options_context(options_data) if options_data else ""
    call2_user = (
        f"Today's date: {session_date}\n\n"
        f"## Analysis Summary\n{analysis_text}\n\n"
        f"## Pre-Screened Candidates & Position Actions (from Analysis)\n"
        f"```json\n{json.dumps(handoff, indent=2)}\n```\n\n"
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
                f"{pos['days_held']}d held\n"
            )

    strategy_conc = strategy_json.get("concentration", {})
    call2_response, decision_raw, parsed = await call_decision(
        strategy, guidance, playbook.content, min_cash_pct, call2_user,
        max_position_pct=strategy_conc.get("max_position_pct", 33),
        max_holdings=strategy_conc.get("max_holdings", 10),
        tracker=tracker,
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

    # DIAG: log a truncated slice of Call 2's raw response so diagnoses survive
    # the session delete that happens on zero-rec runs.
    logger.info("Call 2 raw response (first 2000 chars):\n%s", (decision_raw or "")[:2000])

    research_summary = parsed.get("research_summary", "")
    raw_recs = parsed.get("recommendations", [])[:3]

    # DIAG: dump every rec Call 2 returned so silent drops downstream are visible.
    logger.info("Call 2 parsed %d recommendations:", len(raw_recs))
    for i, r in enumerate(raw_recs):
        logger.info(
            "  [%d] action=%s symbol=%s qty=%s price=%s confidence=%s",
            i,
            r.get("action"),
            r.get("symbol"),
            r.get("quantity"),
            r.get("suggested_price"),
            r.get("confidence"),
        )

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

        # DIAG: log risk committee's raw verdict so rejections are inspectable.
        logger.info("Call 3 raw response (first 2000 chars):\n%s", (risk_raw or "")[:2000])

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

        if risk_decisions is None:
            # Parse failure → fail-closed: reject ALL buys, let sells through
            logger.warning("Risk review parse failure — rejecting all buys (fail-closed)")
            await send_telegram(
                "TRADEBOT // Risk committee parse failure — all BUY recs rejected (fail-closed)"
            )
            raw_recs = [r for r in raw_recs if r.get("action", "").lower() != "buy"]
        else:
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

    # Build the list of held positions enriched with sector and market_value for the
    # sector-exposure gate.  We compute this once before the loop so the gate has a
    # stable baseline; accepted buys from *this run* are appended dynamically below.
    # Sector lookups are offloaded to a thread pool (asyncio.to_thread) because
    # _get_sector_for_symbol may call Finnhub via sync requests.get + retry_call
    # (up to 9s of blocking on outage) — running it on the event loop would stall
    # all other async work.  Symbols are gathered concurrently so the batch cost is
    # the slowest single lookup, not the sum.
    total_value_decimal = Decimal(str(portfolio_dict["total_value"]))
    held_sectors: list[str | None] = await asyncio.gather(
        *(asyncio.to_thread(_get_sector_for_symbol, pos.symbol) for pos in current_positions)
    )
    held_positions_for_sector: list[dict] = []
    for pos, sector in zip(current_positions, held_sectors):
        live_px = price_data.get(pos.symbol, {}).get("current_price")
        mkt_val = (
            Decimal(str(live_px)) * Decimal(str(pos.shares))
            if live_px
            else Decimal(str(pos.avg_cost_basis)) * Decimal(str(pos.shares))
        )
        held_positions_for_sector.append(
            {
                "symbol": pos.symbol,
                "sector": sector,
                "market_value": mkt_val,
            }
        )

    # Running cash tracks how much cash remains after each accepted buy in this
    # session.  Initialized from the actual balance; decremented on each accepted
    # buy so the floor check accounts for cumulative spend (audit H1 fix).
    running_cash = Decimal(str(portfolio.cash_balance))
    total_value_for_floor = Decimal(str(portfolio_dict["total_value"]))
    reserve_pct = Decimal(str(settings.min_cash_reserve_pct))  # already a fraction (0.10)

    # Running set of symbols already held or accepted as new buys this session.
    # Used by check_holdings_cap so successive new-symbol buys see the correct
    # projected count (audit H2 fix).
    held_symbol_set = {p.symbol.upper() for p in current_positions}
    accepted_new_symbols: set[str] = set()

    # Per-symbol market value map for post-trade position cap check (audit H3 fix).
    # Seeded from held_positions_for_sector which already has live-price market values.
    existing_position_value: dict[str, Decimal] = {
        pos["symbol"].upper(): Decimal(str(pos["market_value"]))
        for pos in held_positions_for_sector
    }

    recommendation_rows = []
    for rec in raw_recs:
        action = rec.get("action", "").lower()
        symbol = rec.get("symbol", "").upper()
        suggested_price = Decimal(str(rec.get("suggested_price", 0)))
        quantity = Decimal(str(rec.get("quantity", 0)))

        if action not in ("buy", "sell"):
            logger.info("Dropping rec (action=%r not buy/sell): %s", action, symbol)
            continue
        if quantity <= 0 or suggested_price <= 0:
            logger.info(
                "Dropping %s %s — invalid qty/price (qty=%s price=%s)",
                action, symbol, quantity, suggested_price,
            )
            continue

        # Override suggested_price with the live price we actually fetched —
        # Claude's price output is based on what we sent it and may be stale.
        live_price = price_data.get(symbol, {}).get("current_price")
        if live_price and live_price > 0:
            suggested_price = Decimal(str(round(live_price, 4)))

        if action == "buy":
            estimated_cost = suggested_price * quantity
            cash_check = check_cash_floor(
                current_cash=running_cash,
                total_portfolio_value=total_value_for_floor,
                buy_notional=estimated_cost,
                reserve_pct=reserve_pct,
            )
            if not cash_check.passed:
                logger.warning(
                    "Skipping %s buy — cash floor: %s",
                    symbol, cash_check.reason,
                )
                await send_telegram(
                    f"TRADEBOT // Cash reserve gate: {symbol} BUY skipped — {cash_check.reason}"
                )
                continue

            # Max position size gate — post-trade total exposure (audit H3 fix).
            # existing_value includes current market value of any existing position;
            # adding buy_notional gives the post-trade total so add-ons can't stack
            # past the cap while each individual tranche looks small.
            existing_value = existing_position_value.get(symbol.upper(), Decimal("0"))
            position_check = check_position_cap(
                existing_market_value=existing_value,
                buy_notional=estimated_cost,
                total_portfolio_value=total_value_for_floor,
                max_position_pct=Decimal(str(strategy_conc.get("max_position_pct", 33))),
            )
            if not position_check.passed:
                logger.warning(
                    "Skipping %s buy — position cap: %s", symbol, position_check.reason,
                )
                await send_telegram(
                    f"TRADEBOT // Position size gate: {symbol} BUY skipped — {position_check.reason}"
                )
                continue

            # Max holdings gate (cumulative — tracks accepted new buys this session)
            holdings_check = check_holdings_cap(
                held_symbols=held_symbol_set,
                accepted_new_symbols=accepted_new_symbols,
                proposed_symbol=symbol,
                max_holdings=strategy_conc.get("max_holdings", 10),
            )
            if not holdings_check.passed:
                logger.warning(
                    "Skipping %s buy — holdings cap: %s", symbol, holdings_check.reason,
                )
                await send_telegram(
                    f"TRADEBOT // Holdings gate: {symbol} BUY skipped — {holdings_check.reason}"
                )
                continue

            # Sector concentration gate — offload to thread so sync Finnhub HTTP
            # calls don't block the event loop (same rationale as held_sectors above).
            max_sector_pct = strategy_conc.get("max_sector_pct", 40.0)
            symbol_sector = await asyncio.to_thread(_get_sector_for_symbol, symbol)
            if not check_sector_exposure(
                symbol,
                symbol_sector,
                estimated_cost,
                held_positions_for_sector,
                total_value_decimal,
                max_sector_pct,
            ):
                sector_label = symbol_sector or "unknown sector"
                logger.warning(
                    "Skipping %s buy — sector concentration gate rejected (%s, cap=%.0f%%)",
                    symbol, sector_label, max_sector_pct,
                )
                await send_telegram(
                    f"TRADEBOT // Sector gate: {symbol} BUY skipped — "
                    f"would breach {max_sector_pct:.0f}% {sector_label} cap"
                )
                continue

            # Track this accepted buy so subsequent buys in the same sector see the
            # correct running total (prevents two same-sector buys slipping through).
            held_positions_for_sector.append(
                {
                    "symbol": symbol,
                    "sector": symbol_sector,
                    "market_value": estimated_cost,
                }
            )

            # Decrement running cash so subsequent buys in this session see the
            # correct available balance (audit H1 fix — cumulative cash tracking).
            running_cash = cash_check.projected_cash

            # Track accepted new symbols so the holdings cap sees the cumulative
            # count across the whole session (audit H2 fix).
            if symbol.upper() not in held_symbol_set:
                accepted_new_symbols.add(symbol.upper())

            # Update running position exposure so a second add-on buy in the same
            # session can't stack past the cap (audit H3 fix).
            existing_position_value[symbol.upper()] = existing_value + estimated_cost

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

    # If no recommendations survived filtering, delete the session so cron can
    # retry without needing force=True (#12: empty session caching blocks retries)
    if not recommendation_rows:
        logger.info("No recommendations survived filtering — removing empty session to allow retry")
        await db.execute(
            update(TokenUsage).where(TokenUsage.session_id == session_row.id).values(session_id=None)
        )
        await db.delete(session_row)

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
