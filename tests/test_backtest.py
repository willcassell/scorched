"""Tests for the backtester."""
from datetime import date, timedelta

import numpy as np
import pytest

from scorched.services.backtest import (
    SimTrade,
    compute_metrics,
    replay_with_alternate_exits,
    simulate_breakout_strategy,
)


def _bars(symbol_close: list[float], start: date = date(2026, 1, 1), volume: float = 1_000_000.0):
    """Build a list of dict bars with high=close*1.02, low=close*0.98."""
    out = []
    d = start
    for c in symbol_close:
        # Skip weekends
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append({
            "date": d,
            "open": c,
            "high": c * 1.02,
            "low": c * 0.98,
            "close": c,
            "volume": volume,
        })
        d += timedelta(days=1)
    return out


class TestComputeMetrics:
    def test_empty_returns_zeros(self):
        m = compute_metrics([])
        assert m.n_trades == 0
        assert m.win_rate == 0.0
        assert m.profit_factor is None

    def test_all_wins(self):
        trades = [
            SimTrade("X", date(2026, 1, 1), 100.0, date(2026, 1, 10), 110.0, 1.0, "target")
            for _ in range(5)
        ]
        m = compute_metrics(trades)
        assert m.n_trades == 5
        assert m.win_rate == 100.0
        assert m.avg_win_pct == pytest.approx(10.0, rel=1e-6)
        assert m.profit_factor is None  # no losses

    def test_mixed_results(self):
        trades = [
            SimTrade("A", date(2026, 1, 1), 100.0, date(2026, 1, 5), 110.0, 1.0, "target"),  # +10%
            SimTrade("B", date(2026, 1, 6), 100.0, date(2026, 1, 10), 95.0, 1.0, "stop"),    # -5%
            SimTrade("C", date(2026, 1, 11), 100.0, date(2026, 1, 15), 92.0, 1.0, "stop"),   # -8%
        ]
        m = compute_metrics(trades)
        assert m.n_trades == 3
        assert m.win_rate == pytest.approx(33.3, abs=0.5)
        assert m.profit_factor == pytest.approx(10.0 / 13.0, rel=1e-3)
        assert m.avg_win_pct == pytest.approx(10.0, rel=1e-6)
        assert m.avg_loss_pct == pytest.approx(-6.5, abs=0.5)
        # Compounded: 1.10 * 0.95 * 0.92 - 1 = -3.86%
        assert m.total_return_pct == pytest.approx(-3.86, abs=0.1)

    def test_max_drawdown_tracks_peak(self):
        # Sequence: +10%, -20%, +5% — peak after first, drawdown at -12% from peak
        trades = [
            SimTrade("A", date(2026, 1, 1), 100.0, date(2026, 1, 5), 110.0, 1.0, "target"),
            SimTrade("B", date(2026, 1, 6), 100.0, date(2026, 1, 10), 80.0, 1.0, "stop"),
            SimTrade("C", date(2026, 1, 11), 100.0, date(2026, 1, 15), 105.0, 1.0, "target"),
        ]
        m = compute_metrics(trades)
        # Equity: 1.10, 0.88, 0.924 — peak 1.10, trough 0.88 → -20%
        assert m.max_drawdown_pct == pytest.approx(-20.0, abs=0.1)


class TestReplayAlternateExits:
    def test_stop_fires_before_target(self):
        # Price drifts down to stop level
        bars = {"X": _bars([100, 99, 98, 97, 96, 95, 94, 93, 92, 91])}
        entries = [{"symbol": "X", "entry_date": date(2026, 1, 1), "entry_price": 100.0}]
        trades = replay_with_alternate_exits(entries, bars, stop_pct=0.05, target_pct=0.10)
        assert len(trades) == 1
        # 5% stop = 95; bar low = 95 * 0.98 = 93.1 once close = 95, so triggers when close=95
        assert trades[0].exit_reason == "stop"
        assert trades[0].exit_price == pytest.approx(95.0, rel=1e-6)

    def test_target_fires_when_price_rises(self):
        bars = {"X": _bars([100, 102, 105, 108, 112, 115])}
        entries = [{"symbol": "X", "entry_date": date(2026, 1, 1), "entry_price": 100.0}]
        trades = replay_with_alternate_exits(entries, bars, stop_pct=0.05, target_pct=0.10)
        assert len(trades) == 1
        assert trades[0].exit_reason == "target"

    def test_time_stop_fires_when_neither_triggers(self):
        bars = {"X": _bars([100] * 60)}  # flat
        entries = [{"symbol": "X", "entry_date": date(2026, 1, 1), "entry_price": 100.0}]
        trades = replay_with_alternate_exits(
            entries, bars, stop_pct=0.50, target_pct=0.50, time_stop_days=10
        )
        assert len(trades) == 1
        assert trades[0].exit_reason == "time"

    def test_no_forward_bars_skipped(self):
        bars = {"X": _bars([100, 101], start=date(2025, 12, 1))}
        entries = [{"symbol": "X", "entry_date": date(2026, 6, 1), "entry_price": 100.0}]
        trades = replay_with_alternate_exits(entries, bars)
        assert trades == []

    def test_iso_string_date_accepted(self):
        bars = {"X": _bars([100] * 30)}
        entries = [{"symbol": "X", "entry_date": "2026-01-01", "entry_price": 100.0}]
        trades = replay_with_alternate_exits(entries, bars, time_stop_days=5)
        assert len(trades) == 1


class TestSimulator:
    def test_simulator_finds_breakout(self):
        # 25 flat days, then a +5% jump on 3x volume → entry next day
        flat = [100.0] * 25
        breakout = [105.0]  # +5% jump
        follow = [110.0, 112.0, 115.0, 113.0, 110.0]
        closes = flat + breakout + follow
        all_bars = _bars(closes)
        # Pump volume on the breakout day (index 25)
        all_bars[25]["volume"] = 5_000_000.0
        bars = {"AAA": all_bars}

        trades = simulate_breakout_strategy(
            bars,
            momentum_5d_min=0.03,
            volume_multiplier=2.0,
            rsi_min=0.0,    # disable RSI gate for the synthetic series
            rsi_max=100.0,
            stop_pct=0.10,
            target_pct=0.05,  # tight target so the +5% on day 27 hits
            time_stop_days=20,
        )
        assert len(trades) >= 1
        assert trades[0].symbol == "AAA"

    def test_simulator_caps_concurrent(self):
        # Two symbols both with breakouts on similar dates; max_concurrent=1
        bars = {}
        for sym in ("AAA", "BBB"):
            closes = [100.0] * 25 + [105.0] + [108.0, 110.0, 112.0, 113.0, 114.0]
            sym_bars = _bars(closes)
            sym_bars[25]["volume"] = 5_000_000.0
            bars[sym] = sym_bars

        trades = simulate_breakout_strategy(
            bars,
            momentum_5d_min=0.03,
            volume_multiplier=2.0,
            rsi_min=0.0,
            rsi_max=100.0,
            target_pct=0.05,
            time_stop_days=10,
            max_concurrent=1,
        )
        # The first signal is taken; second symbol is blocked while first is open
        assert all(t.symbol == trades[0].symbol or t.entry_date > trades[0].exit_date
                   for t in trades)

    def test_simulator_no_signals_with_strict_filter(self):
        bars = {"AAA": _bars([100.0] * 60)}  # no momentum
        trades = simulate_breakout_strategy(bars, momentum_5d_min=0.05)
        assert trades == []
