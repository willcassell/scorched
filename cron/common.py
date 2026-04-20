"""Shared utilities for cron phase scripts."""
import datetime
import json
import os
import pathlib
import sys
import time
import urllib.request
import urllib.error

import pytz


MAX_LOCK_AGE_S = 10 * 60  # reclaim a lock older than this (process assumed hung)


def _lock_path_for(name: str) -> str:
    return f"/tmp/tradebot_{name}.lock"


def acquire_lock(name):
    """Acquire a PID lock. Exits 0 if another instance is actively running.

    A lock file older than MAX_LOCK_AGE_S is treated as stale and reclaimed —
    this prevents a hung process from blocking cron runs forever. Emits a
    Telegram alert when a stale lock is evicted so the operator notices.
    """
    lock_path = _lock_path_for(name)
    if os.path.exists(lock_path):
        try:
            age = time.time() - os.path.getmtime(lock_path)
        except FileNotFoundError:
            age = 0
        try:
            with open(lock_path) as f:
                old_pid = int(f.read().strip())
        except (OSError, ValueError):
            old_pid = None

        if old_pid is not None:
            try:
                os.kill(old_pid, 0)  # raises ProcessLookupError if dead
            except ProcessLookupError:
                pass  # dead PID — fall through to reclaim
            else:
                # Process exists. If the lock is old, reclaim it (hung process).
                if age > MAX_LOCK_AGE_S:
                    _reclaim_stale_lock(name, old_pid, age)
                else:
                    print(
                        f"Another {name} instance running (PID {old_pid}, "
                        f"age {age:.0f}s), exiting"
                    )
                    sys.exit(0)

    # Either no lock, a dead-PID lock, or a stale-age lock → take it.
    with open(lock_path, "w") as f:
        f.write(str(os.getpid()))


def _reclaim_stale_lock(name: str, old_pid: int, age_s: float) -> None:
    msg = (
        f"TRADEBOT // Stale lock reclaimed for {name}: "
        f"PID {old_pid} alive but lock age {age_s / 60:.1f} min exceeds "
        f"MAX_LOCK_AGE_S={MAX_LOCK_AGE_S / 60:.0f} min — evicting"
    )
    print(msg)
    try:
        send_telegram(msg)
    except Exception as e:
        print(f"(Telegram notify failed: {e})")


def release_lock(name):
    """Release the PID lock file."""
    try:
        os.remove(_lock_path_for(name))
    except FileNotFoundError:
        pass


def load_env():
    """Load .env from project root into os.environ.

    .env OVERRIDES any pre-existing env values. `.env` is the single source of
    truth for tradebot config — if cron's BASH_ENV file has stale duplicates
    (e.g., an old SETTINGS_PIN from before a rotate), the server and cron would
    silently diverge and every mutation would 403. Overriding prevents that.
    """
    env_file = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ[key.strip()] = value.strip()


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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 403:
            # Make this failure mode self-diagnosing. 403 on a cron POST is
            # almost always a PIN mismatch between cron env and the server.
            raise urllib.error.HTTPError(
                e.url, e.code,
                f"{e.reason} — PIN mismatch? cron PIN len={len(pin)}; verify SETTINGS_PIN in /home/ubuntu/tradebot/.env matches the server's",
                e.headers, e.fp,
            ) from None
        raise


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
    """Warn if the script is running at an unexpected ET hour (DST drift).

    `expected_hour` may be an int or an iterable of ints (for scripts that run
    more than once per session, e.g. the 10:45 + 14:00 reconcile).
    """
    now_est_time, _ = now_et()
    actual_hour = now_est_time.hour
    expected = {expected_hour} if isinstance(expected_hour, int) else set(expected_hour)
    if actual_hour not in expected:
        msg = (
            f"TRADEBOT // TIMING WARNING: {script_name} ran at "
            f"{now_est_time.strftime('%H:%M %Z')} instead of expected "
            f"{sorted(expected)}:xx"
        )
        print(msg)
        send_telegram(msg)
