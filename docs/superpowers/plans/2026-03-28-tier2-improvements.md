# Tier 2 Trading Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 7 improvements to the trading bot that collectively move it from $0.08/day to ~$0.35/day in exchange for significantly better analysis quality: technical indicators, adversarial risk review, analyst consensus data, wider stock universe, full news article text, and proactive position management.

**Architecture:** Each improvement is an independent module that plugs into the existing research → analysis → decision pipeline. New data sources (`technicals.py`, `finnhub.py`) produce structured text that gets appended to the research context. New Claude calls (risk committee, position management) are inserted after the existing pipeline steps. No existing behavior changes — only additions.

**Tech Stack:** `finnhub-python` SDK (free tier), existing `yfinance` + `numpy` for technical calculations, `anthropic` SDK for additional Claude calls, Polygon API (existing integration upgraded to fetch full article text)

---

## File Structure

| File | Responsibility |
|------|---------------|
| `src/scorched/services/technicals.py` | Calculate MACD, Bollinger Bands, 50/200 MA crossover, volume profile, support/resistance for any symbol list |
| `src/scorched/services/finnhub_data.py` | Fetch analyst consensus, price targets, recommendation trends from Finnhub |
| `src/scorched/services/risk_review.py` | Call 3: Adversarial risk committee — reviews recommendations and kills bad trades |
| `src/scorched/services/position_mgmt.py` | Call 4: EOD position review — evaluates each open position and suggests stop adjustments |
| `src/scorched/services/research.py` | Modify: expand WATCHLIST, increase screener to 30, add technicals + finnhub to build_research_context |
| `src/scorched/services/recommender.py` | Modify: increase THINKING_BUDGET, wire in technicals + finnhub fetch, add risk committee call after Call 2 |
| `src/scorched/services/eod_review.py` | Modify: add position management call after existing EOD review |
| `src/scorched/config.py` | Modify: add `finnhub_api_key` setting |
| `analyst_guidance.md` | Modify: add signal interpretation tables for MACD, Bollinger, MA crossover, analyst consensus |
| `tests/test_technicals.py` | Tests for technical analysis calculations |
| `tests/test_finnhub_data.py` | Tests for Finnhub data fetching (mocked) |
| `tests/test_risk_review.py` | Tests for risk committee prompt construction and response parsing |

---

## Task 1: Technical Analysis Module

**Files:**
- Create: `src/scorched/services/technicals.py`
- Create: `tests/test_technicals.py`

- [ ] **Step 1: Write failing tests for technical indicators**

Create `tests/test_technicals.py`:

```python
"""Tests for technical analysis calculations."""
import pytest
import numpy as np
from scorched.services.technicals import (
    calc_macd,
    calc_bollinger_bands,
    calc_ma_crossover,
    calc_support_resistance,
    calc_volume_profile,
    compute_technicals,
)


def _make_prices(n=60, start=100.0, trend=0.5, noise=2.0):
    """Generate synthetic price series for testing."""
    np.random.seed(42)
    prices = [start]
    for _ in range(n - 1):
        prices.append(prices[-1] + trend + np.random.normal(0, noise))
    return prices


class TestMACD:
    def test_returns_correct_keys(self):
        prices = _make_prices(60)
        result = calc_macd(prices)
        assert "macd_line" in result
        assert "signal_line" in result
        assert "histogram" in result
        assert "signal" in result

    def test_signal_is_valid_enum(self):
        prices = _make_prices(60)
        result = calc_macd(prices)
        assert result["signal"] in ("bullish", "bearish", "neutral")

    def test_insufficient_data_returns_none(self):
        result = calc_macd([100, 101, 102])
        assert result is None


class TestBollingerBands:
    def test_returns_correct_keys(self):
        prices = _make_prices(30)
        result = calc_bollinger_bands(prices)
        assert "upper" in result
        assert "middle" in result
        assert "lower" in result
        assert "pct_b" in result
        assert "signal" in result

    def test_price_within_bands(self):
        prices = _make_prices(30)
        result = calc_bollinger_bands(prices)
        assert result["lower"] <= result["middle"] <= result["upper"]

    def test_signal_is_valid(self):
        prices = _make_prices(30)
        result = calc_bollinger_bands(prices)
        assert result["signal"] in ("overbought", "oversold", "neutral")


class TestMACrossover:
    def test_returns_correct_keys(self):
        prices = _make_prices(210)
        result = calc_ma_crossover(prices)
        assert "ma_50" in result
        assert "ma_200" in result
        assert "signal" in result

    def test_signal_values(self):
        prices = _make_prices(210)
        result = calc_ma_crossover(prices)
        assert result["signal"] in ("golden_cross", "death_cross", "above_both", "below_both", "between")


class TestSupportResistance:
    def test_returns_levels(self):
        prices = _make_prices(60)
        result = calc_support_resistance(prices)
        assert "support" in result
        assert "resistance" in result
        assert isinstance(result["support"], float)
        assert isinstance(result["resistance"], float)
        assert result["support"] < result["resistance"]


class TestVolumeProfile:
    def test_returns_signal(self):
        prices = _make_prices(20)
        volumes = [1_000_000 + i * 50_000 for i in range(20)]
        result = calc_volume_profile(prices, volumes)
        assert "avg_volume_20d" in result
        assert "relative_volume" in result
        assert "signal" in result
        assert result["signal"] in ("high_volume", "low_volume", "normal")


class TestComputeTechnicals:
    def test_returns_dict_per_symbol(self):
        price_data = {
            "AAPL": {
                "history_close": _make_prices(210),
                "history_volume": [1_000_000] * 210,
            }
        }
        result = compute_technicals(price_data)
        assert "AAPL" in result
        assert "macd" in result["AAPL"]
        assert "bollinger" in result["AAPL"]
        assert "ma_crossover" in result["AAPL"]
        assert "support_resistance" in result["AAPL"]
        assert "volume" in result["AAPL"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/test_technicals.py -v 2>&1 | tail -5`
Expected: FAIL — `ModuleNotFoundError: No module named 'scorched.services.technicals'`

- [ ] **Step 3: Implement technical analysis module**

Create `src/scorched/services/technicals.py`:

```python
"""Technical analysis calculations — pure math on price/volume arrays.

All functions take plain Python lists and return dicts. No I/O, no API calls.
yfinance history data is passed in by the caller.
"""
import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


def calc_macd(
    prices: list[float],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> Optional[dict]:
    """Calculate MACD line, signal line, histogram, and directional signal."""
    if len(prices) < slow + signal_period:
        return None

    arr = np.array(prices, dtype=float)

    def _ema(data, span):
        weights = np.exp(np.linspace(-1., 0., span))
        weights /= weights.sum()
        ema = np.convolve(data, weights, mode='full')[:len(data)]
        ema[:span] = ema[span]
        return ema

    ema_fast = _ema(arr, fast)
    ema_slow = _ema(arr, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal_period)
    histogram = macd_line - signal_line

    current_hist = float(histogram[-1])
    prev_hist = float(histogram[-2]) if len(histogram) > 1 else 0

    if current_hist > 0 and current_hist > prev_hist:
        signal = "bullish"
    elif current_hist < 0 and current_hist < prev_hist:
        signal = "bearish"
    else:
        signal = "neutral"

    return {
        "macd_line": round(float(macd_line[-1]), 4),
        "signal_line": round(float(signal_line[-1]), 4),
        "histogram": round(current_hist, 4),
        "signal": signal,
    }


def calc_bollinger_bands(
    prices: list[float],
    period: int = 20,
    num_std: float = 2.0,
) -> Optional[dict]:
    """Calculate Bollinger Bands and %B position."""
    if len(prices) < period:
        return None

    arr = np.array(prices[-period:], dtype=float)
    middle = float(np.mean(arr))
    std = float(np.std(arr, ddof=1))
    upper = middle + num_std * std
    lower = middle - num_std * std

    current = prices[-1]
    band_width = upper - lower
    pct_b = (current - lower) / band_width if band_width > 0 else 0.5

    if pct_b > 1.0:
        signal = "overbought"
    elif pct_b < 0.0:
        signal = "oversold"
    else:
        signal = "neutral"

    return {
        "upper": round(upper, 2),
        "middle": round(middle, 2),
        "lower": round(lower, 2),
        "pct_b": round(pct_b, 3),
        "signal": signal,
    }


def calc_ma_crossover(prices: list[float]) -> Optional[dict]:
    """Calculate 50/200 MA crossover status."""
    if len(prices) < 200:
        return None

    arr = np.array(prices, dtype=float)
    ma_50 = float(np.mean(arr[-50:]))
    ma_200 = float(np.mean(arr[-200:]))
    current = prices[-1]

    # Check for recent crossover (within last 5 days)
    prev_ma_50 = float(np.mean(arr[-55:-5])) if len(arr) >= 55 else ma_50
    prev_ma_200 = float(np.mean(arr[-205:-5])) if len(arr) >= 205 else ma_200

    if prev_ma_50 <= prev_ma_200 and ma_50 > ma_200:
        signal = "golden_cross"
    elif prev_ma_50 >= prev_ma_200 and ma_50 < ma_200:
        signal = "death_cross"
    elif current > ma_50 and current > ma_200:
        signal = "above_both"
    elif current < ma_50 and current < ma_200:
        signal = "below_both"
    else:
        signal = "between"

    return {
        "ma_50": round(ma_50, 2),
        "ma_200": round(ma_200, 2),
        "signal": signal,
    }


def calc_support_resistance(prices: list[float], lookback: int = 20) -> dict:
    """Estimate support and resistance from recent price action.

    Uses the lowest low and highest high in the lookback window.
    Simple but effective for short-term momentum trading.
    """
    recent = prices[-lookback:] if len(prices) >= lookback else prices
    support = float(min(recent))
    resistance = float(max(recent))
    return {
        "support": round(support, 2),
        "resistance": round(resistance, 2),
    }


def calc_volume_profile(
    prices: list[float],
    volumes: list[float],
    period: int = 20,
) -> dict:
    """Calculate volume profile: average volume and relative volume."""
    if len(volumes) < period:
        avg_vol = float(np.mean(volumes)) if volumes else 0
        rel_vol = 1.0
    else:
        avg_vol = float(np.mean(volumes[-period:]))
        today_vol = volumes[-1] if volumes else 0
        rel_vol = today_vol / avg_vol if avg_vol > 0 else 1.0

    if rel_vol > 1.5:
        signal = "high_volume"
    elif rel_vol < 0.5:
        signal = "low_volume"
    else:
        signal = "normal"

    return {
        "avg_volume_20d": round(avg_vol),
        "relative_volume": round(rel_vol, 2),
        "signal": signal,
    }


def compute_technicals(price_data: dict) -> dict:
    """Compute all technical indicators for a dict of {symbol: {history_close, history_volume}}.

    This is the main entry point called from the research pipeline.
    Returns {symbol: {macd: {...}, bollinger: {...}, ...}}.
    """
    results = {}
    for symbol, data in price_data.items():
        closes = data.get("history_close", [])
        volumes = data.get("history_volume", [])

        technicals = {}

        macd = calc_macd(closes)
        if macd:
            technicals["macd"] = macd

        bb = calc_bollinger_bands(closes)
        if bb:
            technicals["bollinger"] = bb

        ma = calc_ma_crossover(closes)
        if ma:
            technicals["ma_crossover"] = ma

        sr = calc_support_resistance(closes)
        technicals["support_resistance"] = sr

        vol = calc_volume_profile(closes, volumes)
        technicals["volume"] = vol

        results[symbol] = technicals

    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/test_technicals.py -v 2>&1 | tail -20`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/scorched/services/technicals.py tests/test_technicals.py
git commit -m "feat: add technical analysis module (MACD, Bollinger, MA crossover, S/R, volume)"
```

---

## Task 2: Expand Price Data to Include History Arrays

**Files:**
- Modify: `src/scorched/services/research.py` — `_fetch_price_data_sync`

The technical analysis module needs close/volume history arrays, not just the summary stats currently fetched. We need to add `history_close` and `history_volume` to each symbol's price data dict.

- [ ] **Step 1: Update `_fetch_price_data_sync` to include history arrays**

In `src/scorched/services/research.py`, in the `_fetch_price_data_sync` function, change `hist = ticker.history(period="1mo")` to fetch 1 year of data (needed for 200-day MA), and add the history arrays to the result dict. Find the line:

```python
hist = ticker.history(period="1mo")
```

Replace with:

```python
hist = ticker.history(period="1y")
```

Then after the line `"insider_buy_pct": None,  # populated separately`, add:

```python
                "history_close": [float(x) for x in hist["Close"].tolist()],
                "history_volume": [float(x) for x in hist["Volume"].tolist()],
```

Also fix the `week_ago_price` and `month_ago_price` references — with 1y of data, index -5 is still 1 week ago, and index 0 is now 1 year ago. Change:

```python
month_ago_price = float(hist["Close"].iloc[0])
```

To:

```python
month_ago_price = float(hist["Close"].iloc[-22]) if len(hist) >= 22 else float(hist["Close"].iloc[0])
```

- [ ] **Step 2: Run all tests to verify no regression**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/ -v 2>&1 | tail -10`
Expected: All tests PASS.

- [ ] **Step 3: Commit**

```bash
git add src/scorched/services/research.py
git commit -m "feat: extend price data fetch to 1y history for technical analysis"
```

---

## Task 3: Wire Technicals into Research Context and Recommender

**Files:**
- Modify: `src/scorched/services/research.py` — `build_research_context`
- Modify: `src/scorched/services/recommender.py` — `generate_recommendations`

- [ ] **Step 1: Add technicals parameter to `build_research_context`**

In `src/scorched/services/research.py`, update the `build_research_context` function signature to accept a `technicals` parameter:

```python
def build_research_context(
    portfolio_dict: dict,
    price_data: dict,
    news_data: dict,
    current_symbols: list[str],
    earnings_surprise: dict | None = None,
    insider_activity: dict | None = None,
    fred_macro: dict | None = None,
    polygon_news: dict | None = None,
    av_technicals: dict | None = None,
    technicals: dict | None = None,
) -> str:
```

Then in the per-stock section, after the RSI block (after the `if av_technicals and symbol in av_technicals:` block), add:

```python
        # Technical analysis (MACD, Bollinger, MA crossover, S/R, volume)
        if technicals and symbol in technicals:
            t = technicals[symbol]
            ta_parts = []
            if "macd" in t:
                m = t["macd"]
                ta_parts.append(f"MACD: {m['signal'].upper()} (hist={m['histogram']:+.4f})")
            if "bollinger" in t:
                b = t["bollinger"]
                ta_parts.append(f"BB: {b['signal'].upper()} (%B={b['pct_b']:.2f}, band=${b['lower']:.0f}-${b['upper']:.0f})")
            if "ma_crossover" in t:
                ma = t["ma_crossover"]
                ta_parts.append(f"MA: {ma['signal'].upper()} (50d=${ma['ma_50']:.0f}, 200d=${ma['ma_200']:.0f})")
            if "support_resistance" in t:
                sr = t["support_resistance"]
                ta_parts.append(f"S/R: ${sr['support']:.0f} / ${sr['resistance']:.0f}")
            if "volume" in t:
                v = t["volume"]
                ta_parts.append(f"Vol: {v['signal'].upper()} (rel={v['relative_volume']:.1f}x)")
            if ta_parts:
                lines.append(f"  Technicals: {' | '.join(ta_parts)}")
```

- [ ] **Step 2: Wire technicals into recommender's data fetch**

In `src/scorched/services/recommender.py`, add the import at the top (after the existing research imports):

```python
from .technicals import compute_technicals
```

Then in `generate_recommendations`, after the Phase 1 parallel fetch `asyncio.gather(...)` block (around line 281), add:

```python
    # Compute technical indicators from price history (pure math, no I/O)
    technicals = compute_technicals(price_data)
    logger.info("Computed technicals for %d symbols", len(technicals))
```

Then update the `build_research_context` call to pass technicals:

```python
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
    )
```

- [ ] **Step 3: Run all tests**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/ -v 2>&1 | tail -10`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add src/scorched/services/research.py src/scorched/services/recommender.py
git commit -m "feat: wire technical analysis into research context and recommender pipeline"
```

---

## Task 4: Finnhub Analyst Consensus Integration

**Files:**
- Create: `src/scorched/services/finnhub_data.py`
- Create: `tests/test_finnhub_data.py`
- Modify: `src/scorched/config.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add finnhub dependency and config**

Add `"finnhub-python>=2.4.0",` to the `dependencies` list in `pyproject.toml` (after the `alpaca-py` line).

Add to `src/scorched/config.py` Settings class (after `alpaca_secret_key`):

```python
    finnhub_api_key: str = ""
```

- [ ] **Step 2: Write failing tests**

Create `tests/test_finnhub_data.py`:

```python
"""Tests for Finnhub analyst consensus data fetching."""
import pytest
from unittest.mock import MagicMock, patch
from scorched.services.finnhub_data import (
    fetch_analyst_consensus_sync,
    build_analyst_context,
)


def _mock_client():
    client = MagicMock()
    client.recommendation_trends.return_value = [
        MagicMock(
            buy=10, hold=5, sell=2, strong_buy=3, strong_sell=1,
            period="2026-03-01",
        )
    ]
    client.price_target.return_value = MagicMock(
        target_high=200.0, target_low=140.0, target_mean=175.0, target_median=172.0,
    )
    return client


class TestFetchAnalystConsensus:
    def test_returns_data_for_symbol(self):
        client = _mock_client()
        result = fetch_analyst_consensus_sync(["AAPL"], client)
        assert "AAPL" in result
        assert result["AAPL"]["strong_buy"] == 3
        assert result["AAPL"]["buy"] == 10
        assert result["AAPL"]["target_mean"] == 175.0

    def test_empty_on_no_api_key(self):
        result = fetch_analyst_consensus_sync(["AAPL"], None)
        assert result == {}

    def test_handles_api_error_gracefully(self):
        client = MagicMock()
        client.recommendation_trends.side_effect = Exception("API error")
        client.price_target.side_effect = Exception("API error")
        result = fetch_analyst_consensus_sync(["AAPL"], client)
        assert result.get("AAPL") is None or result == {}


class TestBuildAnalystContext:
    def test_formats_output(self):
        data = {
            "AAPL": {
                "strong_buy": 3, "buy": 10, "hold": 5, "sell": 2, "strong_sell": 1,
                "target_high": 200.0, "target_low": 140.0, "target_mean": 175.0,
            }
        }
        text = build_analyst_context(data)
        assert "AAPL" in text
        assert "Strong Buy: 3" in text
        assert "175.0" in text

    def test_empty_data_returns_empty(self):
        assert build_analyst_context({}) == ""
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/test_finnhub_data.py -v 2>&1 | tail -5`
Expected: FAIL

- [ ] **Step 4: Implement Finnhub data module**

Create `src/scorched/services/finnhub_data.py`:

```python
"""Finnhub analyst consensus and price target data."""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def fetch_analyst_consensus_sync(symbols: list[str], client) -> dict:
    """Fetch analyst recommendation trends and price targets from Finnhub.

    Args:
        symbols: List of ticker symbols.
        client: A finnhub.Client instance, or None if no API key configured.

    Returns:
        {symbol: {strong_buy, buy, hold, sell, strong_sell, target_high, target_low, target_mean}}
    """
    if client is None:
        return {}

    result = {}
    for symbol in symbols:
        try:
            # Recommendation trends (most recent period)
            trends = client.recommendation_trends(symbol)
            if not trends:
                continue
            latest = trends[0]
            entry = {
                "strong_buy": latest.strong_buy if hasattr(latest, 'strong_buy') else getattr(latest, 'strongBuy', 0),
                "buy": latest.buy,
                "hold": latest.hold,
                "sell": latest.sell,
                "strong_sell": latest.strong_sell if hasattr(latest, 'strong_sell') else getattr(latest, 'strongSell', 0),
            }

            # Price targets
            try:
                pt = client.price_target(symbol)
                if pt:
                    entry["target_high"] = pt.target_high if hasattr(pt, 'target_high') else getattr(pt, 'targetHigh', None)
                    entry["target_low"] = pt.target_low if hasattr(pt, 'target_low') else getattr(pt, 'targetLow', None)
                    entry["target_mean"] = pt.target_mean if hasattr(pt, 'target_mean') else getattr(pt, 'targetMean', None)
            except Exception:
                entry["target_high"] = None
                entry["target_low"] = None
                entry["target_mean"] = None

            result[symbol] = entry
        except Exception as e:
            logger.warning("Finnhub: failed to fetch %s: %s", symbol, e)

    return result


def build_analyst_context(analyst_data: dict) -> str:
    """Format analyst consensus data as text for injection into Claude's prompt."""
    if not analyst_data:
        return ""

    lines = ["=== ANALYST CONSENSUS (FINNHUB) ==="]
    for symbol, data in sorted(analyst_data.items()):
        total = sum([
            data.get("strong_buy", 0), data.get("buy", 0),
            data.get("hold", 0), data.get("sell", 0), data.get("strong_sell", 0),
        ])
        if total == 0:
            continue

        bullish = data.get("strong_buy", 0) + data.get("buy", 0)
        bearish = data.get("sell", 0) + data.get("strong_sell", 0)
        bull_pct = round(bullish / total * 100) if total > 0 else 0

        line = (
            f"{symbol}: Strong Buy: {data.get('strong_buy', 0)}, "
            f"Buy: {data.get('buy', 0)}, Hold: {data.get('hold', 0)}, "
            f"Sell: {data.get('sell', 0)}, Strong Sell: {data.get('strong_sell', 0)} "
            f"({bull_pct}% bullish)"
        )

        pt_mean = data.get("target_mean")
        pt_high = data.get("target_high")
        pt_low = data.get("target_low")
        if pt_mean:
            line += f" | PT: ${pt_low:.0f}-${pt_high:.0f} (mean ${pt_mean:.0f})"

        lines.append(f"  {line}")

    return "\n".join(lines) if len(lines) > 1 else ""
```

- [ ] **Step 5: Install finnhub-python and run tests**

Run: `cd /home/ubuntu/tradebot && pip3 install --break-system-packages finnhub-python>=2.4.0`
Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/test_finnhub_data.py -v 2>&1 | tail -10`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/scorched/services/finnhub_data.py tests/test_finnhub_data.py src/scorched/config.py pyproject.toml
git commit -m "feat: add Finnhub analyst consensus and price target integration"
```

---

## Task 5: Wire Finnhub into Research Pipeline

**Files:**
- Modify: `src/scorched/services/research.py` — `build_research_context`
- Modify: `src/scorched/services/recommender.py` — `generate_recommendations`

- [ ] **Step 1: Add Finnhub fetch to recommender's parallel gather**

In `src/scorched/services/recommender.py`, add the import:

```python
from .finnhub_data import fetch_analyst_consensus_sync, build_analyst_context
```

In `generate_recommendations`, before the Phase 1 parallel fetch, create the Finnhub client:

```python
    # Initialize Finnhub client (None if no API key)
    finnhub_client = None
    if settings.finnhub_api_key:
        import finnhub
        finnhub_client = finnhub.Client(api_key=settings.finnhub_api_key)
```

Add a new async wrapper and add it to the gather. After the existing gather block, add a separate call (Finnhub is rate-limited, so we fetch it for research_symbols only, running in executor):

```python
    # Finnhub analyst consensus (separate from main gather — has its own rate limits)
    import asyncio
    loop = asyncio.get_event_loop()
    analyst_consensus = await loop.run_in_executor(
        None, fetch_analyst_consensus_sync, research_symbols, finnhub_client
    )
    logger.info("Fetched analyst consensus for %d symbols", len(analyst_consensus))
```

- [ ] **Step 2: Add analyst_consensus to build_research_context**

In `src/scorched/services/research.py`, update `build_research_context` signature to accept `analyst_consensus`:

```python
def build_research_context(
    portfolio_dict: dict,
    price_data: dict,
    news_data: dict,
    current_symbols: list[str],
    earnings_surprise: dict | None = None,
    insider_activity: dict | None = None,
    fred_macro: dict | None = None,
    polygon_news: dict | None = None,
    av_technicals: dict | None = None,
    technicals: dict | None = None,
    analyst_consensus: dict | None = None,
) -> str:
```

After the FRED macro section and before the portfolio section, add:

```python
    # Analyst consensus section
    if analyst_consensus:
        from .finnhub_data import build_analyst_context
        analyst_text = build_analyst_context(analyst_consensus)
        if analyst_text:
            lines.append(analyst_text)
            lines.append("")
```

Update the `build_research_context` call in `recommender.py` to pass `analyst_consensus=analyst_consensus`.

- [ ] **Step 3: Run all tests**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/ -v 2>&1 | tail -10`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add src/scorched/services/research.py src/scorched/services/recommender.py
git commit -m "feat: wire Finnhub analyst consensus into research pipeline"
```

---

## Task 6: Risk Committee Call (Call 3 — Adversarial Review)

**Files:**
- Create: `src/scorched/services/risk_review.py`
- Create: `tests/test_risk_review.py`
- Modify: `src/scorched/services/recommender.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_risk_review.py`:

```python
"""Tests for risk committee adversarial review."""
import pytest
import json
from scorched.services.risk_review import (
    build_risk_review_prompt,
    parse_risk_review_response,
    RISK_REVIEW_SYSTEM,
)


class TestBuildRiskReviewPrompt:
    def test_includes_recommendations(self):
        recs = [
            {"symbol": "AAPL", "action": "buy", "quantity": 10, "reasoning": "Strong momentum"},
        ]
        portfolio = {"cash_balance": 50000, "positions": []}
        prompt = build_risk_review_prompt(recs, portfolio, "Market looks good", "")
        assert "AAPL" in prompt
        assert "buy" in prompt.lower()

    def test_includes_portfolio_context(self):
        recs = []
        portfolio = {
            "cash_balance": 50000,
            "positions": [
                {"symbol": "NVDA", "shares": 50, "days_held": 5, "unrealized_gain": 500}
            ],
        }
        prompt = build_risk_review_prompt(recs, portfolio, "", "")
        assert "NVDA" in prompt

    def test_system_prompt_exists(self):
        assert "risk" in RISK_REVIEW_SYSTEM.lower()
        assert len(RISK_REVIEW_SYSTEM) > 100


class TestParseRiskReviewResponse:
    def test_parses_approved_trades(self):
        response = json.dumps({
            "decisions": [
                {"symbol": "AAPL", "action": "buy", "verdict": "approve", "reason": "Solid setup"},
                {"symbol": "TSLA", "action": "buy", "verdict": "reject", "reason": "Too risky"},
            ]
        })
        result = parse_risk_review_response(response)
        assert len(result) == 2
        assert result[0]["verdict"] == "approve"
        assert result[1]["verdict"] == "reject"

    def test_handles_malformed_json(self):
        result = parse_risk_review_response("not json at all")
        assert result == []

    def test_handles_empty_decisions(self):
        response = json.dumps({"decisions": []})
        result = parse_risk_review_response(response)
        assert result == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/test_risk_review.py -v 2>&1 | tail -5`
Expected: FAIL

- [ ] **Step 3: Implement risk review module**

Create `src/scorched/services/risk_review.py`:

```python
"""Call 3: Risk committee — adversarial review of trade recommendations.

After the trader (Call 2) generates recommendations, the risk committee
reviews each one and can approve or reject. Its job is to find reasons
NOT to trade. Only trades that survive both the trader and the risk
reviewer get submitted.
"""
import json
import logging
import re

logger = logging.getLogger(__name__)


RISK_REVIEW_SYSTEM = """You are an independent risk committee reviewing proposed trades. Your job is to be skeptical and find reasons NOT to trade. You are the last line of defense before real money is deployed.

For each proposed trade, evaluate:

1. **Thesis quality**: Is the catalyst specific and verifiable, or vague? Would this thesis have worked on the last 3 similar setups?
2. **Concentration risk**: Does this trade increase portfolio correlation? If the portfolio already has 2+ positions in the same sector or theme, reject new entries in that sector.
3. **Timing risk**: Is this chasing a move that already happened? Is the stock extended (>8% in 5 days)?
4. **Loss pattern match**: Does this trade resemble recent losing trades in the portfolio? (Same sector, same type of thesis, same entry pattern)
5. **Risk/reward**: Is the downside (-5% stop) proportional to the realistic upside given the holding period?
6. **Macro alignment**: Does this trade fight the current macro regime? (e.g., buying cyclicals when macro indicators are deteriorating)

## Output format
Respond with valid JSON only:
{{
  "review_summary": "1-2 sentence overall assessment",
  "decisions": [
    {{
      "symbol": "TICKER",
      "action": "buy or sell",
      "verdict": "approve" or "reject",
      "reason": "Specific reason for the decision (2-3 sentences)"
    }}
  ]
}}

Default to REJECT unless the trade clearly passes all checks. Approving a bad trade is worse than missing a good one.
Sell recommendations should almost always be approved — exiting risk is good."""


def build_risk_review_prompt(
    recommendations: list[dict],
    portfolio: dict,
    analysis_text: str,
    playbook_excerpt: str,
) -> str:
    """Build the user prompt for the risk committee review."""
    lines = [f"## Proposed Trades for Review\n"]
    for rec in recommendations:
        lines.append(
            f"- {rec.get('action', '').upper()} {rec['symbol']}: "
            f"{rec.get('quantity', '?')} shares @ ~${rec.get('suggested_price', '?')}\n"
            f"  Reasoning: {rec.get('reasoning', 'none')}\n"
            f"  Confidence: {rec.get('confidence', 'unknown')}\n"
            f"  Key risks flagged by trader: {rec.get('key_risks', 'none')}\n"
        )

    lines.append(f"\n## Current Portfolio\n")
    lines.append(f"Cash: ${portfolio.get('cash_balance', 0):,.2f}")
    for pos in portfolio.get("positions", []):
        lines.append(
            f"  {pos['symbol']}: {pos.get('shares', 0)} shares, "
            f"{pos.get('days_held', 0)}d held, "
            f"P&L: ${pos.get('unrealized_gain', 0):+,.2f}"
        )

    if playbook_excerpt:
        lines.append(f"\n## Recent Playbook Learnings\n{playbook_excerpt[:500]}")

    lines.append(f"\n## Today's Market Analysis Summary\n{analysis_text[:800]}")

    return "\n".join(lines)


def parse_risk_review_response(raw: str) -> list[dict]:
    """Parse the risk committee's JSON response into a list of decisions."""
    try:
        parsed = json.loads(raw)
        return parsed.get("decisions", [])
    except json.JSONDecodeError:
        match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1))
                return parsed.get("decisions", [])
            except json.JSONDecodeError:
                pass
    logger.warning("Failed to parse risk review response")
    return []
```

- [ ] **Step 4: Run tests**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/test_risk_review.py -v 2>&1 | tail -10`
Expected: All tests PASS.

- [ ] **Step 5: Wire risk committee into recommender after Call 2**

In `src/scorched/services/recommender.py`, add the import:

```python
from .risk_review import RISK_REVIEW_SYSTEM, build_risk_review_prompt, parse_risk_review_response
```

After Call 2 response is parsed and `raw_recs` is extracted (after the line `raw_recs = parsed.get("recommendations", [])[:3]`), insert Call 3:

```python
    # ── Call 3: Risk committee review (adversarial) ──────────────────────────
    if raw_recs:
        logger.info("Call 3: risk committee review of %d recommendations", len(raw_recs))
        playbook_excerpt = playbook.content[:500] if playbook else ""
        risk_prompt = build_risk_review_prompt(raw_recs, portfolio_dict, analysis_text, playbook_excerpt)

        call3_response = claude_call_with_retry(
            client, "Call 3 (risk review)",
            model=MODEL,
            max_tokens=1024,
            system=RISK_REVIEW_SYSTEM,
            messages=[{"role": "user", "content": risk_prompt}],
        )

        usage3 = call3_response.usage
        await record_usage(
            db,
            session_id=session_row.id,
            call_type="risk_review",
            model=MODEL,
            input_tokens=usage3.input_tokens,
            output_tokens=usage3.output_tokens,
        )

        risk_decisions = parse_risk_review_response(call3_response.content[0].text)
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
```

- [ ] **Step 6: Run all tests**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/ -v 2>&1 | tail -10`
Expected: All tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/scorched/services/risk_review.py tests/test_risk_review.py src/scorched/services/recommender.py
git commit -m "feat: add Call 3 risk committee adversarial review of recommendations"
```

---

## Task 7: Increase Thinking Budget and Expand Universe

**Files:**
- Modify: `src/scorched/services/recommender.py` — `THINKING_BUDGET`
- Modify: `src/scorched/services/research.py` — `WATCHLIST`, screener `n` param

- [ ] **Step 1: Increase thinking budget from 8000 to 16000**

In `src/scorched/services/recommender.py`, change:

```python
THINKING_BUDGET = 8000  # tokens; ~$0.024/day
```

To:

```python
THINKING_BUDGET = 16000  # tokens; ~$0.048/day (Tier 2 upgrade from 8K)
```

- [ ] **Step 2: Expand watchlist to ~60 stocks**

In `src/scorched/services/research.py`, replace the WATCHLIST:

```python
WATCHLIST = [
    # Mega-cap tech (10)
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "ORCL", "CRM",
    # Tech growth (8)
    "AMD", "NFLX", "ADBE", "NOW", "PANW", "CRWD", "DDOG", "PLTR",
    # Financials (8)
    "JPM", "V", "MA", "GS", "BLK", "SPGI", "SCHW", "ICE",
    # Healthcare (6)
    "UNH", "JNJ", "ABBV", "LLY", "MRK", "PFE",
    # Consumer (6)
    "WMT", "HD", "COST", "PG", "KO", "PEP",
    # Energy (6)
    "XOM", "CVX", "COP", "SLB", "HAL", "OXY",
    # Industrials (6)
    "BA", "CAT", "HON", "GE", "RTX", "DE",
    # Other high-liquidity (8)
    "UBER", "DIS", "HOOD", "PYPL", "COIN", "NET", "SNOW", "SHOP",
]
```

- [ ] **Step 3: Increase momentum screener from 20 to 30**

In `src/scorched/services/recommender.py`, change:

```python
    screener_symbols = await fetch_momentum_screener(n=20)
```

To:

```python
    screener_symbols = await fetch_momentum_screener(n=30)
```

- [ ] **Step 4: Run all tests**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/ -v 2>&1 | tail -10`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/scorched/services/recommender.py src/scorched/services/research.py
git commit -m "feat: increase thinking budget to 16K and expand universe to 60+30 stocks"
```

---

## Task 8: Position Management Call (Call 4 — EOD Review Extension)

**Files:**
- Create: `src/scorched/services/position_mgmt.py`
- Modify: `src/scorched/services/eod_review.py`

- [ ] **Step 1: Create position management module**

Create `src/scorched/services/position_mgmt.py`:

```python
"""Call 4: EOD position management — review each open position and suggest stop adjustments.

Runs after the existing EOD review. Evaluates each position against today's
price action and suggests whether to tighten stops, take partial profit, or hold.
"""
import logging

logger = logging.getLogger(__name__)

POSITION_MGMT_SYSTEM = """You are reviewing open positions after market close. For each position, evaluate today's price action and recommend an action for tomorrow.

For each position, consider:
- How many days has it been held vs. the strategy's target holding period?
- Is the position approaching a stop loss or profit target?
- Did today's price action strengthen or weaken the original thesis?
- Are there any earnings or events approaching that create risk?

## Output format
Respond with valid JSON only:
{{
  "position_reviews": [
    {{
      "symbol": "TICKER",
      "action": "hold" or "tighten_stop" or "take_partial" or "exit_tomorrow",
      "new_stop_pct": null or float (e.g. -3.0 means set stop at -3% from current price),
      "reasoning": "1-2 sentences"
    }}
  ]
}}

Be conservative. "hold" is the default. Only recommend changes when today's action provides clear evidence."""


def build_position_review_prompt(positions: list[dict], market_summary: str) -> str:
    """Build user prompt for position management review."""
    lines = ["## Open Positions for Review\n"]
    for pos in positions:
        lines.append(
            f"- {pos['symbol']}: {pos.get('shares', 0)} shares, "
            f"avg cost ${pos.get('avg_cost_basis', 0):.2f}, "
            f"current ${pos.get('current_price', 0):.2f}, "
            f"P&L {pos.get('unrealized_gain_pct', 0):+.1f}%, "
            f"held {pos.get('days_held', 0)} days"
        )

    lines.append(f"\n## Today's Market Summary\n{market_summary[:500]}")
    return "\n".join(lines)
```

- [ ] **Step 2: Wire into EOD review**

In `src/scorched/services/eod_review.py`, at the end of the `run_eod_review` function, add the position management call. Read the file first to find the exact insertion point, then add after the existing playbook update logic:

Import at top:

```python
from .position_mgmt import POSITION_MGMT_SYSTEM, build_position_review_prompt
```

After the existing EOD review completes (playbook is updated), add:

```python
    # ── Call 4: Position management review ───────────────────────────────────
    if positions:
        logger.info("Call 4: position management review for %d positions", len(positions))
        pos_prompt = build_position_review_prompt(
            [{"symbol": p.symbol, "shares": float(p.shares),
              "avg_cost_basis": float(p.avg_cost_basis),
              "current_price": float(price_data.get(p.symbol, {}).get("current_price", float(p.avg_cost_basis))),
              "unrealized_gain_pct": round(
                  (float(price_data.get(p.symbol, {}).get("current_price", float(p.avg_cost_basis))) - float(p.avg_cost_basis))
                  / float(p.avg_cost_basis) * 100, 1
              ) if float(p.avg_cost_basis) > 0 else 0,
              "days_held": (review_date - p.first_purchase_date).days}
             for p in positions],
            market_summary,
        )

        pos_response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=POSITION_MGMT_SYSTEM,
            messages=[{"role": "user", "content": pos_prompt}],
        )

        await record_usage(
            db,
            session_id=None,
            call_type="position_mgmt",
            model=MODEL,
            input_tokens=pos_response.usage.input_tokens,
            output_tokens=pos_response.usage.output_tokens,
        )

        logger.info("Position management review: %s", pos_response.content[0].text[:200])
```

Note: This logs the position management output. A future enhancement could parse it and auto-adjust circuit breaker thresholds, but for now it provides the intelligence in the logs and can be reviewed.

- [ ] **Step 3: Run all tests**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/ -v 2>&1 | tail -10`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add src/scorched/services/position_mgmt.py src/scorched/services/eod_review.py
git commit -m "feat: add Call 4 position management review at EOD"
```

---

## Task 9: Update Analyst Guidance with New Signal Interpretation Tables

**Files:**
- Modify: `analyst_guidance.md`

- [ ] **Step 1: Add technical analysis signal interpretation**

In `analyst_guidance.md`, after the existing `### RSI(14) from Alpha Vantage` section and before the `### FRED Macro Indicators` section, add:

```markdown
### Technical Indicators (computed)

**MACD:**
- BULLISH (histogram positive and rising): Momentum is accelerating upward — supports buy entries.
- BEARISH (histogram negative and falling): Momentum is deteriorating — avoid new buys, consider exits.
- NEUTRAL: No clear momentum signal — rely on other indicators.

**Bollinger Bands (%B):**
- %B > 1.0 (OVERBOUGHT): Price is above the upper band — overextended, expect a pullback. Lower confidence on new buys.
- %B < 0.0 (OVERSOLD): Price is below the lower band — wrong direction for momentum strategy. Avoid.
- %B 0.3–0.7 (NEUTRAL): Price is within normal range — no band signal, rely on other data.

**50/200 MA Crossover:**
- GOLDEN_CROSS: 50-day MA crossed above 200-day — strong long-term bullish signal. Supports buy thesis.
- DEATH_CROSS: 50-day MA crossed below 200-day — strong bearish signal. Avoid new buys.
- ABOVE_BOTH: Price above both MAs — healthy uptrend. Good for momentum entries.
- BELOW_BOTH: Price below both MAs — downtrend. Avoid.
- BETWEEN: Mixed signal — proceed with caution, require strong catalyst.

**Support/Resistance:**
- Price near support with positive catalyst: Potential bounce entry (lower risk).
- Price near resistance: Breakout candidate if volume confirms, otherwise expect rejection.

**Relative Volume:**
- HIGH_VOLUME (>1.5x average): Institutional interest — confirms moves. Bullish if price is up, bearish if price is down.
- LOW_VOLUME (<0.5x average): Lack of conviction — moves are less reliable.

### Analyst Consensus (Finnhub)
- >80% bullish (Buy + Strong Buy): Wall Street is overwhelmingly positive — supports buy thesis but watch for crowded trade risk.
- 50-80% bullish: Moderate consensus — acceptable.
- <50% bullish: Street is skeptical — require a specific catalyst that the consensus hasn't priced in.
- **Price target vs current price**: If current price is already above mean price target, the "easy" upside is gone. Require a re-rating catalyst.
- **Price target gap**: If mean PT is >20% above current price, there's meaningful upside if the thesis plays out.
```

- [ ] **Step 2: Commit**

```bash
git add analyst_guidance.md
git commit -m "docs: add signal interpretation for technicals and analyst consensus"
```

---

## Task 10: Update Polygon Integration for Full Article Text

**Files:**
- Modify: `src/scorched/services/research.py` — `_fetch_polygon_news_sync`

- [ ] **Step 1: Update Polygon news fetch to include article descriptions**

In `src/scorched/services/research.py`, update `_fetch_polygon_news_sync` to also return article descriptions (the `description` field in Polygon's response). This is available on paid tiers and provides article summaries instead of just headlines.

Replace the `_fetch_polygon_news_sync` function:

```python
def _fetch_polygon_news_sync(symbols: list[str], api_key: str, limit_per_symbol: int = 5) -> dict:
    """
    Fetch recent news from Polygon.io for each symbol.
    Returns {symbol: [{"title": ..., "description": ...}, ...]}
    On free tier, description may be empty. On paid tier, it contains article summary.
    yfinance news remains as fallback in build_research_context().
    """
    import requests
    if not api_key:
        return {}
    result = {}
    base = "https://api.polygon.io/v2/reference/news"
    for symbol in symbols:
        try:
            resp = requests.get(
                base,
                params={"ticker": symbol, "limit": limit_per_symbol, "apiKey": api_key},
                timeout=10,
            )
            resp.raise_for_status()
            articles = resp.json().get("results", [])
            result[symbol] = [
                {
                    "title": a.get("title", ""),
                    "description": a.get("description", ""),
                }
                for a in articles
                if a.get("title")
            ]
        except Exception:
            result[symbol] = []
    return result
```

- [ ] **Step 2: Update news rendering in build_research_context**

In `build_research_context`, update the news section to show descriptions when available. Replace the news block:

```python
        # News — prefer Polygon (with descriptions if available); fall back to yfinance
        poly_articles = (polygon_news or {}).get(symbol, [])
        yf_headlines = news_data.get(symbol, [])
        if poly_articles:
            lines.append("  News:")
            for a in poly_articles[:3]:
                if isinstance(a, dict):
                    title = a.get("title", "")
                    desc = a.get("description", "")
                    if desc:
                        lines.append(f"    - {title}: {desc[:150]}")
                    else:
                        lines.append(f"    - {title}")
                else:
                    lines.append(f"    - {a}")
        elif yf_headlines:
            lines.append("  News:")
            for h in yf_headlines[:3]:
                lines.append(f"    - {h}")
```

- [ ] **Step 3: Run all tests**

Run: `cd /home/ubuntu/tradebot && python3 -m pytest tests/ -v 2>&1 | tail -10`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add src/scorched/services/research.py
git commit -m "feat: upgrade Polygon news to include article descriptions for richer context"
```

---

## Task 11: Update Documentation and Rebuild

**Files:**
- Modify: `CLAUDE.md`
- Modify: `.env.example`
- Modify: `strategy.json` (no changes needed — circuit_breaker section already exists)

- [ ] **Step 1: Update .env.example with Finnhub key**

Add after the `FRED_API_KEY=` line:

```
FINNHUB_API_KEY=
```

- [ ] **Step 2: Update CLAUDE.md**

Add to the Data Sources table:

```
| Finnhub | Analyst consensus ratings, price targets, recommendation trends | `FINNHUB_API_KEY` |
```

Add to the Key Files table:

```
| `src/scorched/services/technicals.py` | MACD, Bollinger Bands, MA crossover, S/R, volume profile calculations |
| `src/scorched/services/finnhub_data.py` | Analyst consensus and price targets from Finnhub |
| `src/scorched/services/risk_review.py` | Call 3: Adversarial risk committee review |
| `src/scorched/services/position_mgmt.py` | Call 4: EOD position management review |
```

Update the Claude Pipeline section to reflect 4 calls:

```markdown
## Claude Pipeline (recommender.py)

Four API calls per day, all using `claude-sonnet-4-6`:

**Call 1 — Analysis** (extended thinking, budget=16000 tokens):
- System: `ANALYSIS_SYSTEM` — analyst persona, strategy injected
- Input: market context + full research context (all data sources including technicals + analyst consensus)
- Output: `{"analysis": "...", "candidates": ["TICK1", ...]}`

**Call 2 — Decision** (standard, no extended thinking):
- System: `DECISION_SYSTEM` — trader persona, strategy + playbook injected
- Input: analysis text + options data for candidates + current portfolio
- Output: `{"research_summary": "...", "recommendations": [...]}`

**Call 3 — Risk Committee** (standard, no extended thinking):
- System: `RISK_REVIEW_SYSTEM` — skeptical risk reviewer, default-reject stance
- Input: proposed recommendations + portfolio + analysis summary + recent playbook
- Output: `{"decisions": [{"symbol": ..., "verdict": "approve"|"reject", ...}]}`
- Rejected buys are removed before saving. Sells always pass through.

**Call 4 — Position Management** (EOD, standard):
- System: `POSITION_MGMT_SYSTEM` — conservative position reviewer
- Input: all open positions + today's market summary
- Output: per-position hold/tighten/partial/exit recommendations (logged)
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md .env.example
git commit -m "docs: update documentation for Tier 2 improvements (4-call pipeline, new data sources)"
```

- [ ] **Step 4: Rebuild Docker container**

Run: `cd /home/ubuntu/tradebot && docker compose up -d --build --force-recreate tradebot`

- [ ] **Step 5: Install finnhub-python in Docker (handled by pyproject.toml)**

The `docker compose up --build` will reinstall dependencies from the updated `pyproject.toml`. Verify:

Run: `docker compose exec tradebot python -c "import finnhub; print('Finnhub OK')"`
Expected: `Finnhub OK`

---

## Summary

| Task | What it adds | Daily cost impact |
|------|-------------|-------------------|
| 1 | Technical analysis (MACD, BB, MA, S/R, volume) | $0.00 (compute only) |
| 2 | Price history arrays for technicals | $0.00 |
| 3 | Wire technicals into research context | $0.00 |
| 4 | Finnhub analyst consensus + price targets | $0.00 (free tier) |
| 5 | Wire Finnhub into research pipeline | $0.00 |
| 6 | Risk committee (Call 3) — adversarial review | ~$0.02/day |
| 7 | Thinking budget 8K→16K + universe 40→60+30 | ~$0.03/day |
| 8 | Position management (Call 4) — EOD review | ~$0.02/day |
| 9 | Signal interpretation docs for new data | $0.00 |
| 10 | Polygon full article text | $0.00 (data tier upgrade is external) |
| 11 | Documentation + rebuild | $0.00 |

**Total new daily cost: ~$0.07/day added → ~$0.15/day total (up from $0.08)**

The cost is lower than the $0.35/day Tier 2 estimate because we're using Sonnet for all calls. The estimate included buffer for larger contexts from the expanded universe. Actual cost will settle after a few days of real usage.
