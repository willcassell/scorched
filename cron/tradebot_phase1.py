#!/usr/bin/env python3
"""
Phase 1 — Claude analysis (9:45 AM ET, Mon-Fri)

Calls tradebot to generate today's trade recommendations and sends them via Telegram.
Writes /tmp/tradebot_recommendations.json for Phase 2 to consume.

Runs 10 min after market open so Claude sees real opening data (gaps, volume,
early sentiment) from Phase 0's post-open cache.

Requirements: pip3 install pytz
Environment:  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
              TRADEBOT_URL (optional, defaults to http://localhost:8000)
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_env, http_get, http_post, send_telegram, fmt_pct, now_et, acquire_lock, release_lock, check_expected_hour

load_env()

# Host-side logs dir — cron runs on the VM, not in the container.
# The container sees the same directory mounted at /app/logs.
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)
RECS_FILE = str(LOGS_DIR / "tradebot_recommendations.json")


def main():
    now_est, today_str = now_et()
    check_expected_hour(9, "Phase 1")

    print(f"[{now_est.strftime('%Y-%m-%d %H:%M:%S %Z')}] Phase 1: generating recommendations for {today_str}")

    try:
        session = http_post("/api/v1/recommendations/generate", {"session_date": today_str}, timeout=600)
    except Exception as e:
        msg = f"TRADEBOT // {today_str} - Phase 1 failed\nError: {e}"
        send_telegram(msg)
        print(f"Error: {e}")
        return

    if session.get("market_closed"):
        send_telegram(f"TRADEBOT // {today_str} - Market closed. No recommendations.")
        print("Market closed.")
        return

    recs = session.get("recommendations", [])
    if not recs:
        send_telegram(f"TRADEBOT // {today_str} - No action taken. Analysis complete, no trades met criteria.")
        print("No recommendations.")
        return

    # Fetch portfolio for current balance
    try:
        portfolio = http_get("/api/v1/portfolio")
        total = float(portfolio.get("total_value", 0))
        ret_pct = portfolio.get("all_time_return_pct", 0)
        balance_line = f"Portfolio: ${total:,.2f} ({fmt_pct(ret_pct)})"
    except Exception:
        balance_line = "Portfolio: unavailable"

    msg = f"TRADEBOT // {today_str} - Pre-market\n"
    msg += f"{balance_line}\n\n"
    msg += "Today's Picks (pending open):\n"
    symbols = []
    for r in recs:
        action = r["action"].upper()
        qty = float(r["quantity"])
        price = float(r["suggested_price"])
        msg += f"  {action} {r['symbol']} - {qty:.0f}sh @ ${price:.2f}\n"
        symbols.append(r["symbol"])

    summary = session.get("research_summary", "")
    if summary:
        msg += f"\n{summary[:300]}"

    send_telegram(msg)

    # Write JSON for Phase 2 (atomic write: tempfile + rename)
    payload = {"date": today_str, "recommendations": recs, "symbols": symbols, "status": "complete"}
    fd, tmp_path = tempfile.mkstemp(dir=str(LOGS_DIR), suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.rename(tmp_path, RECS_FILE)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    print(f"Wrote {RECS_FILE} with {len(recs)} recommendations.")


if __name__ == "__main__":
    acquire_lock("phase1")
    try:
        main()
    except Exception as e:
        try:
            from common import send_telegram
            send_telegram(f"TRADEBOT // Phase 1 CRASHED\n{type(e).__name__}: {str(e)[:300]}")
        except Exception:
            pass
        raise
    finally:
        release_lock("phase1")
