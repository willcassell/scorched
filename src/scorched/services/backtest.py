"""Backtester for strategy and prompt edits.

Two modes both produce a list of `SimTrade` objects, which feed the same
`compute_metrics()` so results are directly comparable.

  Mode A (replay):  take the bot's actual past entries from TradeHistory and
                    apply alternate exit policies (different stop %, time stop,
                    profit target). Answers questions like "would a -6% stop
                    have beaten -8% on the trades we actually took?".

  Mode B (sim):     simulate a parameterized entry rule (e.g. "5d momentum >
                    Z% on volume > V × avg") over historical Alpaca bars and
                    apply exit rules. Answers "does the new MODERATE_VOLUME
                    tier produce profitable trades on the last year of data?".

Limitations (read these before drawing conclusions):
  - No slippage or commission. Add them by adjusting entry/exit prices in the
    caller if needed (Alpaca is commission-free; slippage on liquid names runs
    ~0.05–0.20% — not material at swing horizons).
  - No look-ahead checks: the simulator only uses bars[<= signal_date] when
    deciding entry, but be careful when you write new entry rules.
  - No portfolio-level position sizing or correlation: each trade is sized to
    `position_pct` of starting capital, independently. Use this to compare
    *strategies*, not to project actual portfolio P&L.
  - Assumes daily bars (no intraday). Stop/target are hit on the day's high/low.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional

import numpy as np


@dataclass
class SimTrade:
    symbol: str
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    shares: float
    exit_reason: str  # "stop" | "target" | "time" | "end_of_data"

    @property
    def pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.shares

    @property
    def return_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (self.exit_price - self.entry_price) / self.entry_price

    @property
    def hold_days(self) -> int:
        return (self.exit_date - self.entry_date).days


@dataclass
class BacktestMetrics:
    n_trades: int
    win_rate: float          # % wins of closed trades
    profit_factor: Optional[float]  # gross wins / gross losses; None if no losses
    avg_win_pct: float
    avg_loss_pct: float
    expectancy_pct: float    # win% * avg_win + loss% * avg_loss
    total_return_pct: float  # cumulative return from compounding all trade returns
    max_drawdown_pct: float  # worst peak-to-trough on the trade-by-trade equity curve
    sharpe: Optional[float]  # annualized; None if too few trades or zero stdev
    avg_hold_days: float


def compute_metrics(trades: list[SimTrade]) -> BacktestMetrics:
    """Trade-list → standard performance metrics. No I/O."""
    if not trades:
        return BacktestMetrics(
            n_trades=0, win_rate=0.0, profit_factor=None,
            avg_win_pct=0.0, avg_loss_pct=0.0, expectancy_pct=0.0,
            total_return_pct=0.0, max_drawdown_pct=0.0,
            sharpe=None, avg_hold_days=0.0,
        )

    returns = np.array([t.return_pct for t in trades], dtype=float)
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    n = len(returns)

    win_rate = (len(wins) / n) * 100.0 if n else 0.0
    avg_win_pct = float(wins.mean()) * 100 if wins.size else 0.0
    avg_loss_pct = float(losses.mean()) * 100 if losses.size else 0.0
    expectancy_pct = (
        (len(wins) / n) * avg_win_pct + (len(losses) / n) * avg_loss_pct
        if n else 0.0
    )

    gross_wins = float(wins.sum())
    gross_losses = float(abs(losses.sum()))
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else None

    # Compound by treating each trade as a sequential ±return on equity-at-risk.
    # Real portfolios overlap trades; this is a conservative single-thread compounding.
    equity = np.cumprod(1.0 + returns)
    total_return_pct = float((equity[-1] - 1.0) * 100)
    peak = np.maximum.accumulate(equity)
    drawdowns = (equity - peak) / peak
    max_drawdown_pct = float(drawdowns.min() * 100) if drawdowns.size else 0.0

    if n >= 5 and float(returns.std(ddof=1)) > 0:
        # Annualized via the *trade frequency* implied by avg hold days
        avg_hold = float(np.mean([t.hold_days for t in trades])) or 1.0
        trades_per_year = 252.0 / max(avg_hold, 1.0)
        sharpe = float(returns.mean() / returns.std(ddof=1) * np.sqrt(trades_per_year))
    else:
        sharpe = None

    avg_hold_days = float(np.mean([t.hold_days for t in trades]))

    return BacktestMetrics(
        n_trades=n,
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_win_pct=avg_win_pct,
        avg_loss_pct=avg_loss_pct,
        expectancy_pct=expectancy_pct,
        total_return_pct=total_return_pct,
        max_drawdown_pct=max_drawdown_pct,
        sharpe=sharpe,
        avg_hold_days=avg_hold_days,
    )


def _bars_after(bars: list[dict], start: date) -> list[dict]:
    """Bars strictly after `start`. Bars are dicts with iso `date` strings."""
    out = []
    for b in bars:
        d = b["date"]
        bar_date = d if isinstance(d, date) else date.fromisoformat(d)
        if bar_date > start:
            out.append({**b, "date": bar_date})
    return out


def _resolve_exit(
    entry_price: float,
    entry_date: date,
    forward_bars: list[dict],
    stop_pct: float,
    target_pct: Optional[float],
    time_stop_days: Optional[int],
) -> tuple[date, float, str]:
    """Walk forward bar-by-bar and return the first triggered exit.

    Stop and target are checked against the day's high/low (favoring the
    customer-pessimistic outcome when both could fire on the same bar).
    """
    stop_price = entry_price * (1.0 - stop_pct)
    target_price = (
        entry_price * (1.0 + target_pct) if target_pct is not None else None
    )

    for bar in forward_bars:
        d: date = bar["date"]
        days_held = (d - entry_date).days

        # Conservative resolution: if both stop and target could trigger on the
        # same bar (a wide-range day), assume stop hit first.
        if bar["low"] <= stop_price:
            return d, stop_price, "stop"
        if target_price is not None and bar["high"] >= target_price:
            return d, target_price, "target"
        if time_stop_days is not None and days_held >= time_stop_days:
            return d, float(bar["close"]), "time"

    # Out of data — close at last available price
    if forward_bars:
        last = forward_bars[-1]
        return last["date"], float(last["close"]), "end_of_data"
    return entry_date, entry_price, "end_of_data"


def replay_with_alternate_exits(
    entries: Iterable[dict],
    bars: dict[str, list[dict]],
    stop_pct: float = 0.08,
    target_pct: Optional[float] = 0.15,
    time_stop_days: Optional[int] = 30,
) -> list[SimTrade]:
    """Re-run the bot's actual entries with alternate exit policies.

    Args:
        entries: iterable of {"symbol", "entry_date", "entry_price", "shares"}.
                 entry_date may be `date` or ISO string.
        bars: symbol → list of bar dicts ({date, open, high, low, close, volume}).
        stop_pct, target_pct, time_stop_days: alternate exit rules to evaluate.

    Returns one SimTrade per resolvable entry; entries whose symbol has no
    forward bars are skipped.
    """
    out: list[SimTrade] = []
    for e in entries:
        sym = e["symbol"]
        entry_date_raw = e["entry_date"]
        entry_date = (
            entry_date_raw if isinstance(entry_date_raw, date)
            else date.fromisoformat(str(entry_date_raw)[:10])
        )
        entry_price = float(e["entry_price"])
        shares = float(e.get("shares", 1.0))
        sym_bars = bars.get(sym, [])
        forward = _bars_after(sym_bars, entry_date)
        if not forward:
            continue
        exit_date, exit_price, reason = _resolve_exit(
            entry_price, entry_date, forward,
            stop_pct=stop_pct, target_pct=target_pct, time_stop_days=time_stop_days,
        )
        out.append(SimTrade(
            symbol=sym,
            entry_date=entry_date,
            entry_price=entry_price,
            exit_date=exit_date,
            exit_price=exit_price,
            shares=shares,
            exit_reason=reason,
        ))
    return out


def _rolling(arr: np.ndarray, window: int) -> np.ndarray:
    """Right-aligned rolling mean. Output[i] uses arr[i-window+1:i+1]."""
    if len(arr) < window:
        return np.full_like(arr, np.nan, dtype=float)
    cumsum = np.cumsum(arr, dtype=float)
    out = np.full(len(arr), np.nan, dtype=float)
    out[window - 1:] = (cumsum[window - 1:] - np.concatenate(([0.0], cumsum[:-window]))) / window
    return out


def _rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder RSI. Returns NaN for first `period` elements."""
    if len(closes) <= period:
        return np.full(len(closes), np.nan, dtype=float)
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    out = np.full(len(closes), np.nan, dtype=float)
    avg_gain = float(gains[:period].mean())
    avg_loss = float(losses[:period].mean())
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else 1e9
        out[i + 1] = 100.0 - 100.0 / (1.0 + rs)
    return out


def simulate_breakout_strategy(
    bars: dict[str, list[dict]],
    momentum_5d_min: float = 0.03,
    volume_multiplier: float = 1.5,
    rsi_min: float = 50.0,
    rsi_max: float = 75.0,
    stop_pct: float = 0.08,
    target_pct: Optional[float] = 0.15,
    time_stop_days: Optional[int] = 30,
    max_concurrent: int = 10,
) -> list[SimTrade]:
    """Parameterized swing-breakout simulator.

    Entry: 5-day return ≥ momentum_5d_min, today's volume ≥ vol_mult × 20d avg,
           RSI(14) inside [rsi_min, rsi_max], close above 20-day MA.
    Exit:  stop_pct, target_pct, time_stop_days (any first).

    `max_concurrent` caps simultaneous open positions per symbol pool to mimic
    the bot's max-holdings rule. Trades are scanned chronologically across all
    symbols and entered in date order until the cap is full.
    """
    candidates: list[tuple[date, str, float]] = []  # (signal_date, symbol, entry_price)

    for sym, sym_bars in bars.items():
        if len(sym_bars) < 30:
            continue
        # Normalize date to date objects
        rows = [
            {**b, "date": (b["date"] if isinstance(b["date"], date) else date.fromisoformat(b["date"]))}
            for b in sym_bars
        ]
        rows.sort(key=lambda r: r["date"])
        closes = np.array([r["close"] for r in rows], dtype=float)
        volumes = np.array([r["volume"] for r in rows], dtype=float)

        ma20 = _rolling(closes, 20)
        vol20 = _rolling(volumes, 20)
        rsi14 = _rsi(closes, 14)

        # 5-day return at index i: (close[i] - close[i-5]) / close[i-5]
        for i in range(20, len(closes) - 1):
            if np.isnan(ma20[i]) or np.isnan(vol20[i]) or np.isnan(rsi14[i]):
                continue
            ret_5d = (closes[i] - closes[i - 5]) / closes[i - 5]
            if ret_5d < momentum_5d_min:
                continue
            if volumes[i] < volume_multiplier * vol20[i]:
                continue
            if not (rsi_min <= rsi14[i] <= rsi_max):
                continue
            if closes[i] <= ma20[i]:
                continue
            # Enter on next bar's open (no look-ahead)
            entry_bar = rows[i + 1]
            candidates.append((entry_bar["date"], sym, float(entry_bar["open"])))

    candidates.sort(key=lambda c: c[0])

    open_positions: list[tuple[date, str]] = []  # (exit_date, symbol)
    sim_trades: list[SimTrade] = []
    for signal_date, sym, entry_price in candidates:
        # Drop positions that have already closed by this date
        open_positions = [(d, s) for d, s in open_positions if d > signal_date]
        if len(open_positions) >= max_concurrent:
            continue
        # Don't pyramid same-symbol positions
        if any(s == sym for _, s in open_positions):
            continue
        sym_bars = bars[sym]
        rows = [
            {**b, "date": (b["date"] if isinstance(b["date"], date) else date.fromisoformat(b["date"]))}
            for b in sym_bars
        ]
        rows.sort(key=lambda r: r["date"])
        forward = [r for r in rows if r["date"] > signal_date]
        if not forward:
            continue
        exit_date, exit_price, reason = _resolve_exit(
            entry_price, signal_date, forward,
            stop_pct=stop_pct, target_pct=target_pct, time_stop_days=time_stop_days,
        )
        sim_trades.append(SimTrade(
            symbol=sym,
            entry_date=signal_date,
            entry_price=entry_price,
            exit_date=exit_date,
            exit_price=exit_price,
            shares=1.0,
            exit_reason=reason,
        ))
        open_positions.append((exit_date, sym))

    return sim_trades
