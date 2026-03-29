#!/usr/bin/env python3
"""
Phase 1 — Pre-market (8:30 AM ET, Mon-Fri)

Calls tradebot to generate today's trade recommendations and sends them via Telegram.
Writes /tmp/tradebot_recommendations.json for Phase 2 to consume.

Requirements: pip3 install pytz
Environment:  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
              TRADEBOT_URL (optional, defaults to http://localhost:8000)
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_env, http_get, http_post, send_telegram, fmt_pct, now_et

load_env()

RECS_FILE = "/tmp/tradebot_recommendations.json"


def main():
    now_est, today_str = now_et()

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

    # Write JSON for Phase 2
    with open(RECS_FILE, "w") as f:
        json.dump({"date": today_str, "recommendations": recs, "symbols": symbols}, f)
    print(f"Wrote {RECS_FILE} with {len(recs)} recommendations.")


if __name__ == "__main__":
    main()
