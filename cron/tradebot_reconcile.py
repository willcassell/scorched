#!/usr/bin/env python3
"""
Order Reconciliation + Position Sync — runs twice per session:
  10:45 AM ET (Phase 2.5) — 30 min after Phase 2 submission
  2:00 PM ET (Phase 2.75) — catches fills that happened after the first check
                            and alerts if anything is still open going into
                            the last 2 hours of the session

Two steps:
1. Reconcile: Check pending Alpaca orders for fills and record them in the local DB.
2. Sync: Compare local DB positions against Alpaca holdings. Auto-correct mismatches
   (Alpaca is source of truth). Send Telegram alert on any corrections.

Safe to call multiple times — already-reconciled orders are skipped,
and sync is idempotent (no-op if already in sync).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_env, http_post, send_telegram, now_et, acquire_lock, release_lock, check_expected_hour

load_env()


def main():
    now_est, today_str = now_et()
    check_expected_hour((10, 14), "Reconcile")
    is_afternoon = now_est.hour >= 13
    label = "Phase 2.75 (afternoon)" if is_afternoon else "Phase 2.5 (morning)"

    print(f"[{now_est.strftime('%Y-%m-%d %H:%M:%S %Z')}] {label} — reconciling pending orders for {today_str}")

    # Step 1: Reconcile pending fills
    filled = []
    stuck = []
    try:
        result = http_post("/api/v1/trades/reconcile", {})
        count = result.get("reconciled", 0)
        results = result.get("results", [])

        if count == 0:
            print("No pending orders to reconcile.")
        else:
            for r in results:
                status = r.get("status", "unknown")
                symbol = r.get("symbol", "???")
                action = r.get("action", "???").upper()
                if status == "filled":
                    print(f"  {action} {symbol}: FILLED {r.get('filled_qty')}sh @ ${r.get('filled_price')}")
                    filled.append(r)
                else:
                    print(f"  {action} {symbol}: {status}")
                    if status.startswith("still_open"):
                        stuck.append(r)
            print(f"Reconciliation complete — {count} orders checked.")

        # The afternoon run is the last automated chance to notice an exit
        # that hasn't happened. Alert loudly on stuck sells — a swing-trader
        # who meant to be flat before earnings needs to see this before 4pm.
        if is_afternoon and stuck:
            lines = [f"  {r['action'].upper()} {r['symbol']} — {r['status']}" for r in stuck]
            send_telegram(
                "TRADEBOT // ⚠️ Orders still open at 2pm ET\n"
                "These limits have not filled and will expire at close:\n"
                + "\n".join(lines)
                + "\nConsider manual intervention if the exit is time-sensitive (earnings, thesis break)."
            )

    except Exception as e:
        msg = f"TRADEBOT // Reconciliation FAILED ({label})\n{type(e).__name__}: {str(e)[:300]}"
        print(msg)
        send_telegram(msg)
        raise

    # Step 2: Position sync (Alpaca → local DB)
    try:
        sync = http_post("/api/v1/broker/sync", {})
        sync_status = sync.get("status", "unknown")
        corrections = sync.get("corrections", [])

        if sync_status == "in_sync":
            print("Position sync: all positions match Alpaca.")
        elif sync_status == "skipped":
            print(f"Position sync: skipped ({sync.get('reason', 'N/A')})")
        else:
            print(f"Position sync: {len(corrections)} correction(s) applied.")
            msg = f"TRADEBOT // Position Sync — {len(corrections)} correction(s)\n"
            for c in corrections:
                line = f"  {c['symbol']}: {c['action']} — {c['detail']}"
                print(line)
                msg += line + "\n"
            send_telegram(msg)

    except Exception as e:
        msg = f"TRADEBOT // Position Sync FAILED\n{type(e).__name__}: {str(e)[:300]}"
        print(msg)
        send_telegram(msg)


if __name__ == "__main__":
    acquire_lock("reconcile")
    try:
        main()
    except Exception as e:
        try:
            send_telegram(f"TRADEBOT // Reconcile CRASHED\n{type(e).__name__}: {str(e)[:300]}")
        except Exception:
            pass
        raise
    finally:
        release_lock("reconcile")
