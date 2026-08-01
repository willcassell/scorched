#!/usr/bin/env python3
"""Evaluate the active experiment's kill criterion.

Run INSIDE the container (it needs the live DB, Alpaca, and Telegram env):
    docker compose exec -T tradebot python3 scripts/evaluate_experiment.py

Config-driven (generalized from `evaluate_experiment_b.py` when Experiment B
was reset as Experiment C, 2026-08-01): name, start date, deadline, and
baselines all come from strategy.json's `experiment` block instead of
hardcoded constants, so resetting the test for a future experiment is a
strategy.json edit, not a script edit.

Kill criterion (pre-committed at experiment start — see the matching
`.handovers/<date>-experiment-<letter>.md` file):
  RETIRE unless BOTH hold over the experiment window:
    1. portfolio window return > SPY window return, AND
    2. window profit factor > 1.0
  Window = trades/marks since `experiment.start_date`. No extension past
  `experiment.deadline_approx_date`.

This script only MEASURES and reports (Telegram + stdout). It does not change
strategy.json or stop any cron — retiring an experiment is a deliberate human
act (turn off the crons per `experiment.fallback`).
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, "src")


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


_REQUIRED_EXPERIMENT_FIELDS = (
    "start_date", "deadline_approx_date",
    "baseline_portfolio_value", "baseline_spy",
)


def missing_experiment_fields(experiment: dict) -> list[str]:
    """Return required `experiment` block keys that are absent.

    Pure and testable without a live strategy.json — main() uses this to fail
    with a clear config error instead of a bare KeyError traceback when a
    future experiment reset forgets to set a baseline.
    """
    return [k for k in _REQUIRED_EXPERIMENT_FIELDS if k not in experiment]


def compute_verdict(
    *,
    name: str,
    start_date: date,
    deadline_date: date,
    baseline_portfolio: Decimal,
    baseline_spy: Decimal,
    current_portfolio: Decimal,
    current_spy: Decimal | None,
    pf: Decimal,
    n_sells: int,
    wins: Decimal,
    losses: Decimal,
    today: date,
) -> dict:
    """Pure verdict computation — no I/O, fully unit-testable.

    Returns a dict with the computed returns, the KEEP/RETIRE verdict, and a
    formatted message ready to print/send. Kept separate from main() so the
    decision logic can be tested without a live DB or Alpaca connection.
    """
    port_ret = (current_portfolio - baseline_portfolio) / baseline_portfolio * 100
    spy_ret = (
        (current_spy - baseline_spy) / baseline_spy * 100
        if current_spy is not None
        else None
    )

    beats_spy = spy_ret is not None and port_ret > spy_ret
    pf_ok = pf > 1
    keep = beats_spy and pf_ok
    verdict = "KEEP (criterion met)" if keep else "RETIRE"
    on_time = today >= deadline_date

    spy_line = (
        f"SPY:       {spy_ret:+.2f}%  (${baseline_spy} → ${current_spy})"
        if spy_ret is not None
        else "SPY:       UNAVAILABLE (Alpaca snapshot failed)"
    )

    msg = (
        f"TRADEBOT // EXPERIMENT {name} VERDICT — {verdict}\n"
        f"Eval date: {today}  (deadline ~{deadline_date}"
        f"{'' if on_time else ' — EARLY, informational'})\n"
        f"\n"
        f"Window since {start_date}:\n"
        f"  Portfolio: {port_ret:+.2f}%  (${baseline_portfolio} → ${current_portfolio:.2f})\n"
        f"  {spy_line}\n"
        f"  Beats SPY: {'YES' if beats_spy else 'NO'}\n"
        f"\n"
        f"  Profit factor: {pf:.2f}  ({n_sells} sells, wins ${wins:.0f} / losses ${losses:.0f})\n"
        f"  PF > 1.0:  {'YES' if pf_ok else 'NO'}\n"
        f"\n"
        f"DECISION: {verdict}\n"
    )
    if on_time and not keep:
        msg += (
            "\nACTION: criterion failed at the deadline. Retire per the pre-commit —\n"
            "turn the crons off. No extension.\n"
        )

    return {
        "name": name,
        "port_ret": port_ret,
        "spy_ret": spy_ret,
        "beats_spy": beats_spy,
        "pf_ok": pf_ok,
        "keep": keep,
        "verdict": verdict,
        "on_time": on_time,
        "message": msg,
    }


async def _portfolio_total_value(db) -> Decimal:
    from sqlalchemy import select
    from scorched.models import Portfolio, Position
    from scorched.services.alpaca_data import fetch_snapshots_sync

    portfolio = (await db.execute(select(Portfolio))).scalars().first()
    positions = list((await db.execute(select(Position))).scalars().all())
    cash = Decimal(str(portfolio.cash_balance))
    if not positions:
        return cash
    snaps = await asyncio.to_thread(
        fetch_snapshots_sync, [p.symbol for p in positions]
    )
    total = cash
    for p in positions:
        px = (snaps.get(p.symbol) or {}).get("current_price")
        price = Decimal(str(px)) if px else Decimal(str(p.avg_cost_basis))
        total += Decimal(str(p.shares)) * price
    return total


async def _window_profit_factor(db, start: date) -> tuple[Decimal, Decimal, Decimal, int]:
    """Profit factor over sells executed on/after `start`. Returns
    (profit_factor, gross_wins, gross_losses, n_sells)."""
    from sqlalchemy import select
    from scorched.models import TradeHistory

    rows = (await db.execute(
        select(TradeHistory).where(TradeHistory.action == "sell")
    )).scalars().all()
    wins = Decimal("0")
    losses = Decimal("0")
    n = 0
    for r in rows:
        executed = r.executed_at.date() if r.executed_at else None
        if executed is None or executed < start:
            continue
        g = Decimal(str(r.realized_gain or 0))
        n += 1
        if g > 0:
            wins += g
        elif g < 0:
            losses += -g
    pf = (wins / losses) if losses > 0 else (Decimal("999") if wins > 0 else Decimal("0"))
    return pf, wins, losses, n


def _spy_now() -> Decimal | None:
    from scorched.services.alpaca_data import fetch_snapshots_sync
    px = (fetch_snapshots_sync(["SPY"]).get("SPY") or {}).get("current_price")
    return Decimal(str(px)) if px else None


async def main() -> int:
    from scorched.database import AsyncSessionLocal
    from scorched.services.strategy import load_strategy_json
    from scorched.services.telegram import send_telegram
    from scorched.tz import market_today

    experiment = load_strategy_json().get("experiment") or {}
    if not experiment:
        print("No `experiment` block in strategy.json — nothing to evaluate.")
        return 1

    missing = missing_experiment_fields(experiment)
    if missing:
        print(
            f"strategy.json `experiment` block is missing required field(s): "
            f"{', '.join(missing)}. Nothing was measured — this is a config "
            f"problem, not a KEEP/RETIRE result. Fix strategy.json and re-run."
        )
        return 1

    name = experiment.get("name", "unknown")
    start_date = _parse_date(experiment["start_date"])
    deadline_date = _parse_date(experiment["deadline_approx_date"])
    baseline_portfolio = Decimal(str(experiment["baseline_portfolio_value"]))
    baseline_spy = Decimal(str(experiment["baseline_spy"]))

    today = market_today()
    async with AsyncSessionLocal() as db:
        total_value = await _portfolio_total_value(db)
        pf, wins, losses, n_sells = await _window_profit_factor(db, start_date)

    spy_now = await asyncio.to_thread(_spy_now)

    result = compute_verdict(
        name=name,
        start_date=start_date,
        deadline_date=deadline_date,
        baseline_portfolio=baseline_portfolio,
        baseline_spy=baseline_spy,
        current_portfolio=total_value,
        current_spy=spy_now,
        pf=pf,
        n_sells=n_sells,
        wins=wins,
        losses=losses,
        today=today,
    )

    print(result["message"])
    try:
        await send_telegram(result["message"])
    except Exception as e:  # noqa: BLE001
        print(f"(Telegram send failed: {e})")
    return 0 if result["keep"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
