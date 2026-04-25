"""Strategy loader — reads strategy.json and converts selections to prose for Claude."""
import json
import logging
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)

# ── Option labels ──────────────────────────────────────────────────────────────
# Each value is a directive sentence Claude can act on directly.

_OBJECTIVE_LABELS = {
    "growth":   "Maximize portfolio growth — accept higher risk and volatility in pursuit of upside",
    "balanced": "Balance growth and capital preservation — do not sacrifice safety for marginal gains",
    "income":   "Preserve capital first; growth is secondary — avoid speculative positions",
    "learning": "Prioritize learning — log full reasoning for every trade and track outcomes over time",
}

_REC_STYLE_LABELS = {
    "aggressive": "Always find 2–3 recommendations per day; stay fully invested and active at all times",
    "selective":  "Only recommend trades when conviction is genuinely high; do not manufacture setups",
    "adaptive":   "Calibrate recommendation frequency to market conditions — more active in strong markets, fewer in weak or uncertain ones",
    "minimal":    "Default to cash; only act on exceptional, high-conviction setups — missing a trade is acceptable",
}

_NO_TRADE_LABELS = {
    "always_trade":  "Always recommend something — do not leave the day without a trade idea",
    "high_bar":      "Require a clear, high-conviction setup before recommending any trade; no-trade is a valid outcome",
    "adaptive":      "Weigh market conditions and individual setup quality together — choose no-trade when neither is favorable",
    "cash_default":  "Default answer is no-trade; only override when the setup is exceptional — a missed opportunity is better than a forced mistake",
}

_HOLD_LABELS = {
    "intraday": "Intraday only — exit all positions before market close the same day",
    "1-3d":     "1–3 calendar days — very short-term; exit by end of the third trading day",
    "3-10d":    "3–10 trading days — short-term swing; most positions should be closed within two calendar weeks",
    "2-6wk":    "2–6 weeks — medium-term; allow time for a thesis to develop but do not hold indefinitely",
    "3mo+":     "3 months or longer — long-term holds; prioritize fundamentals and thesis durability over short-term noise",
}

_ENTRY_LABELS = {
    "momentum_cont":     "Momentum continuation — buy stocks already moving up strongly on high volume with a named catalyst",
    "pullback_uptrend":  "Pullback in uptrend — buy temporary dips in stocks that have a strong established trend",
    "breakout":          "Breakout — buy when price moves decisively above a key resistance level with above-average volume",
    "relative_strength": "Relative strength — favor stocks visibly outperforming their sector or SPY over the trailing 5–20 days",
    "value_catalyst":    "Value with catalyst — buy undervalued stocks only when there is a specific near-term re-rating event",
    "mean_reversion":    "Mean reversion — buy oversold stocks with a high historical tendency to snap back toward their average",
}

_SELL_LABELS = {
    "full_target":   "Sell the entire position when the profit target is hit — do not hold for additional upside",
    "scale_out":     "Sell half the position at the first profit target; hold the remainder until either the next target or a trailing stop",
    "trailing_stop": "Let winners run — do not use a fixed profit target; exit when price pulls back a set percentage from its high",
    "time_based":    "If the position has not reached the profit target within the target holding period, exit regardless of thesis",
}

_LOSS_LABELS = {
    "hard_stop":          "Exit the full position immediately when the stop-loss percentage is hit — no exceptions",
    "time_price_hybrid":  "Exit when either the stop-loss percentage is hit OR the position is flat/down after the target holding period — whichever comes first",
    "thesis_based":       "Hold through losses as long as the original catalyst remains valid; exit only when the thesis is broken",
    "no_stop":            "No hard stop — manage exit timing manually based on day-to-day judgment",
}

_SIZING_LABELS = {
    "equal_weight":        "Allocate the same dollar amount to every position regardless of conviction level",
    "conviction_weighted": "Allocate more capital to high-conviction setups and less to speculative or lower-confidence trades",
    "volatility_scaled":   "Allocate less capital to highly volatile stocks so that the dollar risk per position stays roughly equal",
    "kelly":               "Size positions using a Kelly-influenced formula based on historical win rate and average payoff ratio for similar setups",
}

_ADD_ROTATE_LABELS = {
    "add_winners":  "Add to winning positions — when a position is up and the thesis is intact, allocate more capital to it",
    "rotate":       "Rotate capital — take partial profits from winners and redeploy into fresh setups",
    "adaptive":     "Decide case-by-case — add to a winner only if it has a clearly better expected value than available new setups",
    "never_add":    "Never increase a position after entry — keep all sizes fixed from the initial trade",
}

_PARTIAL_SELL_LABELS = {
    "never":         "Never take partial profits — hold the full position until the final exit signal is triggered",
    "at_target":     "Sell half the position when the first profit target is hit; hold the rest for further upside",
    "adaptive":      "Trim the position when the risk/reward ratio has deteriorated — do not need to hit a specific target",
    "always_ladder": "Sell in tranches on the way up — distribute exits at multiple price levels",
}

_GUARDRAIL_LABELS = {
    "max_portfolio_dd":    "If the portfolio is down more than 10% from its peak, pause all new buys until it recovers",
    "weak_regime_cash":    "When the market regime is weak or uncertain, move at least 50% of the portfolio to cash",
    "no_correlated":       "Never hold two positions that are highly correlated — they behave like one trade with double the risk",
    "earnings_blackout":   "During market-wide earnings season peaks, do not open any new positions for 5 trading days",
    "vix_pause":           "When the VIX spikes above 30, pause all new buys until volatility subsides",
}

_REGIME_LABELS = {
    "always_active": "Stay fully active regardless of market direction — always look for opportunities",
    "reduce_weak":   "Reduce position sizes in weak or mixed markets; trade at full size only in clear uptrends",
    "adaptive":      "Assess market regime daily — increase aggressiveness in strong markets, pull back in weak or uncertain ones",
    "cash_weak":     "Go mostly to cash in weak or uncertain market conditions; open new positions only in clear, confirmed uptrends",
}

_EVENT_LABELS = {
    "reduce_before_earnings":  "Reduce existing position size by at least half before a scheduled earnings announcement",
    "no_new_before_earnings":  "Do not open any new position if the company has an earnings announcement within 5 trading days",
    "exit_before_earnings":    "Exit all positions entirely before their earnings dates — binary event risk is unacceptable",
    "hold_through_earnings":   "Hold positions through earnings — the risk is already priced in; do not treat earnings as a special event",
    "no_weekend":              "Sell all positions before long weekends (3-day holidays) to avoid overnight gap risk",
}

_SECTOR_LABELS = {
    "no_preference":   "No sector preference — evaluate all sectors equally on setup quality",
    "technology":      "Favor Technology — software, semiconductors, and AI infrastructure",
    "healthcare":      "Favor Healthcare and Biotech — prioritize stocks with near-term catalysts",
    "financials":      "Favor Financials — banks, fintech, and insurance",
    "consumer":        "Favor Consumer Discretionary",
    "energy":          "Favor Energy",
    "industrials":     "Favor Industrials",
    "avoid_defensive": "Avoid defensive sectors (utilities, REITs, consumer staples) — they do not produce short-term momentum setups",
}

_EXPLANATION_LABELS = {
    "brief":    "Keep reasoning brief — one sentence naming the catalyst and the action; no elaboration",
    "standard": "Standard detail — 3–4 sentences covering the catalyst, core thesis, and key risks",
    "detailed": "Full detail — explain the catalyst, which entry criteria are met, the exit plan, and all material risks",
    "teach":    "Teaching mode — explain why this trade fits the declared strategy; connect each point to the user's stated goals to help them learn",
}

# ── Default strategy used when strategy.json is missing ───────────────────────

DEFAULT_JSON = {
    "objective": "balanced",
    "rec_style": "adaptive",
    "no_trade_threshold": "adaptive",
    "hold_period": "3-10d",
    "entry_style": ["momentum_cont", "pullback_uptrend"],
    "sell_discipline": "scale_out",
    "loss_management": "time_price_hybrid",
    "sizing_style": "conviction_weighted",
    "concentration": {
        "max_position_pct": 20,
        "max_sector_pct": 40,
        "max_holdings": 10,
    },
    "add_vs_rotate": "adaptive",
    "partial_sell": "adaptive",
    "risk_guardrails": ["max_portfolio_dd", "weak_regime_cash"],
    "market_regime": "adaptive",
    "event_risk": ["reduce_before_earnings", "no_new_before_earnings"],
    "sectors": ["no_preference"],
    "rec_explanation": "standard",
    "notes": "",
    "drawdown_gate": {
        "enabled": True,
        "max_drawdown_pct": 8.0,
    },
}


# Anchor for relative paths like "strategy.json" and "analyst_guidance.md".
#
# When imported from the source tree (tests, cron scripts that prepend src/ to
# sys.path), three levels up from this file lands on the repo root:
#   src/scorched/services/strategy.py  →  parents[3] = repo root
#
# Inside the Docker container, the package is pip-installed into site-packages,
# so parents[3] resolves to /usr/local/lib/python3.11/ — not useful. In that
# case, fall back to the Docker WORKDIR (/app) where strategy.json and
# analyst_guidance.md are volume-mounted by docker-compose.yml.
def _pick_repo_root() -> Path:
    candidate = Path(__file__).resolve().parents[3]
    if (candidate / "strategy.json").exists() or (candidate / "analyst_guidance.md").exists():
        return candidate
    docker_workdir = Path("/app")
    if (docker_workdir / "strategy.json").exists() or (docker_workdir / "analyst_guidance.md").exists():
        return docker_workdir
    return candidate


_REPO_ROOT = _pick_repo_root()


def _resolve_path() -> Path:
    path: Path = settings.strategy_file
    if not path.is_absolute():
        path = _REPO_ROOT / path
    return path


def load_strategy_json() -> dict:
    """Return the raw strategy dict (for the API / settings form)."""
    path = _resolve_path()
    if not path.exists():
        logger.warning("strategy.json not found at %s — using defaults", path)
        return DEFAULT_JSON.copy()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        logger.info("Loaded strategy from %s", path)
        return data
    except (OSError, json.JSONDecodeError) as e:
        logger.error("Failed to read %s: %s — using defaults", path, e)
        return DEFAULT_JSON.copy()


def save_strategy_json(data: dict) -> None:
    """Write strategy dict to strategy.json."""
    path = _resolve_path()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("Saved strategy to %s", path)


def load_analyst_guidance() -> str:
    """Return the analyst_guidance.md content for injection into Claude prompts."""
    path = _REPO_ROOT / "analyst_guidance.md"
    if not path.exists():
        logger.warning("analyst_guidance.md not found at %s — skipping guidance injection", path)
        return ""
    try:
        content = path.read_text(encoding="utf-8").strip()
        logger.info("Loaded analyst guidance from %s (%d chars)", path, len(content))
        return content
    except OSError as e:
        logger.error("Failed to read analyst_guidance.md: %s — skipping", e)
        return ""


def load_strategy() -> str:
    """Return the strategy as a clean prose string for injection into Claude prompts."""
    s = load_strategy_json()
    lines = ["## Declared Trading Strategy\n"]

    # ── Group 1: Core Personality ─────────────────────────────────────────────
    lines.append("### Core Personality\n")

    obj = s.get("objective", "balanced")
    lines.append(f"**Objective:** {_OBJECTIVE_LABELS.get(obj, obj)}")

    rec = s.get("rec_style", "adaptive")
    lines.append(f"**Recommendation style:** {_REC_STYLE_LABELS.get(rec, rec)}")

    no_trade = s.get("no_trade_threshold", "adaptive")
    lines.append(f"**No-trade threshold:** {_NO_TRADE_LABELS.get(no_trade, no_trade)}")

    expl = s.get("rec_explanation", "standard")
    lines.append(f"**Explanation style:** {_EXPLANATION_LABELS.get(expl, expl)}")

    # ── Group 2: Entry & Exit ──────────────────────────────────────────────────
    lines.append("\n### Entry & Exit\n")

    hold = s.get("hold_period", "3-10d")
    lines.append(f"**Target holding period:** {_HOLD_LABELS.get(hold, hold)}")

    entries = s.get("entry_style", [])
    if entries:
        lines.append("**Entry types to look for:**")
        for e in entries:
            lines.append(f"  - {_ENTRY_LABELS.get(e, e)}")

    sell = s.get("sell_discipline", "scale_out")
    lines.append(f"**How to exit winners:** {_SELL_LABELS.get(sell, sell)}")

    loss = s.get("loss_management", "time_price_hybrid")
    lines.append(f"**How to handle losers:** {_LOSS_LABELS.get(loss, loss)}")

    partial = s.get("partial_sell", "adaptive")
    lines.append(f"**Partial sell rule:** {_PARTIAL_SELL_LABELS.get(partial, partial)}")

    # ── Group 3: Portfolio Controls ────────────────────────────────────────────
    lines.append("\n### Portfolio Controls\n")

    sizing = s.get("sizing_style", "conviction_weighted")
    lines.append(f"**Position sizing approach:** {_SIZING_LABELS.get(sizing, sizing)}")

    conc = s.get("concentration", {})
    if conc:
        lines.append(
            f"**Concentration limits:** Never allocate more than "
            f"{conc.get('max_position_pct', 20)}% of the portfolio to a single position; "
            f"never allocate more than {conc.get('max_sector_pct', 40)}% to any one sector; "
            f"hold no more than {conc.get('max_holdings', 10)} positions simultaneously."
        )

    add_rot = s.get("add_vs_rotate", "adaptive")
    lines.append(f"**Adding vs rotating:** {_ADD_ROTATE_LABELS.get(add_rot, add_rot)}")

    guardrails = s.get("risk_guardrails", [])
    if guardrails:
        lines.append("**Hard risk guardrails (must not be violated):**")
        for g in guardrails:
            lines.append(f"  - {_GUARDRAIL_LABELS.get(g, g)}")

    # ── Group 4: Market & Context ──────────────────────────────────────────────
    lines.append("\n### Market & Context\n")

    regime = s.get("market_regime", "adaptive")
    lines.append(f"**Market regime behavior:** {_REGIME_LABELS.get(regime, regime)}")

    events = s.get("event_risk", [])
    if events:
        lines.append("**Earnings and event risk rules:**")
        for ev in events:
            lines.append(f"  - {_EVENT_LABELS.get(ev, ev)}")

    sectors = s.get("sectors", [])
    if sectors and sectors != ["no_preference"]:
        lines.append("**Sector guidance:**")
        for sec in sectors:
            lines.append(f"  - {_SECTOR_LABELS.get(sec, sec)}")
    else:
        lines.append(
            "**Sector guidance:** No sector preference — evaluate all sectors equally on setup quality."
        )

    notes = (s.get("notes") or "").strip()
    if notes:
        lines.append(f"\n**Additional instructions from the user:**\n{notes}")

    return "\n".join(lines)
