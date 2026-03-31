#!/usr/bin/env python3
"""
Intraday Monitor — runs every 5 minutes during market hours.

Checks held positions against configurable triggers. Only calls Claude
(via POST /api/v1/intraday/evaluate) when a trigger fires. Zero LLM
cost on quiet days.

Cron: */5 13-19 * * 1-5  (script self-gates on ET market hours)
"""
import json
import os
import socket
import sys
import tempfile
import time
from datetime import time as dt_time
from decimal import Decimal
from pathlib import Path

socket.setdefaulttimeout(30)

# Add cron directory to path for common module
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_env, http_get, http_post, send_telegram, now_et, acquire_lock, release_lock

load_env()

# Add src/ to path for intraday trigger functions
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from scorched.intraday import check_intraday_triggers, check_market_triggers

COOLDOWN_FILE = "/tmp/intraday_cooldown.json"


def is_market_hours(now_est) -> bool:
    """Return True if within 9:35 AM - 3:55 PM ET."""
    t = now_est.time()
    return dt_time(9, 35) <= t <= dt_time(15, 55)


def load_cooldowns() -> dict:
    if not os.path.exists(COOLDOWN_FILE):
        return {}
    try:
        with open(COOLDOWN_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_cooldowns(cooldowns: dict) -> None:
    fd, tmp_path = tempfile.mkstemp(dir="/tmp", prefix="tradebot_cooldown_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cooldowns, f)
        os.rename(tmp_path, COOLDOWN_FILE)
    except Exception:
        os.unlink(tmp_path)
        raise


def is_on_cooldown(symbol: str, cooldowns: dict, cooldown_minutes: int) -> bool:
    last_trigger = cooldowns.get(symbol, 0)
    return (time.time() - last_trigger) < cooldown_minutes * 60


def fetch_position_data(symbols: list[str]) -> dict:
    """Fetch current prices, today's OHLV, and 20d avg volume via yfinance."""
    import yfinance as yf

    data = {}
    all_symbols = list(set(symbols + ["SPY", "^VIX"]))

    for sym in all_symbols:
        try:
            ticker = yf.Ticker(sym)
            hist_1d = ticker.history(period="1d")
            hist_1mo = ticker.history(period="1mo")

            if hist_1d.empty:
                continue

            current_price = float(hist_1d["Close"].iloc[-1])
            today_open = float(hist_1d["Open"].iloc[-1])
            today_high = float(hist_1d["High"].iloc[-1])
            today_low = float(hist_1d["Low"].iloc[-1])
            today_volume = float(hist_1d["Volume"].iloc[-1])

            avg_volume_20d = 0.0
            if not hist_1mo.empty and len(hist_1mo) >= 2:
                avg_volume_20d = float(hist_1mo["Volume"].iloc[:-1].tail(20).mean())

            data[sym] = {
                "current_price": current_price,
                "today_open": today_open,
                "today_high": today_high,
                "today_low": today_low,
                "today_volume": today_volume,
                "avg_volume_20d": avg_volume_20d,
            }
        except Exception as e:
            print(f"  Fetch failed for {sym}: {e}")

    return data


def main():
    now_est, today_str = now_et()

    if not is_market_hours(now_est):
        return

    print(f"[{now_est.strftime('%H:%M:%S')}] Intraday monitor check")

    # Get held positions
    try:
        portfolio = http_get("/api/v1/portfolio")
    except Exception as e:
        print(f"  Portfolio fetch failed: {e}")
        return

    positions = portfolio.get("positions", [])
    if not positions:
        return

    # Load strategy config
    strategy_path = Path(__file__).resolve().parent.parent / "strategy.json"
    try:
        strategy = json.loads(strategy_path.read_text())
    except (OSError, json.JSONDecodeError):
        strategy = {}

    config = strategy.get("intraday_monitor", {})
    if not config.get("enabled", True):
        return

    cooldown_minutes = config.get("cooldown_minutes", 30)
    cooldowns = load_cooldowns()

    # Batch fetch market data
    held_symbols = [p["symbol"] for p in positions]
    print(f"  Checking {len(held_symbols)} positions: {held_symbols}")
    data = fetch_position_data(held_symbols)

    if not data:
        print("  No market data available")
        return

    # Market-level triggers
    spy_data = data.get("SPY", {})
    vix_data = data.get("^VIX", {})
    market_triggers = check_market_triggers(
        spy_current=Decimal(str(spy_data.get("current_price", 0))),
        spy_open=Decimal(str(spy_data.get("today_open", 0))),
        vix_current=Decimal(str(vix_data.get("current_price", 0))),
        config=config,
    )

    if market_triggers:
        reasons = [t.reason for t in market_triggers]
        print(f"  Market triggers fired: {reasons}")

    # Per-position triggers
    triggered_positions = []
    for pos in positions:
        symbol = pos["symbol"]

        if is_on_cooldown(symbol, cooldowns, cooldown_minutes):
            continue

        sym_data = data.get(symbol)
        if not sym_data:
            continue

        triggers = check_intraday_triggers(
            current_price=Decimal(str(sym_data["current_price"])),
            entry_price=Decimal(str(pos["avg_cost_basis"])),
            today_open=Decimal(str(sym_data["today_open"])),
            current_volume=sym_data["today_volume"],
            avg_volume_20d=sym_data["avg_volume_20d"],
            market_triggers=market_triggers,
            config=config,
        )

        if triggers:
            triggered_positions.append({
                "symbol": symbol,
                "trigger_reasons": [t.reason for t in triggers],
                "current_price": sym_data["current_price"],
                "entry_price": float(pos["avg_cost_basis"]),
                "today_open": sym_data["today_open"],
                "today_high": sym_data["today_high"],
                "today_low": sym_data["today_low"],
                "days_held": pos.get("days_held", 0),
                "shares": float(pos["shares"]),
                "original_reasoning": "",
            })
            cooldowns[symbol] = time.time()

    if not triggered_positions:
        print("  All clear — no triggers fired")
        return

    print(f"  TRIGGERS FIRED for {[t['symbol'] for t in triggered_positions]}")
    save_cooldowns(cooldowns)

    # Call the evaluate endpoint
    spy_change_pct = 0.0
    if spy_data.get("today_open") and spy_data.get("current_price"):
        spy_change_pct = (spy_data["current_price"] - spy_data["today_open"]) / spy_data["today_open"] * 100

    try:
        result = http_post("/api/v1/intraday/evaluate", {
            "triggers": triggered_positions,
            "market_context": {
                "spy_change_pct": round(spy_change_pct, 2),
                "vix_current": vix_data.get("current_price", 0),
            },
        }, timeout=120)
    except Exception as e:
        msg = f"INTRADAY ALERT - Trigger evaluation failed\nTriggered: {[t['symbol'] for t in triggered_positions]}\nError: {e}"
        send_telegram(msg)
        print(f"  Evaluate failed: {e}")
        return

    # Send Telegram for each decision
    for decision in result.get("decisions", []):
        symbol = decision["symbol"]
        action = decision["action"]
        reasoning = decision["reasoning"]

        if action in ("exit_full", "exit_partial"):
            trade = decision.get("trade_result") or {}
            shares = trade.get("shares", "?")
            price = trade.get("execution_price", "?")
            gain = trade.get("realized_gain", 0)
            gain_sign = "+" if gain >= 0 else ""
            msg = (
                f"INTRADAY EXIT: {symbol}\n"
                f"Sold {shares}sh @ ${price}\n"
                f"Realized: {gain_sign}${gain:,.2f}\n"
                f"Reason: {reasoning}"
            )
        else:
            triggers = [t for t in triggered_positions if t["symbol"] == symbol]
            trigger_reasons = triggers[0]["trigger_reasons"] if triggers else []
            msg = (
                f"INTRADAY ALERT: {symbol} — HOLD\n"
                f"Triggers: {', '.join(trigger_reasons)}\n"
                f"Claude says: {reasoning}"
            )

        send_telegram(msg)
        print(f"  {symbol}: {action} — {reasoning[:80]}")

    print(f"  Intraday check complete: {len(result.get('decisions', []))} decisions")


if __name__ == "__main__":
    acquire_lock("intraday")
    try:
        main()
    except Exception as e:
        try:
            from common import send_telegram
            send_telegram(f"TRADEBOT // Intraday Monitor CRASHED\n{type(e).__name__}: {str(e)[:300]}")
        except Exception:
            pass
        raise
    finally:
        release_lock("intraday")
