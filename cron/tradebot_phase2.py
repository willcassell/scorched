#!/usr/bin/env python3
"""
Phase 2 — Market open (9:45 AM ET, Mon-Fri)

Reads Phase 1's recommendations JSON, fetches actual opening prices,
confirms (or rejects) each trade, then sends a fill report via Telegram.
Deletes the recommendations JSON when done.

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
from common import load_env, http_get, http_post, send_telegram, fmt_pct, now_et

load_env()

RECS_FILE = "/tmp/tradebot_recommendations.json"


def main():
    now_est, today_str = now_et()

    print(f"[{now_est.strftime('%Y-%m-%d %H:%M:%S %Z')}] Phase 2: confirming trades for {today_str}")

    if not os.path.exists(RECS_FILE):
        send_telegram(f"TRADEBOT // {today_str} - Phase 2 skipped: no Phase 1 data found.")
        print("No recommendations file found.")
        return

    with open(RECS_FILE) as f:
        stored = json.load(f)

    if stored["date"] != today_str:
        send_telegram(
            f"TRADEBOT // {today_str} - Phase 2 skipped: "
            f"recommendations are for {stored['date']}, not today."
        )
        os.remove(RECS_FILE)
        print(f"Date mismatch: {stored['date']} != {today_str}")
        return

    recs = stored["recommendations"]
    symbols = stored["symbols"]
    pending = recs

    if not pending:
        send_telegram(f"TRADEBOT // {today_str} - Phase 2: no trades to confirm.")
        os.remove(RECS_FILE)
        return

    # Fetch broker mode for reporting
    try:
        broker_info = http_get("/api/v1/broker/status")
        broker_mode = broker_info.get("broker_mode", "paper")
    except Exception:
        broker_mode = "paper"

    # Fetch opening prices (used as limit price for broker orders)
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
        open_price = opening_prices.get(symbol)
        fill_price = open_price if open_price is not None else suggested

        try:
            result = http_post("/api/v1/trades/confirm", {
                "recommendation_id": rec_id,
                "execution_price": fill_price,
                "shares": qty,
            })
            print(f"confirm_trade {symbol}: {result}")
            if "error" in result:
                print(f"  skipping {symbol}: {result['error']}")
                continue
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

    send_telegram(msg)
    os.remove(RECS_FILE)
    print("Phase 2 complete.")


if __name__ == "__main__":
    main()
