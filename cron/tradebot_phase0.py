#!/usr/bin/env python3
"""
Phase 0 — Data prefetch (7:30 AM ET, Mon-Fri)

Fetches all external research data (yfinance, FRED, Polygon, Finnhub, etc.)
and caches it for Phase 1. No LLM calls — zero Claude cost.

Phase 1 at 8:30 AM loads the cache and skips straight to Claude analysis.
If Phase 0 fails, Phase 1 falls back to inline fetching.

Environment:  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
              TRADEBOT_URL (optional, defaults to http://localhost:8000)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_env, http_post, send_telegram, now_et, acquire_lock, release_lock, check_expected_hour

load_env()


def main():
    now_est, today_str = now_et()
    check_expected_hour(7, "Phase 0")

    print(f"[{now_est.strftime('%Y-%m-%d %H:%M:%S %Z')}] Phase 0: prefetching research data for {today_str}")

    try:
        result = http_post("/api/v1/research/prefetch", {}, timeout=3600)
    except Exception as e:
        msg = (
            f"TRADEBOT // {today_str} - Phase 0 FAILED\n"
            f"Data prefetch error: {e}\n"
            f"Phase 1 will fall back to inline fetching."
        )
        send_telegram(msg)
        print(f"Error: {e}")
        return

    timing = result.get("timing", {})
    n_symbols = result.get("research_symbols", 0)
    screener = result.get("screener_symbols", [])
    total_s = timing.get("total", 0)

    # Build timing breakdown for Telegram
    timing_lines = []
    for step, elapsed in sorted(timing.items(), key=lambda x: -x[1]):
        if step == "total":
            continue
        timing_lines.append(f"  {step}: {elapsed:.0f}s")

    msg = (
        f"TRADEBOT // {today_str} - Phase 0 complete ({total_s:.0f}s)\n"
        f"Research universe: {n_symbols} symbols\n"
        f"Screener picks: {', '.join(screener[:10])}\n\n"
        f"Timing:\n" + "\n".join(timing_lines)
    )
    send_telegram(msg)
    print(f"Phase 0 complete in {total_s:.0f}s for {n_symbols} symbols.")


if __name__ == "__main__":
    acquire_lock("phase0")
    try:
        main()
    except Exception as e:
        try:
            from common import send_telegram
            send_telegram(f"TRADEBOT // Phase 0 CRASHED\n{type(e).__name__}: {str(e)[:300]}")
        except Exception:
            pass
        raise
    finally:
        release_lock("phase0")
