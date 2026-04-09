#!/usr/bin/env python3
"""
Order Reconciliation — runs ~30 min after Phase 2 (10:45 AM ET, Mon-Fri)

Checks pending Alpaca orders for fills and records them in the local DB.
Sends a Telegram summary of what was filled and what wasn't.
Safe to call multiple times — already-reconciled orders are skipped.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_env, http_post, send_telegram, now_et, acquire_lock, release_lock, check_expected_hour

load_env()


def main():
    now_est, today_str = now_et()
    check_expected_hour(10, "Reconcile")

    print(f"[{now_est.strftime('%Y-%m-%d %H:%M:%S %Z')}] Reconciling pending orders for {today_str}")

    try:
        result = http_post("/api/v1/trades/reconcile", {})
        count = result.get("reconciled", 0)
        results = result.get("results", [])

        if count == 0:
            print("No pending orders to reconcile.")
            return

        for r in results:
            status = r.get("status", "unknown")
            symbol = r.get("symbol", "???")
            action = r.get("action", "???").upper()
            if status == "filled":
                print(f"  {action} {symbol}: FILLED {r.get('filled_qty')}sh @ ${r.get('filled_price')}")
            else:
                print(f"  {action} {symbol}: {status}")

        print(f"Reconciliation complete — {count} orders checked.")

    except Exception as e:
        msg = f"TRADEBOT // Reconciliation FAILED\n{type(e).__name__}: {str(e)[:300]}"
        print(msg)
        send_telegram(msg)
        raise


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
