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
import pathlib
import urllib.request
import urllib.error
import datetime
import pytz

# Load .env from project root so host cron has the same vars as the container.
_env_file = pathlib.Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

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
    with urllib.request.urlopen(req, timeout=300) as resp:
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

    print(f"[{now_est.strftime('%Y-%m-%d %H:%M:%S %Z')}] Phase 1: generating recommendations for {today_str}")

    try:
        session = http_post("/api/v1/recommendations/generate", {"date": today_str})
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
        send_telegram(f"TRADEBOT // {today_str} - No recommendations generated. Manual check required.")
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
