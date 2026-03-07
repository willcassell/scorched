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
import urllib.request
import urllib.parse
import datetime
import pytz

BASE_URL = os.environ.get("TRADEBOT_URL", "http://localhost:8000")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
RECS_FILE = "/tmp/tradebot_recommendations.json"


def http_get(path):
    req = urllib.request.Request(f"{BASE_URL}{path}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def http_post(path, payload):
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    pin = os.environ.get("SETTINGS_PIN", "")
    if pin:
        headers["X-Owner-Pin"] = pin
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram env vars not set — skipping notification")
        return
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"Telegram sent: {resp.read().decode()[:120]}")
    except Exception as e:
        print(f"Telegram error: {e}")


def fmt_pct(val):
    v = float(val)
    return f"+{v:.2f}%" if v >= 0 else f"{v:.2f}%"


def main():
    est_tz = pytz.timezone("America/New_York")
    now_est = datetime.datetime.now(est_tz)
    today_str = now_est.date().strftime("%Y-%m-%d")

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
    # All recs in the file are candidates — the confirm endpoint returns an error
    # for any that are already confirmed, which we handle gracefully below.
    pending = recs

    if not pending:
        send_telegram(f"TRADEBOT // {today_str} - Phase 2: no trades to confirm.")
        os.remove(RECS_FILE)
        return

    # Fetch opening prices
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
            # Check if the API returned an error (already confirmed, etc.)
            if "error" in result:
                print(f"  skipping {symbol}: {result['error']}")
                continue
            gain = result.get("realized_gain")
            if open_price is not None:
                slip = open_price - suggested
                trades_detail += f"  {action} {symbol} - {qty:.0f}sh @ ${fill_price:.2f} (slippage: {'+' if slip>=0 else ''}{slip:.2f})\n"
            else:
                trades_detail += f"  {action} {symbol} - {qty:.0f}sh @ ${fill_price:.2f} (fallback: no open price)\n"
            if gain is not None:
                gain_f = float(gain)
                trades_detail += f"    Realized P&L: {'+' if gain_f>=0 else ''}${gain_f:,.2f}\n"
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

    msg = f"TRADEBOT // {today_str} - Executed at open\n"
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
