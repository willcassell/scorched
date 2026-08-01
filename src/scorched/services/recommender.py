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
from ..models import GateDecision, Portfolio, Position, RecommendationSession, TokenUsage, TradeHistory, TradeRecommendation
from ..schemas import PortfolioSummary, RecommendationItem, RecommendationsResponse
from .claude_client import MODEL, call_analysis, call_decision, call_risk_review, parse_json_response
from .gate_decisions import (
    PHASE_FILTER,
    PHASE_RISK_REVIEW,
    record_gate_decision,
)
from .playbook import get_playbook, update_playbook
from .risk_review import build_risk_review_prompt, parse_risk_review_response
from .portfolio import get_portfolio_summary
from .guidance import load_effective_guidance
from .strategy import load_strategy, load_strategy_json
from .technicals import compute_technicals
from .finnhub_data import fetch_analyst_consensus_sync
from ..drawdown_gate import update_peak_and_check
from ..correlation import find_high_correlations
from ..risk_gates import (
    DEFAULT_MAX_POSITION_PCT,
    check_cash_floor,
    check_holdings_cap,
    check_position_cap,
)
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
    fetch_news,
    fetch_options_data,
    fetch_detailed_news,
    fetch_premarket_prices,
    fetch_price_data,
    fetch_screeners,
    fetch_sector_returns,
    gate_cached_mean_reversion,
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
            status=r.status,
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
        # Fail closed — a zero/negative total means portfolio state is broken,
        # not that sector exposure is fine (consistent with the M10 stance above).
        logger.warning(
            "Sector gate REJECT %s: non-positive total_value=%s — failing closed",
            proposed_symbol, total_value,
        )
        return False

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


def check_factor_alignment(
    candidate_20d_return: float | None,
    factor_returns: dict,
    config: dict,
) -> tuple[bool, str | None]:
    """Experiment gate (B-momentum-discipline, 2026-06-18): code-enforced
    version of analyst_guidance hard rule #9.

    In a momentum-led regime, block buys that lack their own positive momentum —
    the falling-knife / mean-reversion entries that produced the ~$5.8k
    energy/commodity loss bucket. Backtest finding: our discretionary entries
    lose under every exit policy, so the lever is entry quality, not exits.

    Momentum regime := a momentum factor (MTUM/SPMO/QQQ) leads SPY by
    >= min_factor_lead_pts over the trailing 20 trading days. When that holds,
    the candidate's own 20-day return must be >= min_candidate_mom_pct.

    Returns (passed, reason). FAILS CLOSED on missing data (matching the
    sector-gate posture, audit M10): missing SPY/factor returns, or an
    unknown candidate momentum while a momentum regime is active, both
    return (False, "factor_data_missing") rather than silently passing the
    buy. A data gap is not evidence the buy is safe.
    """
    if not config.get("enabled", False):
        return True, None
    spy = (factor_returns.get("SPY") or {}).get("20d")
    momentum_leaders = [
        (factor_returns.get(f) or {}).get("20d") for f in ("MTUM", "SPMO", "QQQ")
    ]
    momentum_leaders = [x for x in momentum_leaders if x is not None]
    if spy is None or not momentum_leaders:
        logger.warning(
            "Factor gate: missing SPY/factor return data — failing closed "
            "(factor_returns=%s)", factor_returns,
        )
        return False, "factor_data_missing"
    lead = max(momentum_leaders) - spy
    if lead < float(config.get("min_factor_lead_pts", 3.0)):
        return True, None  # no clear momentum regime — gate is inactive
    if candidate_20d_return is None:
        logger.warning(
            "Factor gate: momentum regime active (lead %.1fpts) but candidate's "
            "own 20d return is unknown — failing closed", lead,
        )
        return False, "factor_data_missing"
    floor = float(config.get("min_candidate_mom_pct", 0.0))
    if candidate_20d_return < floor:
        return False, (
            f"momentum regime (factor lead {lead:.1f}pts) but candidate's own "
            f"20d return {candidate_20d_return:.1f}% < {floor:.1f}% floor"
        )
    return True, None


def resolve_candidate_20d_momentum(symbol: str, price_row: dict | None) -> float | None:
    """Resolve a candidate's own 20-trading-day return for the factor gate.

    Prefers `trailing_20d_return_pct` (true 20-trading-day return computed
    from Alpaca daily bars: close[-1]/close[-21]-1). Falls back to
    `month_change_pct` (a calendar-month change vs. live price — a coarser,
    non-trading-day-aligned proxy) only when the true figure is absent, and
    logs a warning so a persistently-missing trailing figure is visible in
    the logs rather than silently degrading data quality forever.
    """
    if not price_row:
        return None
    trailing = price_row.get("trailing_20d_return_pct")
    if trailing is not None:
        return trailing
    month_change = price_row.get("month_change_pct")
    if month_change is not None:
        logger.warning(
            "Factor gate: trailing_20d_return_pct missing for %s — falling back "
            "to month_change_pct (calendar-month, not true 20-trading-day return)",
            symbol,
        )
        return month_change
    return None


async def check_reentry_cooldown(
    db: AsyncSession, symbol: str, cooldown_days: int
) -> tuple[bool, str | None]:
    """Block a BUY if `symbol` has a SELL in trade_history within the last
    `cooldown_days` NYSE trading days (Task 9: whipsaw re-entry guard).

    Motivation: whipsaw churn (e.g. CVX sold 6/11, re-bought 6/15) cost real
    money that Experiment B deferred fixing. This is a pure cooldown timer —
    it has no opinion on thesis quality, only on how recently we exited.

    Trading-day math uses `pandas_market_calendars`, same library/pattern as
    `_is_market_open` above, so weekends/holidays don't inflate or shrink the
    window versus a naive calendar-day count. A sell exactly `cooldown_days`
    trading days ago is ALLOWED (strictly-greater-than passes) — only sells
    strictly more recent than that boundary block the buy.

    Returns (allowed, reason). Fails OPEN (allowed=True) on a DB error OR a
    malformed `cooldown_days` (e.g. a hand-edited strategy.json with a string
    `"3"`, `null`, or a list) — both are logged at ERROR level. Coercing
    `cooldown_days` to `int` happens inside this same try/except: a
    misconfigured value must never raise into the hot path and abort the
    whole session's recommendation generation, consistent with the project's
    best-effort gate-recording convention (telemetry/config failures must
    never block a trade that would otherwise clear). This is distinct from
    the fail-CLOSED posture used for missing market data in
    check_factor_alignment / check_sector_exposure (a DB/config problem here
    isn't evidence of anything about the candidate, whereas missing
    momentum/sector data is).
    """
    try:
        cooldown_days = int(cooldown_days)
        if cooldown_days <= 0:
            return True, None

        import pandas_market_calendars as mcal
        from datetime import timedelta

        today = market_today()
        nyse = mcal.get_calendar("NYSE")
        # Generous lookback so we always resolve >= cooldown_days sessions
        # even across long holiday clusters (e.g. Thanksgiving + Christmas).
        window_start = today - timedelta(days=cooldown_days * 3 + 15)
        sessions = [
            d.date() for d in nyse.valid_days(start_date=window_start, end_date=today)
        ]
        if len(sessions) < cooldown_days:
            # Shouldn't happen with the buffer above; fail safe by treating
            # the earliest available session as the cutoff.
            cutoff_date = sessions[0] if sessions else today
        else:
            cutoff_date = sessions[-cooldown_days]
        cutoff_dt = datetime.combine(cutoff_date, datetime.min.time())

        result = await db.execute(
            select(TradeHistory)
            .where(
                TradeHistory.symbol == symbol.upper(),
                TradeHistory.action == "sell",
                TradeHistory.executed_at >= cutoff_dt,
            )
            .order_by(TradeHistory.executed_at.desc())
            .limit(1)
        )
        recent_sell = result.scalars().first()
        if recent_sell is not None:
            sell_date = recent_sell.executed_at.date()
            return False, (
                f"{symbol} sold {sell_date.isoformat()} — within the "
                f"{cooldown_days}-NYSE-trading-day re-entry cooldown"
            )
        return True, None
    except Exception as e:
        logger.error(
            "reentry_cooldown check failed for %s (%s: %s) — failing open",
            symbol, type(e).__name__, e,
        )
        return True, f"reentry_cooldown check failed ({e}) — failing open"


def check_mechanical_entry(
    symbol: str, price_data: dict, technicals: dict, cfg: dict
) -> tuple[bool, str | None]:
    """Code-enforced mechanical entry minimums for every BUY (Task 10,
    analyst_guidance.md hard rule #12).

    Backtest finding (6/18, Experiment B): LLM-originated entries lose to a
    mechanical breakout rule. This makes the three logical minimums named in
    hard rule #12 non-negotiable in code — Claude's role narrows to
    selecting/vetoing among names that already qualify, not to
    exception-making on ones that don't.

    Field mapping (there is no field literally named momentum_5d_pct /
    rel_volume / above_20dma anywhere in the codebase — those are the
    *logical* criteria; the real fields backing them are):
      - momentum    -> price_data[symbol]["week_change_pct"] (the true
        5-trading-day return, already computed in _fetch_price_data_sync).
      - rel_volume  -> technicals[symbol]["volume"]["relative_volume"]
        (calc_volume_profile's latest/avg-20d ratio). The 9:45 AM
        partial-bar bug that produced rel=0.0x on nearly every symbol was
        fixed in commit 997c2af (strips today's partial bar before
        averaging) — verified clean against the live 2026-07-31 Phase-0
        cache: 78/78 symbols had a real, non-zero value in a plausible
        0.52x-3.42x range. No systematic gap found, so this criterion gets
        no special-cased warn+pass — same fail-closed default as the other
        two.
      - above_20dma -> price_data[symbol]["current_price"] compared against
        technicals[symbol]["bollinger"]["middle"] (calc_bollinger_bands' 20
        -period SMA, reused rather than adding a redundant MA calc).

    Missing an individual field blocks with "mechanical_data_missing" UNLESS
    cfg["fail_open_on_missing"] is True (default False), in which case that
    one criterion passes with a logged warning and the others are still
    evaluated normally.

    Returns (allowed, reason). reason is one of "mechanical_momentum" /
    "mechanical_volume" / "mechanical_trend" / "mechanical_data_missing", or
    None on pass.
    """
    if not cfg.get("enabled", True):
        return True, None

    fail_open = bool(cfg.get("fail_open_on_missing", False))
    row = price_data.get(symbol) or {}
    tech = technicals.get(symbol) or {}
    volume = tech.get("volume") or {}
    bollinger = tech.get("bollinger") or {}

    # --- Criterion 1: 5-day momentum > floor (strict) ---
    momentum = row.get("week_change_pct")
    min_momentum = float(cfg.get("min_momentum_5d_pct", 0.0))
    if momentum is None:
        if not fail_open:
            logger.warning(
                "Mechanical gate REJECT %s: momentum (week_change_pct) "
                "missing — failing closed", symbol,
            )
            return False, "mechanical_data_missing"
        logger.warning(
            "Mechanical gate: %s momentum missing — failing open "
            "(fail_open_on_missing=True)", symbol,
        )
    elif momentum <= min_momentum:
        return False, "mechanical_momentum"

    # --- Criterion 2: relative volume >= floor (non-strict) ---
    rel_volume = volume.get("relative_volume")
    min_rel_volume = float(cfg.get("min_rel_volume", 1.0))
    if rel_volume is None:
        if not fail_open:
            logger.warning(
                "Mechanical gate REJECT %s: relative_volume missing — "
                "failing closed", symbol,
            )
            return False, "mechanical_data_missing"
        logger.warning(
            "Mechanical gate: %s relative_volume missing — failing open "
            "(fail_open_on_missing=True)", symbol,
        )
    elif rel_volume < min_rel_volume:
        return False, "mechanical_volume"

    # --- Criterion 3: price above 20-day MA (strict), if required ---
    if cfg.get("require_above_20dma", True):
        current_price = row.get("current_price")
        ma20 = bollinger.get("middle")
        if current_price is None or ma20 is None:
            if not fail_open:
                logger.warning(
                    "Mechanical gate REJECT %s: 20dma data missing — "
                    "failing closed", symbol,
                )
                return False, "mechanical_data_missing"
            logger.warning(
                "Mechanical gate: %s 20dma data missing — failing open "
                "(fail_open_on_missing=True)", symbol,
            )
        elif current_price <= ma20:
            return False, "mechanical_trend"

    return True, None


# The only regime condition assess_exposure's hardcoded logic actually
# implements. There is no config DSL — see the warning below.
_SUPPORTED_REGIME_CONDITION = "spy_above_20dma_and_no_drawdown_gate"


def assess_exposure(
    invested_pct: float,
    spy_above_20dma: bool,
    drawdown_gate_active: bool,
    cfg: dict,
) -> dict:
    """Exposure-management advisory target (Task 8).

    Code cannot force good buys, so the floor is advisory-but-loud: this
    verdict feeds a context section + analyst_guidance.md hard rule #10 that
    Claude must answer to, plus a `gate_decisions` telemetry row every
    session for auditability. The ceiling stays enforced by the existing
    position/cash/holdings/sector gates — `overinvested` here is
    informational, not a new block.

    Regime condition (`spy_above_20dma_and_no_drawdown_gate`): being below
    the target floor is only flagged `underinvested` when the regime is
    healthy (SPY above its 20-day MA AND the drawdown gate is clear). If
    either condition fails, low exposure is appropriate defensive behavior,
    not a shortfall — status is `defensive_ok`.

    `cfg["regime_condition"]` is currently a single hardcoded logic path
    (there is no config DSL to interpret alternate conditions). If the value
    is present and doesn't match the one supported string, that's a false
    affordance — editing strategy.json to something else would silently do
    nothing — so this logs a warning and proceeds with the hardcoded logic
    rather than pretending to honor an unsupported value.

    Returns {"status": "underinvested"|"in_range"|"overinvested"|"defensive_ok",
             "invested_pct": float, "target_min": float, "target_max": float}.
    """
    target_min = float(cfg.get("target_min_invested_pct", 60))
    target_max = float(cfg.get("target_max_invested_pct", 90))

    regime_condition = cfg.get("regime_condition")
    if regime_condition is not None and regime_condition != _SUPPORTED_REGIME_CONDITION:
        logger.warning(
            "assess_exposure: unsupported regime_condition %r, using %s",
            regime_condition, _SUPPORTED_REGIME_CONDITION,
        )

    regime_healthy = spy_above_20dma and not drawdown_gate_active

    if invested_pct > target_max:
        status = "overinvested"
    elif invested_pct < target_min:
        status = "underinvested" if regime_healthy else "defensive_ok"
    else:
        status = "in_range"

    return {
        "status": status,
        "invested_pct": invested_pct,
        "target_min": target_min,
        "target_max": target_max,
    }


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
        # gate_decisions.session_id has ondelete=CASCADE — detach rows first so
        # regenerating a session doesn't destroy the gate-attribution history.
        await db.execute(
            update(GateDecision)
            .where(GateDecision.session_id == existing.id)
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
    # Raw dict form, needed early to gate the mean-reversion screener fetch
    # below (both here and in Phase 0 prefetch.py) — see fetch_screeners()
    # in research.py for why this is a code-level gate, not prompt-only.
    strategy_json = load_strategy_json()

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
        detailed_news = cache.get("detailed_news") or {}
        av_technicals = cache["av_technicals"]
        technicals = cache["technicals"]
        analyst_consensus = cache["analyst_consensus"]
        research_symbols = cache["research_symbols"]
        screener_symbols = cache["screener_symbols"]
        # Re-gate on read, not just on Phase 0's write — see
        # gate_cached_mean_reversion() docstring for why a stale cache can't
        # be trusted to already reflect the current entry_style.
        mean_reversion_symbols = gate_cached_mean_reversion(cache, strategy_json)
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

        # Run momentum screener always; mean-reversion only when entry_style
        # includes it (fetch_screeners is the shared gate with Phase 0).
        screener_symbols, mean_reversion_symbols = await fetch_screeners(
            strategy_json, tracker=tracker
        )
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
    # strategy_json was already loaded above (needed earlier to gate the
    # mean-reversion screener fetch) — reused here, not re-read from disk.
    drawdown_config = strategy_json.get("drawdown_gate", {"enabled": True, "max_drawdown_pct": 8.0})
    drawdown_result = await update_peak_and_check(db, price_data, drawdown_config)
    drawdown_blocked = drawdown_result.blocked
    if drawdown_blocked:
        logger.warning(
            "Drawdown gate ACTIVE — buys will be filtered after Claude calls. "
            "Drawdown: %.1f%% (threshold: %.1f%%)",
            drawdown_result.current_drawdown_pct, drawdown_result.threshold_pct,
        )

    # ── Exposure management (Task 8: advisory target + telemetry) ──────────
    # Code cannot force good buys, so the floor is advisory-but-loud: a
    # context section + analyst_guidance.md hard rule #10 Claude must answer
    # to. The ceiling stays enforced by the existing position/cash/holdings/
    # sector gates — this never blocks a trade itself.
    exposure_cfg = strategy_json.get("exposure", {
        "target_min_invested_pct": 60,
        "target_max_invested_pct": 90,
        "regime_condition": "spy_above_20dma_and_no_drawdown_gate",
    })
    total_value = portfolio_dict["total_value"]
    invested_pct = (
        (total_value - portfolio_dict["cash_balance"]) / total_value * 100
        if total_value else 0.0
    )
    # SPY vs its 20-day MA is derived from the same Alpaca bars already
    # fetched for factor_returns (close vs mean of last 20 closes) — no new
    # external API call. See _fetch_factor_returns_sync in research.py.
    spy_above_20dma = bool((factor_returns.get("SPY") or {}).get("above_20dma", False))
    exposure_status = assess_exposure(invested_pct, spy_above_20dma, drawdown_blocked, exposure_cfg)
    logger.info(
        "Exposure check: %.1f%% invested (target %.0f-%.0f%%), spy_above_20dma=%s, "
        "drawdown_gate_active=%s -> %s",
        invested_pct, exposure_status["target_min"], exposure_status["target_max"],
        spy_above_20dma, drawdown_blocked, exposure_status["status"],
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

    # Portfolio VaR/CVaR — informational sizing context for Claude (no hard
    # rule, no numeric gate). Prefer the Phase 0 cache; if missing or stale,
    # compute inline. Failure is non-fatal — Phase 1 should still produce
    # recommendations even if VaR can't be computed.
    portfolio_risk: dict | None = None
    cached_risk = cache.get("portfolio_risk") if cache else None
    if cached_risk:
        portfolio_risk = cached_risk
    else:
        try:
            from .risk import compute_portfolio_risk
            risk_result = await compute_portfolio_risk(db)
            portfolio_risk = {
                "var_pct": risk_result.var_pct,
                "cvar_pct": risk_result.cvar_pct,
                "var_dollars": risk_result.var_dollars,
                "cvar_dollars": risk_result.cvar_dollars,
                "confidence": risk_result.confidence,
                "lookback_days": risk_result.lookback_days,
                "n_positions": risk_result.n_positions,
                "portfolio_value": risk_result.portfolio_value,
            }
        except Exception:  # noqa: BLE001 — VaR is best-effort context
            logger.warning(
                "Failed to compute portfolio VaR — context will omit it",
                exc_info=True,
            )

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
        portfolio_risk=portfolio_risk,
        failed_exits=failed_exits,
        mean_reversion_symbols=mean_reversion_symbols,
        exposure_status=exposure_status,
    )

    # Persist session row early so we have an ID for token_usage FK
    session_row = RecommendationSession(
        session_date=session_date,
        raw_research=f"{market_context}\n\n{research_context}",
    )
    db.add(session_row)
    await db.flush()

    # Exposure-check telemetry row — every session, regardless of verdict, so
    # operators can audit "underinvested while regime healthy" days without
    # parsing logs. use_caller_session=True: session_id references session_row,
    # which is flushed but not yet committed in this transaction.
    await record_gate_decision(
        db,
        use_caller_session=True,
        session_id=session_row.id,
        symbol="PORTFOLIO",
        action="none",
        phase=PHASE_FILTER,
        gate="exposure_check",
        passed=exposure_status["status"] in ("in_range", "defensive_ok"),
        reason=exposure_status["status"],
        details={
            "invested_pct": round(invested_pct, 2),
            "target_min": exposure_status["target_min"],
            "target_max": exposure_status["target_max"],
            "spy_above_20dma": spy_above_20dma,
            "drawdown_gate_active": drawdown_blocked,
        },
    )

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
        max_position_pct=strategy_conc.get("max_position_pct", DEFAULT_MAX_POSITION_PCT),
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
    # No hard cap on rec count. The previous "max 3" truncation discarded a
    # rotation BUY on a multi-exit day (2026-06-15: the US-Iran ceasefire forced
    # 3 oil-thesis sells (CVX/HAL/GE) and the cap dropped the one risk-on buy,
    # CCL — the bot sold into a +1.5% SPY rally and couldn't participate).
    # Exits are risk management and must never be dropped; new buys are already
    # bounded downstream by the holdings cap, cash floor, sector cap, and
    # position cap. Keep only a soft observability warning.
    all_recs = parsed.get("recommendations", [])
    raw_recs = all_recs
    if len(all_recs) > 8:
        logger.warning(
            "Claude returned %d recs in one session — unusually high, review for over-trading: %s",
            len(all_recs),
            [(r.get("action"), r.get("symbol")) for r in all_recs],
        )

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
            drawdown_reason = (
                f"portfolio drawdown {drawdown_result.current_drawdown_pct:.1f}% "
                f"exceeds threshold {drawdown_result.threshold_pct:.1f}%"
            )
            for r in raw_recs:
                if r.get("action", "").lower() != "buy":
                    continue
                await record_gate_decision(
                    db,
                    use_caller_session=True,
                    session_id=session_row.id,
                    symbol=r.get("symbol", ""),
                    action="buy",
                    phase=PHASE_FILTER,
                    gate="drawdown",
                    passed=False,
                    reason=drawdown_reason,
                    details={
                        "current_drawdown_pct": drawdown_result.current_drawdown_pct,
                        "threshold_pct": drawdown_result.threshold_pct,
                    },
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
            for r in raw_recs:
                if r.get("action", "").lower() != "buy":
                    continue
                await record_gate_decision(
                    db,
                    use_caller_session=True,
                    session_id=session_row.id,
                    symbol=r.get("symbol", ""),
                    action="buy",
                    phase=PHASE_RISK_REVIEW,
                    gate="risk_review",
                    passed=False,
                    reason="parse_failure: fail-closed reject",
                )
            raw_recs = [r for r in raw_recs if r.get("action", "").lower() != "buy"]
        else:
            decisions_by_symbol: dict[str, dict] = {
                (d.get("symbol") or "").upper(): d
                for d in risk_decisions
                if d.get("action", "").lower() == "buy"
            }
            # Persist a verdict row for every buy reviewed — approve OR reject —
            # so the attribution endpoint can show the Call-3 funnel ratio.
            # A buy with NO matching verdict (symbol typo, partial Call-3 output)
            # is rejected, matching the committee's declared default-REJECT
            # stance — fail-closed, never fail-open.
            rejected_symbols: set[str] = set()
            for r in raw_recs:
                if r.get("action", "").lower() != "buy":
                    continue
                sym = (r.get("symbol") or "").upper()
                d = decisions_by_symbol.get(sym)
                if d is None:
                    verdict = "reject"
                    reason = "no matching risk-review verdict returned (fail-closed reject)"
                    logger.warning(
                        "Risk review returned no verdict for %s — rejecting (fail-closed)", sym
                    )
                else:
                    verdict = d.get("verdict") or "reject"
                    reason = d.get("reason")
                if verdict == "reject":
                    rejected_symbols.add(sym)
                await record_gate_decision(
                    db,
                    use_caller_session=True,
                    session_id=session_row.id,
                    symbol=sym,
                    action="buy",
                    phase=PHASE_RISK_REVIEW,
                    gate="risk_review",
                    passed=(verdict != "reject"),
                    reason=reason if verdict == "reject" else None,
                    details={"verdict": verdict},
                )
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
            await record_gate_decision(
                db,
                use_caller_session=True,
                session_id=session_row.id,
                symbol=symbol,
                action="buy",
                phase=PHASE_FILTER,
                gate="cash_floor",
                passed=cash_check.passed,
                reason=cash_check.reason if not cash_check.passed else None,
                details={
                    "running_cash": running_cash,
                    "buy_notional": estimated_cost,
                    "projected_cash": cash_check.projected_cash,
                    "floor": cash_check.floor,
                    "reserve_pct": float(reserve_pct),
                },
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
                max_position_pct=Decimal(str(strategy_conc.get("max_position_pct", DEFAULT_MAX_POSITION_PCT))),
            )
            await record_gate_decision(
                db,
                use_caller_session=True,
                session_id=session_row.id,
                symbol=symbol,
                action="buy",
                phase=PHASE_FILTER,
                gate="position_cap",
                passed=position_check.passed,
                reason=position_check.reason if not position_check.passed else None,
                details={
                    "existing_value": existing_value,
                    "buy_notional": estimated_cost,
                    "total_portfolio_value": total_value_for_floor,
                    "projected_pct": position_check.projected_pct,
                    "cap_pct": position_check.cap_pct,
                },
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
            await record_gate_decision(
                db,
                use_caller_session=True,
                session_id=session_row.id,
                symbol=symbol,
                action="buy",
                phase=PHASE_FILTER,
                gate="holdings_cap",
                passed=holdings_check.passed,
                reason=holdings_check.reason if not holdings_check.passed else None,
                details={
                    "projected_count": holdings_check.projected_count,
                    "cap": holdings_check.cap,
                    "held_symbols_count": len(held_symbol_set),
                    "accepted_new_count": len(accepted_new_symbols),
                },
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
            sector_passed = check_sector_exposure(
                symbol,
                symbol_sector,
                estimated_cost,
                held_positions_for_sector,
                total_value_decimal,
                max_sector_pct,
            )
            sector_label = symbol_sector or "unknown sector"
            await record_gate_decision(
                db,
                use_caller_session=True,
                session_id=session_row.id,
                symbol=symbol,
                action="buy",
                phase=PHASE_FILTER,
                gate="sector_cap",
                passed=sector_passed,
                reason=(
                    f"would breach {max_sector_pct:.0f}% {sector_label} cap"
                    if not sector_passed else None
                ),
                details={
                    "sector": symbol_sector,
                    "buy_notional": estimated_cost,
                    "total_portfolio_value": total_value_decimal,
                    "max_sector_pct": float(max_sector_pct),
                },
            )
            if not sector_passed:
                logger.warning(
                    "Skipping %s buy — sector concentration gate rejected (%s, cap=%.0f%%)",
                    symbol, sector_label, max_sector_pct,
                )
                await send_telegram(
                    f"TRADEBOT // Sector gate: {symbol} BUY skipped — "
                    f"would breach {max_sector_pct:.0f}% {sector_label} cap"
                )
                continue

            # Factor-alignment gate (experiment B-momentum-discipline) —
            # code-enforced hard rule #9. In a momentum-led regime, reject buys
            # with negative own-momentum: the falling-knife entries that made up
            # the energy/commodity loss bucket. Prompt rule #9 was advisory and
            # Claude overrode it repeatedly; this enforces it.
            factor_cfg = strategy_json.get("factor_gate", {})
            symbol_price_row = price_data.get(symbol)
            candidate_20d = resolve_candidate_20d_momentum(symbol, symbol_price_row)
            factor_passed, factor_reason = check_factor_alignment(
                candidate_20d, factor_returns, factor_cfg
            )
            # momentum_source lets a later audit distinguish a real 20d-return
            # block from one that fell back to the coarser calendar-month proxy
            # (or had neither, in the fail-closed factor_data_missing case).
            if symbol_price_row and symbol_price_row.get("trailing_20d_return_pct") is not None:
                momentum_source = "trailing_20d"
            elif symbol_price_row and symbol_price_row.get("month_change_pct") is not None:
                momentum_source = "month_change"
            else:
                momentum_source = None
            await record_gate_decision(
                db,
                use_caller_session=True,
                session_id=session_row.id,
                symbol=symbol,
                action="buy",
                phase=PHASE_FILTER,
                gate="factor_alignment",
                passed=factor_passed,
                reason=factor_reason,
                details={
                    "candidate_20d_return": candidate_20d,
                    "momentum_source": momentum_source,
                    "min_factor_lead_pts": factor_cfg.get("min_factor_lead_pts"),
                    "min_candidate_mom_pct": factor_cfg.get("min_candidate_mom_pct"),
                },
            )
            if not factor_passed:
                logger.warning("Skipping %s buy — factor gate: %s", symbol, factor_reason)
                await send_telegram(
                    f"TRADEBOT // Factor gate: {symbol} BUY skipped — {factor_reason}"
                )
                continue

            # Re-entry cooldown gate (Task 9) — block a whipsaw re-buy of a
            # symbol we sold within the last N NYSE trading days. Motivated by
            # real losses (e.g. CVX sold 6/11, re-bought 6/15) that Experiment
            # B deliberately deferred fixing.
            cooldown_days = strategy_json.get("reentry_cooldown_days", 3)
            cooldown_passed, cooldown_reason = await check_reentry_cooldown(
                db, symbol, cooldown_days
            )
            await record_gate_decision(
                db,
                use_caller_session=True,
                session_id=session_row.id,
                symbol=symbol,
                action="buy",
                phase=PHASE_FILTER,
                gate="reentry_cooldown",
                passed=cooldown_passed,
                # Not conditioned on `not cooldown_passed` like the sibling
                # gates above — check_reentry_cooldown returns a non-None
                # reason even on a passing (fail-open) DB-error verdict, and
                # that diagnostic is exactly what an operator needs to see.
                reason=cooldown_reason,
                details={"cooldown_days": cooldown_days},
            )
            if not cooldown_passed:
                logger.warning(
                    "Skipping %s buy — reentry cooldown: %s", symbol, cooldown_reason,
                )
                await send_telegram(
                    f"TRADEBOT // Re-entry cooldown: {symbol} BUY skipped — {cooldown_reason}"
                )
                continue

            # Mechanical entry gate (Task 10) — code-enforced hard rule #12.
            # Backtest evidence (6/18): LLM-originated entries lose to a
            # mechanical breakout rule. Every BUY must clear 5-day momentum,
            # relative volume, and 20dma minimums in code before Claude's
            # pick can go through. Last in the filter chain per the task
            # brief (cash_floor -> position_cap -> holdings_cap -> sector_cap
            # -> factor_alignment -> reentry_cooldown -> mechanical_entry).
            mech_cfg = strategy_json.get("mechanical_entry", {})
            mech_row = price_data.get(symbol) or {}
            mech_tech = technicals.get(symbol) or {}
            mech_volume = mech_tech.get("volume") or {}
            mech_bollinger = mech_tech.get("bollinger") or {}
            mech_passed, mech_reason = check_mechanical_entry(
                symbol, price_data, technicals, mech_cfg
            )
            await record_gate_decision(
                db,
                use_caller_session=True,
                session_id=session_row.id,
                symbol=symbol,
                action="buy",
                phase=PHASE_FILTER,
                gate="mechanical_entry",
                passed=mech_passed,
                reason=mech_reason,
                details={
                    "week_change_pct": mech_row.get("week_change_pct"),
                    "relative_volume": mech_volume.get("relative_volume"),
                    "current_price": mech_row.get("current_price"),
                    "ma20": mech_bollinger.get("middle"),
                    "min_momentum_5d_pct": mech_cfg.get("min_momentum_5d_pct", 0.0),
                    "min_rel_volume": mech_cfg.get("min_rel_volume", 1.0),
                    "require_above_20dma": mech_cfg.get("require_above_20dma", True),
                },
            )
            if not mech_passed:
                logger.warning(
                    "Skipping %s buy — mechanical entry gate: %s", symbol, mech_reason,
                )
                await send_telegram(
                    f"TRADEBOT // Mechanical entry gate: {symbol} BUY skipped — {mech_reason}"
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
        # Detach gate rows before the delete — the CASCADE would otherwise wipe
        # the attribution data on exactly the all-blocked days it matters most.
        await db.execute(
            update(GateDecision).where(GateDecision.session_id == session_row.id).values(session_id=None)
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
            status=row.status,
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
