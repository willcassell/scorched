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
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_env, http_get, http_post, send_telegram, fmt_pct, now_et, acquire_lock, release_lock, check_expected_hour

load_env()

# Phase 1 normally runs ~3-4 min end-to-end (analysis ~3m, decision+risk ~30s each).
# Alert if it takes noticeably longer so we catch drift before it becomes a timeout.
PHASE1_SLOW_THRESHOLD_S = 420

# Host-side logs dir — cron runs on the VM, not in the container.
# The container sees the same directory mounted at /app/logs.
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)
RECS_FILE = str(LOGS_DIR / "tradebot_recommendations.json")


def _write_recs_file(payload: dict) -> None:
    """Atomic write: tempfile + rename so a partial file never appears."""
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


def main():
    now_est, today_str = now_et()
    check_expected_hour(9, "Phase 1")

    print(f"[{now_est.strftime('%Y-%m-%d %H:%M:%S %Z')}] Phase 1: generating recommendations for {today_str}")

    t0 = time.monotonic()
    try:
        session = http_post("/api/v1/recommendations/generate", {"session_date": today_str}, timeout=900)
    except Exception as e:
        elapsed = time.monotonic() - t0
        msg = (
            f"TRADEBOT // {today_str} - 🚨 Phase 1 ERRORED after {elapsed:.0f}s\n"
            f"Exception: {type(e).__name__}: {str(e)[:300]}\n"
            f"This is NOT a 'no trades today' signal — Claude never responded. "
            f"Check docker logs for a traceback."
        )
        send_telegram(msg)
        print(f"Error: {e}")
        return

    elapsed = time.monotonic() - t0
    if elapsed > PHASE1_SLOW_THRESHOLD_S:
        send_telegram(
            f"TRADEBOT // {today_str} - ⚠️ Phase 1 slow: {elapsed:.0f}s "
            f"(normal <{PHASE1_SLOW_THRESHOLD_S}s). "
            f"Investigate before this becomes a timeout."
        )

    if session.get("market_closed"):
        send_telegram(f"TRADEBOT // {today_str} - Market closed. No recommendations.")
        print("Market closed.")
        return

    recs = session.get("recommendations", [])
    summary = (session.get("research_summary") or "").strip()
    if not recs:
        # Two very different empty paths: (a) Claude evaluated and rejected
        # all setups — research_summary has substance; (b) something failed
        # internally and the server returned an empty shell. Collapsing them
        # to one message loses the second signal.
        if len(summary) >= 80:
            msg = (
                f"TRADEBOT // {today_str} - No action taken.\n"
                f"Claude evaluated and no trades met criteria.\n\n"
                f"Summary: {summary[:3500]}"
            )
        else:
            msg = (
                f"TRADEBOT // {today_str} - ⚠️ Empty recommendations with no analysis\n"
                f"Claude returned no picks AND no meaningful summary "
                f"({len(summary)} chars). This is unusual — treat as a likely "
                f"internal failure (JSON parse, validation, gate rejection). "
                f"Check docker logs before assuming it was a deliberate hold day."
            )
        send_telegram(msg)
        # Write today's empty payload so Phase 1.5/Phase 2 don't pick up
        # yesterday's stale file and fire false "date mismatch" alerts.
        _write_recs_file({"date": today_str, "recommendations": [], "symbols": [], "status": "complete"})
        print("No recommendations." if summary else "No recs and no summary — suspicious.")
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
        # Use whatever budget is left under Telegram's 4096 char limit,
        # keeping a 64 char cushion for formatting and any trailing lines.
        budget = max(0, 4096 - len(msg) - 64)
        msg += f"\n{summary[:budget]}"

    send_telegram(msg)

    payload = {"date": today_str, "recommendations": recs, "symbols": symbols, "status": "complete"}
    _write_recs_file(payload)
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
