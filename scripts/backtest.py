"""CLI: stress-test exit policies or screener parameters against historical data.

Modes
-----
  replay  — pull the bot's actual buy entries from TradeHistory, fetch Alpaca
            bars covering each entry's forward window, apply alternate exit
            policies, and report comparable metrics. Use this to answer:
            "Would tighter/looser stops have helped trades we already took?"

  sim     — run the parameterized breakout simulator over a symbol list and
            date range. Use this to validate screener/entry-rule changes
            (e.g., MODERATE_VOLUME tier at 1.0× vs 1.5× volume multiplier)
            before shipping to production.

Examples
--------
  # How would -6% stops have done on our actual trades?
  python scripts/backtest.py replay --stop-pct 0.06 --target-pct 0.15

  # Compare 1.5× vs 1.0× volume multiplier on the screener
  python scripts/backtest.py sim --symbols AAPL,MSFT,NVDA,GOOG --vol-mult 1.5
  python scripts/backtest.py sim --symbols AAPL,MSFT,NVDA,GOOG --vol-mult 1.0

This is a research tool — it does not write to the live database. Run it
locally or in the container; results print to stdout.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta

# Allow `python scripts/backtest.py ...` from the repo root
sys.path.insert(0, "src")

from scorched.services.backtest import (  # noqa: E402
    BacktestMetrics,
    SimTrade,
    compute_metrics,
    replay_with_alternate_exits,
    simulate_breakout_strategy,
)


def _print_metrics(label: str, m: BacktestMetrics, trades: list[SimTrade]) -> None:
    print(f"\n=== {label} ===")
    print(f"  trades:           {m.n_trades}")
    if m.n_trades == 0:
        print("  (no qualifying trades)")
        return
    print(f"  win rate:         {m.win_rate:.1f}%")
    print(f"  avg win / loss:   {m.avg_win_pct:+.2f}% / {m.avg_loss_pct:+.2f}%")
    print(f"  expectancy:       {m.expectancy_pct:+.2f}% per trade")
    pf = f"{m.profit_factor:.2f}" if m.profit_factor is not None else "n/a"
    print(f"  profit factor:    {pf}")
    print(f"  total return:     {m.total_return_pct:+.1f}% (compounded across {m.n_trades} trades)")
    print(f"  max drawdown:     {m.max_drawdown_pct:.1f}%")
    sh = f"{m.sharpe:.2f}" if m.sharpe is not None else "n/a"
    print(f"  sharpe (annlz):   {sh}")
    print(f"  avg hold:         {m.avg_hold_days:.1f} days")

    # Exit-reason breakdown — useful for diagnosing which policy is firing
    reasons = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    if reasons:
        print(f"  exit breakdown:   {reasons}")


async def _load_buy_entries() -> list[dict]:
    """Pull buy trades from the live DB."""
    from scorched.database import async_session
    from scorched.models import TradeHistory
    from sqlalchemy import select

    async with async_session() as db:
        rows = (
            await db.execute(
                select(TradeHistory).where(TradeHistory.action == "buy").order_by(TradeHistory.executed_at)
            )
        ).scalars().all()

    return [
        {
            "symbol": r.symbol,
            "entry_date": r.executed_at.date(),
            "entry_price": float(r.execution_price),
            "shares": float(r.shares),
        }
        for r in rows
    ]


def _fetch_bars(symbols: list[str], days: int) -> dict[str, list[dict]]:
    from scorched.services.alpaca_data import fetch_bars_sync
    return fetch_bars_sync(symbols, days=days)


def cmd_replay(args: argparse.Namespace) -> int:
    print(f"Loading buy entries from TradeHistory...")
    entries = asyncio.run(_load_buy_entries())
    print(f"  found {len(entries)} buy trades")
    if not entries:
        return 0
    syms = sorted({e["symbol"] for e in entries})
    days_back = (date.today() - min(e["entry_date"] for e in entries)).days + 60
    print(f"Fetching {days_back}d of bars for {len(syms)} symbols...")
    bars = _fetch_bars(syms, days=days_back)
    print(f"  got bars for {sum(1 for v in bars.values() if v)}/{len(syms)} symbols")

    trades = replay_with_alternate_exits(
        entries, bars,
        stop_pct=args.stop_pct,
        target_pct=args.target_pct,
        time_stop_days=args.time_stop_days,
    )
    metrics = compute_metrics(trades)
    label = f"REPLAY  stop={args.stop_pct}  target={args.target_pct}  time={args.time_stop_days}d"
    _print_metrics(label, metrics, trades)
    if args.json:
        print(json.dumps({"metrics": asdict(metrics), "n_trades": metrics.n_trades}, indent=2))
    return 0


def cmd_sim(args: argparse.Namespace) -> int:
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    print(f"Fetching {args.lookback_days}d of bars for {len(syms)} symbols...")
    bars = _fetch_bars(syms, days=args.lookback_days)
    print(f"  got bars for {sum(1 for v in bars.values() if v)}/{len(syms)} symbols")

    trades = simulate_breakout_strategy(
        bars,
        momentum_5d_min=args.momentum_5d,
        volume_multiplier=args.vol_mult,
        rsi_min=args.rsi_min,
        rsi_max=args.rsi_max,
        stop_pct=args.stop_pct,
        target_pct=args.target_pct,
        time_stop_days=args.time_stop_days,
        max_concurrent=args.max_concurrent,
    )
    metrics = compute_metrics(trades)
    label = (
        f"SIM  mom5d={args.momentum_5d}  vol×={args.vol_mult}  "
        f"RSI[{args.rsi_min}–{args.rsi_max}]  stop={args.stop_pct}  "
        f"target={args.target_pct}  time={args.time_stop_days}d"
    )
    _print_metrics(label, metrics, trades)
    if args.json:
        print(json.dumps({"metrics": asdict(metrics), "n_trades": metrics.n_trades}, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("replay", help="Re-exit actual TradeHistory buys with alternate rules")
    rp.add_argument("--stop-pct", type=float, default=0.08)
    rp.add_argument("--target-pct", type=float, default=0.15)
    rp.add_argument("--time-stop-days", type=int, default=30)
    rp.add_argument("--json", action="store_true", help="Emit machine-readable metrics block")
    rp.set_defaults(func=cmd_replay)

    sp = sub.add_parser("sim", help="Simulate parameterized breakout strategy over historical bars")
    sp.add_argument("--symbols", type=str, required=True, help="Comma-separated tickers")
    sp.add_argument("--lookback-days", type=int, default=365)
    sp.add_argument("--momentum-5d", type=float, default=0.03)
    sp.add_argument("--vol-mult", type=float, default=1.5)
    sp.add_argument("--rsi-min", type=float, default=50.0)
    sp.add_argument("--rsi-max", type=float, default=75.0)
    sp.add_argument("--stop-pct", type=float, default=0.08)
    sp.add_argument("--target-pct", type=float, default=0.15)
    sp.add_argument("--time-stop-days", type=int, default=30)
    sp.add_argument("--max-concurrent", type=int, default=10)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_sim)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
