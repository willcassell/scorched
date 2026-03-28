#!/usr/bin/env python3
"""
Phase 1.5 — Circuit breaker gate (9:30 AM ET, Mon-Fri)

Reads Phase 1's recommendations JSON, runs circuit breaker checks,
filters out any buys that fail gate checks, writes a filtered
recommendations file for Phase 2, and sends gate results via Telegram.

Environment:  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
              TRADEBOT_URL (optional, defaults to http://localhost:8000)
"""
import json
import os
import pathlib
import urllib.request
import datetime
import pytz

# Load .env from project root
_env_file = pathlib.Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
RECS_FILE = "/tmp/tradebot_recommendations.json"
FILTERED_FILE = "/tmp/tradebot_recommendations.json"  # overwrites in place


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram env vars not set — skipping notification")
        return
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"Telegram sent: {resp.read().decode()[:120]}")
    except Exception as e:
        print(f"Telegram error: {e}")


def main():
    est_tz = pytz.timezone("America/New_York")
    now_est = datetime.datetime.now(est_tz)
    today_str = now_est.date().strftime("%Y-%m-%d")

    print(f"[{now_est.strftime('%Y-%m-%d %H:%M:%S %Z')}] Phase 1.5: circuit breaker for {today_str}")

    if not os.path.exists(RECS_FILE):
        print("No recommendations file found — nothing to gate.")
        return

    with open(RECS_FILE) as f:
        stored = json.load(f)

    if stored["date"] != today_str:
        print(f"Date mismatch: {stored['date']} != {today_str}")
        return

    recs = stored["recommendations"]
    if not recs:
        print("No recommendations to gate.")
        return

    # Import circuit breaker (needs project on sys.path)
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

    import asyncio
    from scorched.circuit_breaker import run_circuit_breaker
    from scorched.services.strategy import load_strategy

    strategy = load_strategy()
    cb_config = strategy.get("circuit_breaker", {"enabled": False})

    if not cb_config.get("enabled", False):
        print("Circuit breaker disabled — passing all recommendations through.")
        return

    # Run gate checks
    results = asyncio.run(run_circuit_breaker(recs, cb_config))

    passed = []
    blocked = []
    for rec in results:
        gate = rec.pop("gate_result")
        if gate.passed:
            passed.append(rec)
        else:
            blocked.append((rec, gate.reason))

    # Build Telegram message
    msg = f"TRADEBOT // {today_str} - Circuit Breaker\n"

    if blocked:
        msg += "\nBLOCKED:\n"
        for rec, reason in blocked:
            msg += f"  {rec['action'].upper()} {rec['symbol']} — {reason}\n"

    if passed:
        msg += "\nCLEARED:\n"
        for rec in passed:
            msg += f"  {rec['action'].upper()} {rec['symbol']}\n"
    else:
        msg += "\nAll buys blocked — no trades will execute.\n"

    send_telegram(msg)

    # Write filtered file for Phase 2
    stored["recommendations"] = passed
    stored["symbols"] = [r["symbol"] for r in passed]
    with open(FILTERED_FILE, "w") as f:
        json.dump(stored, f)

    print(f"Phase 1.5 complete: {len(passed)} passed, {len(blocked)} blocked.")


if __name__ == "__main__":
    main()
