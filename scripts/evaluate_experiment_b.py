#!/usr/bin/env python3
"""Evaluate the Experiment B (momentum-discipline) kill criterion.

Run INSIDE the container (it needs the live DB, Alpaca, and Telegram env):
    docker compose exec -T tradebot python3 scripts/evaluate_experiment_b.py

Kill criterion (pre-committed 2026-06-18, see .handovers/2026-06-18-experiment-B.md):
  RETIRE unless BOTH hold over the experiment window:
    1. portfolio window return > SPY window return, AND
    2. window profit factor > 1.0
  Window = trades/marks since the baseline below. No extension past the deadline.

This script only MEASURES and reports (Telegram + stdout). It does not change
strategy.json or stop any cron — flipping to Option A is a deliberate human act.
"""
import asyncio
import sys
from datetime import date
from decimal import Decimal

sys.path.insert(0, "src")

# ── Pre-committed baseline — do NOT edit after the experiment started ──────────
START = date(2026, 6, 18)
DEADLINE_APPROX = date(2026, 9, 12)
PORTFOLIO_BASELINE = Decimal("99850.63")
SPY_BASELINE = Decimal("748.46")


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


async def _window_profit_factor(db) -> tuple[Decimal, Decimal, Decimal, int]:
    """Profit factor over sells executed on/after START. Returns
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
        if executed is None or executed < START:
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
    from scorched.services.telegram import send_telegram
    from scorched.tz import market_today

    today = market_today()
    async with AsyncSessionLocal() as db:
        total_value = await _portfolio_total_value(db)
        pf, wins, losses, n_sells = await _window_profit_factor(db)

    spy_now = await asyncio.to_thread(_spy_now)

    port_ret = (total_value - PORTFOLIO_BASELINE) / PORTFOLIO_BASELINE * 100
    spy_ret = ((spy_now - SPY_BASELINE) / SPY_BASELINE * 100) if spy_now else None

    beats_spy = spy_ret is not None and port_ret > spy_ret
    pf_ok = pf > 1
    keep = beats_spy and pf_ok
    verdict = "KEEP (criterion met)" if keep else "RETIRE (Option A)"

    spy_line = (
        f"SPY:       {spy_ret:+.2f}%  (${SPY_BASELINE} → ${spy_now})"
        if spy_ret is not None else "SPY:       UNAVAILABLE (Alpaca snapshot failed)"
    )
    on_time = today >= DEADLINE_APPROX

    msg = (
        f"TRADEBOT // EXPERIMENT B VERDICT — {verdict}\n"
        f"Eval date: {today}  (deadline ~{DEADLINE_APPROX}{'' if on_time else ' — EARLY, informational'})\n"
        f"\n"
        f"Window since {START}:\n"
        f"  Portfolio: {port_ret:+.2f}%  (${PORTFOLIO_BASELINE} → ${total_value:.2f})\n"
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
            "turn the crons off (Option A). No extension.\n"
        )
    print(msg)
    try:
        await send_telegram(msg)
    except Exception as e:  # noqa: BLE001
        print(f"(Telegram send failed: {e})")
    return 0 if keep else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
