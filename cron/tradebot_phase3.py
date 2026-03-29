#!/usr/bin/env python3
"""
Phase 3 — End-of-day summary (4:02 PM ET, Mon-Fri)

Fetches portfolio state, today's confirmed trades, and market performance
(major indices + S&P sector ETFs), then sends a summary via Telegram.

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


def http_get(path):
    req = urllib.request.Request(f"{BASE_URL}{path}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def http_post(path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=data, headers={"Content-Type": "application/json"}
    )
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

    print(f"[{now_est.strftime('%Y-%m-%d %H:%M:%S %Z')}] Phase 3: end-of-day summary for {today_str}")

    try:
        portfolio = http_get("/api/v1/portfolio")
        history = http_get("/api/v1/portfolio/history?limit=50")
        qs = urllib.parse.urlencode({"date": today_str})
        market = http_get(f"/api/v1/market/eod-summary?{qs}")
    except Exception as e:
        send_telegram(f"TRADEBOT // {today_str} - Phase 3 failed\nError: {e}")
        print(f"Error: {e}")
        return

    total = float(portfolio.get("total_value", 0))
    ret_pct = portfolio.get("all_time_return_pct", 0)
    cash = float(portfolio.get("cash_balance", 0))
    positions_val = total - cash

    msg = f"TRADEBOT // {today_str} - End of Day\n"
    msg += f"Portfolio: ${total:,.2f} ({fmt_pct(ret_pct)})\n"
    msg += f"Cash: ${cash:,.2f} | Positions: ${positions_val:,.2f}\n\n"

    # Today's confirmed trades — query TradeHistory directly (executed_at stored in UTC;
    # market-hours trades never cross midnight UTC so the date prefix always matches ET date).
    position_prices = {p["symbol"]: float(p["current_price"]) for p in portfolio.get("positions", [])}
    today_trades = [t for t in history if t["executed_at"][:10] == today_str]
    if today_trades:
        msg += "Today's Trades:\n"
        for t in today_trades:
            action = t["action"].upper()
            shares = float(t["shares"])
            price = float(t["execution_price"])
            symbol = t["symbol"]
            gain = t.get("realized_gain")
            if gain is not None:
                # SELL — show realized P&L
                gain_f = float(gain)
                cost_basis = price * shares - gain_f
                gain_pct = gain_f / cost_basis * 100 if cost_basis != 0 else 0
                sign = "+" if gain_f >= 0 else ""
                msg += f"  {action} {symbol} - {shares:.0f}sh @ ${price:.2f} | Realized: {sign}${gain_f:,.2f} ({sign}{gain_pct:.1f}%)\n"
            else:
                # BUY — show EOD price vs execution price (day-1 performance)
                eod_price = position_prices.get(symbol)
                if eod_price is not None:
                    day1_gain = (eod_price - price) * shares
                    day1_pct = (eod_price - price) / price * 100 if price != 0 else 0
                    sign = "+" if day1_gain >= 0 else ""
                    msg += f"  {action} {symbol} - {shares:.0f}sh @ ${price:.2f} | EOD: ${eod_price:.2f} ({sign}${day1_gain:,.2f}, {sign}{day1_pct:.1f}%)\n"
                else:
                    msg += f"  {action} {symbol} - {shares:.0f}sh @ ${price:.2f}\n"
    else:
        msg += "No confirmed trades today.\n"
    msg += "\n"

    # Open positions
    positions = portfolio.get("positions", [])
    if positions:
        msg += "Open Positions:\n"
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
        msg += "\n"

    # Market summary
    indices = market.get("indices", {})
    sectors = market.get("sectors", {})

    if indices:
        msg += "Market:\n"
        for label, data in indices.items():
            pct = data["change_pct"]
            sign = "+" if pct >= 0 else ""
            msg += f"  {label}: {data['price']:,.0f} ({sign}{pct:.1f}%)\n"

    if sectors:
        sorted_sectors = sorted(sectors.items(), key=lambda x: x[1]["change_pct"], reverse=True)
        msg += "Sectors (top 4 / bottom 2):\n"
        for sym, data in sorted_sectors[:4] + sorted_sectors[-2:]:
            pct = data["change_pct"]
            sign = "+" if pct >= 0 else ""
            msg += f"  {sym} ({data['label']}): {sign}{pct:.1f}%\n"

    send_telegram(msg)

    # Trigger EOD review: Claude compares morning thesis against intraday outcomes
    # and updates the playbook so tomorrow's picks benefit from today's learnings.
    try:
        review = http_post(f"/api/v1/market/eod-review?date={today_str}", {})
        status = review.get("status", "unknown")
        version = review.get("playbook_version")
        if status == "completed":
            print(f"EOD review complete — playbook updated to v{version}.")
        else:
            print(f"EOD review: {status} ({review.get('reason', '')})")
    except Exception as e:
        print(f"EOD review error (non-fatal): {e}")

    print("Phase 3 complete.")


if __name__ == "__main__":
    main()
