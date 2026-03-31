"""Shared utilities for cron phase scripts."""
import json
import os
import pathlib
import sys
import urllib.request
import urllib.error
import datetime
import pytz


def acquire_lock(name):
    """Acquire a PID lock file. Exits if another instance is running."""
    lock_path = f"/tmp/tradebot_{name}.lock"
    if os.path.exists(lock_path):
        try:
            with open(lock_path) as f:
                old_pid = int(f.read().strip())
            # Check if process is still running
            os.kill(old_pid, 0)  # signal 0 = check existence
            print(f"Another {name} instance running (PID {old_pid}), exiting")
            sys.exit(0)
        except (ProcessLookupError, ValueError):
            pass  # Stale lock, remove it
    with open(lock_path, "w") as f:
        f.write(str(os.getpid()))


def release_lock(name):
    """Release the PID lock file."""
    lock_path = f"/tmp/tradebot_{name}.lock"
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass


def load_env():
    """Load .env file from project root into os.environ."""
    env_file = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


def get_base_url():
    """Return the tradebot API base URL."""
    return os.environ.get("TRADEBOT_URL", "http://localhost:8000")


def http_get(path, timeout=60):
    """GET from the tradebot API. Returns parsed JSON."""
    req = urllib.request.Request(f"{get_base_url()}{path}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def http_post(path, payload, timeout=60):
    """POST to the tradebot API. Returns parsed JSON. Includes PIN if set."""
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    pin = os.environ.get("SETTINGS_PIN", "")
    if pin:
        headers["X-Owner-Pin"] = pin
    req = urllib.request.Request(f"{get_base_url()}{path}", data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def send_telegram(text):
    """Send a message via Telegram if credentials are configured."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("Telegram env vars not set — skipping notification")
        return
    payload = {"chat_id": chat_id, "text": text}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"Telegram sent: {resp.read().decode()[:120]}")
    except Exception as e:
        print(f"Telegram error: {e}")


def fmt_pct(val):
    """Format a number as a percentage with sign."""
    v = float(val)
    return f"+{v:.2f}%" if v >= 0 else f"{v:.2f}%"


def now_et():
    """Return current ET datetime and today's date string."""
    est_tz = pytz.timezone("America/New_York")
    now = datetime.datetime.now(est_tz)
    return now, now.date().strftime("%Y-%m-%d")


def check_expected_hour(expected_hour, script_name):
    """Warn if the script is running at an unexpected ET hour (DST drift)."""
    now_est_time, _ = now_et()
    actual_hour = now_est_time.hour
    if actual_hour != expected_hour:
        msg = f"TRADEBOT // TIMING WARNING: {script_name} ran at {now_est_time.strftime('%H:%M %Z')} instead of expected {expected_hour}:xx"
        print(msg)
        send_telegram(msg)
