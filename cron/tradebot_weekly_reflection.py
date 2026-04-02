#!/usr/bin/env python3
"""
Weekly Reflection — Sunday evening (6:00 PM ET)

Reviews the past week's trades, compares predictions vs outcomes,
and appends learnings to the playbook. One Claude call (~$0.02).

Environment:  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
              TRADEBOT_URL (optional, defaults to http://localhost:8000)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_env, http_post, send_telegram, now_et, acquire_lock, release_lock

load_env()


def main():
    now_est, today_str = now_et()

    print(f"[{now_est.strftime('%Y-%m-%d %H:%M:%S %Z')}] Weekly reflection for week ending {today_str}")

    try:
        result = http_post("/api/v1/market/weekly-reflection", {}, timeout=120)
    except Exception as e:
        msg = (
            f"TRADEBOT // {today_str} - Weekly Reflection FAILED\n"
            f"Error: {e}"
        )
        send_telegram(msg)
        print(f"Error: {e}")
        return

    reflection = result.get("reflection", {})
    grade = reflection.get("grade", "N/A")
    sells = result.get("sells_reviewed", 0)
    buys = result.get("buys_reviewed", 0)
    skipped = result.get("skipped_recs_reviewed", 0)
    learnings = reflection.get("learnings", [])
    pattern = reflection.get("pattern_detected", "none")
    adjustment = reflection.get("strategy_adjustment", "none needed")

    # Build Telegram message
    learnings_text = "\n".join(f"  - {l}" for l in learnings[:5])
    msg = (
        f"TRADEBOT // {today_str} - Weekly Reflection (Grade: {grade})\n"
        f"Reviewed: {sells} sells, {buys} buys, {skipped} skipped recs\n\n"
        f"Learnings:\n{learnings_text}\n\n"
        f"Pattern: {pattern}\n"
        f"Adjustment: {adjustment}"
    )
    send_telegram(msg)
    print(f"Weekly reflection complete. Grade: {grade}")


if __name__ == "__main__":
    acquire_lock("weekly_reflection")
    try:
        main()
    except Exception as e:
        try:
            from common import send_telegram
            send_telegram(f"TRADEBOT // Weekly Reflection CRASHED\n{type(e).__name__}: {str(e)[:300]}")
        except Exception:
            pass
        raise
    finally:
        release_lock("weekly_reflection")
