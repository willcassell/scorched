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
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from common import load_env, send_telegram, now_et

load_env()

RECS_FILE = "/tmp/tradebot_recommendations.json"
FILTERED_FILE = "/tmp/tradebot_recommendations.json"  # overwrites in place


def main():
    now_est, today_str = now_et()

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

    # Import circuit breaker (needs project src/ on sys.path)
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

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
