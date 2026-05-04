#!/usr/bin/env python3
"""
Phase 2 — Execute trades (10:15 AM ET, Mon-Fri)

Reads Phase 1's recommendations JSON (filtered by Phase 1.5 circuit breaker),
fetches current prices, confirms each trade via Alpaca, then sends a
fill report via Telegram. Deletes the recommendations JSON when done.

Runs 45 min after open to avoid opening range volatility.

Requirements: pip3 install pytz
Environment:  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
              TRADEBOT_URL (optional, defaults to http://localhost:8000)
"""
import json
import os
import urllib.parse
import urllib.error
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_env, http_get, http_post, send_telegram, fmt_pct, now_et, acquire_lock, release_lock, check_expected_hour

load_env()

# Host-side logs dir — cron runs on the VM, not in the container.
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
GATED_FILE = str(LOGS_DIR / "tradebot_recommendations_gated.json")
ORIGINAL_FILE = str(LOGS_DIR / "tradebot_recommendations.json")


def _cleanup_recs_file(path):
    """Remove the recommendations file, ignoring if already gone.

    Also clears the *other* file (gated vs original) — Phase 2 only reads
    the preferred one, so the unused sibling can sit around as a stale
    leftover that confuses the next session's date-mismatch check.
    """
    for p in (path, GATED_FILE, ORIGINAL_FILE):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def main():
    now_est, today_str = now_et()
    check_expected_hour(10, "Phase 2")

    print(f"[{now_est.strftime('%Y-%m-%d %H:%M:%S %Z')}] Phase 2: confirming trades for {today_str}")

    # Prefer gated file (Phase 1.5 output); fall back to original (circuit breaker disabled/not run)
    if os.path.exists(GATED_FILE):
        recs_file = GATED_FILE
    elif os.path.exists(ORIGINAL_FILE):
        recs_file = ORIGINAL_FILE
    else:
        send_telegram(f"TRADEBOT // {today_str} - Phase 2 skipped: no Phase 1 data found.")
        print("No recommendations file found.")
        return

    with open(recs_file) as f:
        stored = json.load(f)

    if stored["date"] != today_str:
        send_telegram(
            f"TRADEBOT // {today_str} - Phase 2 skipped: "
            f"recommendations are for {stored['date']}, not today."
        )
        _cleanup_recs_file(recs_file)
        print(f"Date mismatch: {stored['date']} != {today_str}")
        return

    if stored.get("status") != "complete":
        send_telegram(
            f"TRADEBOT // {today_str} - Phase 2 skipped: "
            f"Phase 1 did not complete successfully (status={stored.get('status')})"
        )
        _cleanup_recs_file(recs_file)
        print(f"Phase 1 incomplete: status={stored.get('status')}")
        return

    recs = stored["recommendations"]
    symbols = stored["symbols"]
    pending = recs

    # Load execution config from strategy.json
    try:
        strat_path = Path(__file__).resolve().parent.parent / "strategy.json"
        with open(strat_path) as sf:
            strategy = json.load(sf)
    except Exception:
        strategy = {}
    exec_cfg = strategy.get("execution", {})
    buy_buffer_pct = exec_cfg.get("buy_limit_buffer_pct", 0.5) / 100  # default 0.5%
    sell_buffer_pct = exec_cfg.get("sell_limit_buffer_pct", 0.5) / 100

    if not pending:
        send_telegram(f"TRADEBOT // {today_str} - Phase 2: no trades to confirm.")
        _cleanup_recs_file(recs_file)
        return

    try:
        # Fetch broker mode for reporting
        try:
            broker_info = http_get("/api/v1/broker/status")
            broker_mode = broker_info.get("broker_mode", "paper")
        except Exception:
            broker_mode = "paper"
            broker_info = {}

        # Flush any stale pending Alpaca fills BEFORE reading broker state.
        # Fire-and-forget orders submitted in prior sessions can sit on
        # `pending_fills.json` until Phase 2.5 reconciles them — if they
        # fill overnight or between sessions, the next Phase 2 sees
        # local_qty > broker_qty and fires a scary "pre-trade drift" warning
        # that is actually just un-recorded fills. Running /trades/reconcile
        # here is idempotent (already-reconciled orders are skipped) and
        # ensures the subsequent /broker/status reflects post-flush truth.
        if broker_mode in ("alpaca_paper", "alpaca_live"):
            try:
                flush_result = http_post("/api/v1/trades/reconcile", {})
                flush_count = flush_result.get("reconciled", 0)
                if flush_count > 0:
                    print(f"Pre-trade flush: reconciled {flush_count} stale pending order(s)")
                    try:
                        broker_info = http_get("/api/v1/broker/status")
                    except Exception as e:
                        print(f"Post-flush broker/status refresh failed: {e}")
            except Exception as e:
                print(f"Pre-trade flush failed (continuing): {e}")

        # Pre-trade reconciliation check (post-flush; any mismatch now is real drift)
        pre_recon_warning = ""
        if broker_mode in ("alpaca_paper", "alpaca_live"):
            try:
                recon = broker_info.get("reconciliation", {})
                if recon.get("has_mismatches"):
                    pre_recon_warning = "--- PRE-TRADE RECONCILIATION WARNING ---\n"
                    pre_recon_warning += "Real position drift detected (not stale pending orders):\n"
                    for m in recon.get("mismatches", []):
                        pre_recon_warning += f"  {m['symbol']}: local={m['local_qty']}, broker={m['broker_qty']}\n"
                    pre_recon_warning += "Proceeding with trades anyway.\n\n"
                    print(f"PRE-TRADE RECONCILIATION WARNING: {recon.get('mismatches')}")
            except Exception as e:
                print(f"Pre-trade reconciliation check failed: {e}")

        # Phase 2 fires 45 min after open, so the 9:30 open is stale. Price
        # every limit off the live snapshot: buy at current * (1 + buffer),
        # sell at current * (1 - buffer). That guarantees buy limits end up
        # above market and sell limits below market — symmetric to today's
        # LRCX/GEV failure where open-based sells sat above market all day.
        # Opening price is still fetched as a fallback if the snapshot fails.
        try:
            qs = urllib.parse.urlencode({"symbols": ",".join(symbols)})
            cur_resp = http_get(f"/api/v1/market/current-prices?{qs}")
            current_prices = cur_resp.get("current_prices", {})
        except Exception as e:
            print(f"Current prices fetch failed: {e}")
            current_prices = {}

        try:
            qs = urllib.parse.urlencode({"symbols": ",".join(symbols), "date": today_str})
            prices_resp = http_get(f"/api/v1/market/opening-prices?{qs}")
            opening_prices = prices_resp.get("opening_prices", {})
        except Exception as e:
            print(f"Opening prices fetch failed: {e}")
            opening_prices = {}

        trades_detail = ""
        for r in pending:
            rec_id = r["id"]
            symbol = r["symbol"]
            action = r["action"].upper()
            qty = float(r["quantity"])
            suggested = float(r["suggested_price"])
            current = current_prices.get(symbol)
            # Base price: prefer live snapshot, then opening auction, then
            # Claude's suggested price. The first two are real market quotes;
            # the last is a stale Claude guess used only when everything else
            # failed.
            base_price = current or opening_prices.get(symbol) or suggested
            if action == "BUY":
                fill_price = round(base_price * (1 + buy_buffer_pct), 2)
            else:
                fill_price = round(base_price * (1 - sell_buffer_pct), 2)

            # Wrong-side-of-market guard. A buy limit below current or sell
            # limit above current almost never fills, so drop the trade loudly
            # instead of silently wasting the session.
            if current is not None:
                if action == "BUY" and fill_price < current:
                    msg = (
                        f"limit ${fill_price:.2f} below current ${current:.2f} — "
                        f"would never fill"
                    )
                    print(f"  skipping {symbol}: {msg}")
                    trades_detail += f"  BUY {symbol} - BLOCKED: {msg}\n"
                    continue
                if action == "SELL" and fill_price > current:
                    msg = (
                        f"limit ${fill_price:.2f} above current ${current:.2f} — "
                        f"would never fill"
                    )
                    print(f"  skipping {symbol}: {msg}")
                    trades_detail += f"  SELL {symbol} - BLOCKED: {msg}\n"
                    continue

            try:
                # Server is now source-of-truth: uses stored rec qty/price + live
                # Alpaca snapshot. Client values are ignored (audit C1 hardening).
                result = http_post("/api/v1/trades/confirm", {
                    "recommendation_id": rec_id,
                })
                print(f"confirm_trade {symbol}: {result}")
                if "error" in result:
                    print(f"  skipping {symbol}: {result['error']}")
                    continue
                trade_id = result.get("trade_id", 0)
                if trade_id == 0:
                    # Alpaca fire-and-forget: order submitted, will reconcile later
                    trades_detail += f"  {action} {symbol} - {qty:.0f}sh SUBMITTED @ limit ${fill_price:.2f} (reconcile in ~15min)\n"
                else:
                    gain = result.get("realized_gain")
                    actual_price = float(result.get("execution_price", fill_price))
                    slip = actual_price - suggested
                    trades_detail += f"  {action} {symbol} - {qty:.0f}sh @ ${actual_price:.2f} (slippage: {'+' if slip>=0 else ''}{slip:.2f})\n"
                    if gain is not None:
                        gain_f = float(gain)
                        trades_detail += f"    Realized P&L: {'+' if gain_f>=0 else ''}${gain_f:,.2f}\n"
            except urllib.error.HTTPError as e:
                body = e.read().decode() if hasattr(e, 'read') else str(e)
                print(f"confirm_trade {symbol} failed ({e.code}): {body}")
                trades_detail += f"  {action} {symbol} - NOT FILLED: {body[:100]}\n"
            except Exception as e:
                print(f"confirm_trade {symbol} failed: {e}")
                trades_detail += f"  {action} {symbol} - ERROR: {e}\n"

        # Fetch updated portfolio
        try:
            portfolio = http_get("/api/v1/portfolio")
            total = float(portfolio.get("total_value", 0))
            ret_pct = portfolio.get("all_time_return_pct", 0)
            cash = float(portfolio.get("cash_balance", 0))
            positions = portfolio.get("positions", [])
        except Exception as e:
            print(f"Portfolio fetch failed: {e}")
            portfolio = {}
            total = cash = 0
            ret_pct = 0
            positions = []

        mode_label = {"paper": "PAPER", "alpaca_paper": "ALPACA-PAPER", "alpaca_live": "LIVE"}.get(broker_mode, broker_mode.upper())
        msg = f"TRADEBOT [{mode_label}] // {today_str} - Executed at open\n"
        if pre_recon_warning:
            msg += "\n" + pre_recon_warning
        msg += f"Portfolio: ${total:,.2f} ({fmt_pct(ret_pct)})\n\n"
        msg += "Trades Executed:\n" + trades_detail

        if positions:
            msg += "\nOpen Positions:\n"
            for p in positions:
                gain = float(p.get("unrealized_gain", 0))
                gain_pct = float(p.get("unrealized_gain_pct", 0))
                tax = "ST" if "short" in p.get("tax_category", "") else "LT"
                sign = "+" if gain >= 0 else ""
                msg += (
                    f"  {p['symbol']}: {float(p['shares']):.0f}sh | "
                    f"avg ${float(p['avg_cost_basis']):.2f} | "
                    f"now ${float(p['current_price']):.2f} | "
                    f"{sign}${gain:,.2f} ({sign}{gain_pct:.1f}%) [{tax}]\n"
                )

        # Reconciliation check — compare local DB vs broker
        if broker_mode in ("alpaca_paper", "alpaca_live"):
            try:
                recon = http_get("/api/v1/broker/status").get("reconciliation", {})
                if recon.get("has_mismatches"):
                    msg += "\n--- RECONCILIATION WARNING ---\n"
                    msg += "Position mismatches detected:\n"
                    for m in recon.get("mismatches", []):
                        msg += f"  {m['symbol']}: local={m['local_qty']}, broker={m['broker_qty']}\n"
                    msg += "Check dashboard for details.\n"
                    print(f"RECONCILIATION WARNING: {recon.get('mismatches')}")
            except Exception as e:
                print(f"Reconciliation check failed: {e}")

        send_telegram(msg)
        print("Phase 2 complete.")
    finally:
        _cleanup_recs_file(recs_file)


if __name__ == "__main__":
    acquire_lock("phase2")
    try:
        main()
    except Exception as e:
        try:
            from common import send_telegram
            send_telegram(f"TRADEBOT // Phase 2 CRASHED\n{type(e).__name__}: {str(e)[:300]}")
        except Exception:
            pass
        raise
    finally:
        release_lock("phase2")
