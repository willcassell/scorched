#!/usr/bin/env python3
"""Pre-flight validation for Scorched trading bot setup.

Usage:
    python3 scripts/validate_setup.py
    python3 scripts/validate_setup.py --json    # Machine-readable output
"""

import json
import os
import shutil
import subprocess
import sys
import urllib.request
import urllib.error

# ── ANSI colors ──────────────────────────────────────────────────────────

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

CHECK = f"{GREEN}✓{RESET}"
CROSS_RED = f"{RED}✗{RESET}"
CROSS_YELLOW = f"{YELLOW}✗{RESET}"

# ── Helpers ──────────────────────────────────────────────────────────────

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    """Run a command and return (returncode, stdout)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except FileNotFoundError:
        return 127, "command not found"
    except subprocess.TimeoutExpired:
        return 1, "timed out"


def parse_env_file(path: str) -> dict[str, str]:
    """Parse a .env file into a dict. Handles quotes and comments."""
    env = {}
    if not os.path.isfile(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            env[key] = value
    return env


def http_get_json(url: str, timeout: int = 5) -> tuple[bool, dict | str]:
    """GET a URL and parse JSON. Returns (ok, data_or_error)."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return True, json.loads(body)
    except urllib.error.URLError as e:
        return False, str(e.reason)
    except Exception as e:
        return False, str(e)


# ── Check definitions ────────────────────────────────────────────────────

class CheckResult:
    def __init__(self, name: str, passed: bool, detail: str,
                 required: bool = True, category: str = ""):
        self.name = name
        self.passed = passed
        self.detail = detail
        self.required = required
        self.category = category

    def line(self) -> str:
        if self.passed:
            return f"  {CHECK} {self.name}: {self.detail}"
        elif not self.required:
            return f"  {CROSS_YELLOW} {self.name}: {self.detail}"
        else:
            return f"  {CROSS_RED} {self.name}: {self.detail}"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "required": self.required,
            "category": self.category,
        }


def check_docker() -> list[CheckResult]:
    results = []

    rc, out = run(["docker", "--version"])
    if rc == 0:
        ver = out.split("version")[-1].strip().rstrip(",").strip() if "version" in out.lower() else out
        results.append(CheckResult("Docker", True, ver, category="Docker"))
    else:
        results.append(CheckResult("Docker", False, "not installed", category="Docker"))

    rc, out = run(["docker", "compose", "version"])
    if rc == 0:
        ver = out.split("version")[-1].strip() if "version" in out.lower() else out
        ver = ver.lstrip("v")
        results.append(CheckResult("Docker Compose", True, ver, category="Docker"))
    else:
        results.append(CheckResult("Docker Compose", False, "not installed", category="Docker"))

    return results


def check_env_file() -> list[CheckResult]:
    results = []
    env_path = os.path.join(PROJECT_ROOT, ".env")

    if not os.path.isfile(env_path):
        results.append(CheckResult(".env file", False, "missing — copy .env.example to .env", category="Configuration"))
        return results

    results.append(CheckResult(".env file", True, "present", category="Configuration"))
    env = parse_env_file(env_path)

    # Required: ANTHROPIC_API_KEY
    key = env.get("ANTHROPIC_API_KEY", "")
    if key and not key.startswith("sk-ant-...") and key != "sk-ant-":
        results.append(CheckResult("ANTHROPIC_API_KEY", True, "configured", category="Configuration"))
    else:
        results.append(CheckResult("ANTHROPIC_API_KEY", False, "not set or placeholder", category="Configuration"))

    # Optional keys with descriptions
    optional_keys = [
        ("FRED_API_KEY", "macro economic data from FRED"),
        ("POLYGON_API_KEY", "news headlines from Polygon.io"),
        ("ALPHA_VANTAGE_API_KEY", "RSI data (25 calls/day free tier)"),
        ("TWELVEDATA_API_KEY", "RSI for full watchlist (800 calls/day)"),
        ("FINNHUB_API_KEY", "analyst consensus ratings"),
        ("ALPACA_API_KEY", "broker integration"),
        ("TELEGRAM_BOT_TOKEN", "trade notifications via Telegram"),
    ]

    for key_name, desc in optional_keys:
        val = env.get(key_name, "")
        if val and val not in ("", "sk-ant-...", "PK..."):
            results.append(CheckResult(key_name, True, "configured", required=False, category="Configuration"))
        else:
            results.append(CheckResult(key_name, False, f"not configured (optional — {desc})", required=False, category="Configuration"))

    return results


def check_containers() -> list[CheckResult]:
    results = []
    rc, out = run(["docker", "compose", "ps", "--format", "json"], timeout=15)
    if rc != 0:
        results.append(CheckResult("PostgreSQL", False, "docker compose ps failed", category="Services"))
        results.append(CheckResult("Tradebot", False, "docker compose ps failed", category="Services"))
        return results

    # Parse container info — docker compose ps --format json outputs one JSON object per line
    containers = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            c = json.loads(line)
            name = c.get("Service", c.get("Name", ""))
            state = c.get("State", "unknown")
            health = c.get("Health", "")
            containers[name] = (state, health)
        except json.JSONDecodeError:
            continue

    # Check postgres
    for key in ("postgres", "db", "postgresql"):
        if key in containers:
            state, health = containers[key]
            if state == "running":
                detail = "healthy" if health == "healthy" else "running"
                results.append(CheckResult("PostgreSQL", True, detail, category="Services"))
            else:
                results.append(CheckResult("PostgreSQL", False, state, category="Services"))
            break
    else:
        results.append(CheckResult("PostgreSQL", False, "container not found", category="Services"))

    # Check tradebot
    if "tradebot" in containers:
        state, health = containers["tradebot"]
        if state == "running":
            results.append(CheckResult("Tradebot", True, "running", category="Services"))
        else:
            results.append(CheckResult("Tradebot", False, state, category="Services"))
    else:
        results.append(CheckResult("Tradebot", False, "container not found", category="Services"))

    return results


def check_api_health() -> list[CheckResult]:
    results = []

    ok, data = http_get_json("http://localhost:8000/health")
    if ok and isinstance(data, dict) and data.get("status") == "ok":
        results.append(CheckResult("API health", True, "responding at http://localhost:8000", category="Services"))
    elif ok:
        results.append(CheckResult("API health", False, f"unexpected response: {data}", category="Services"))
    else:
        results.append(CheckResult("API health", False, f"not reachable ({data})", category="Services"))

    return results


def check_system_health() -> list[CheckResult]:
    results = []

    ok, data = http_get_json("http://localhost:8000/api/v1/system/health")
    if ok and isinstance(data, dict):
        services = data.get("services", data)
        for svc_name, svc_info in services.items():
            if isinstance(svc_info, dict):
                status = svc_info.get("status", "unknown")
                passed = status in ("healthy", "ok", "green")
                results.append(CheckResult(
                    f"Service: {svc_name}", passed, status,
                    required=False, category="Services"
                ))
    elif not ok:
        results.append(CheckResult("System health", False, f"endpoint not reachable ({data})",
                                   required=False, category="Services"))

    return results


def check_cron() -> list[CheckResult]:
    rc, out = run(["crontab", "-l"])
    if rc != 0:
        return [CheckResult("Cron jobs", False, "no crontab installed", category="Automation")]

    # Count tradebot-related cron jobs
    job_count = 0
    has_marker = False
    for line in out.splitlines():
        line = line.strip()
        if "SCORCHED-TRADEBOT" in line or "SCORCHED" in line.upper():
            has_marker = True
        if line and not line.startswith("#") and "tradebot" in line.lower():
            job_count += 1

    if job_count > 0 or has_marker:
        return [CheckResult("Cron jobs", True, f"installed ({job_count} jobs)", category="Automation")]
    else:
        return [CheckResult("Cron jobs", False, "no tradebot cron jobs found", category="Automation")]


def check_telegram() -> list[CheckResult]:
    env = parse_env_file(os.path.join(PROJECT_ROOT, ".env"))
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    if token and chat_id:
        return [CheckResult("Telegram", True, "configured (bot token + chat ID)", required=False, category="Automation")]
    elif token:
        return [CheckResult("Telegram", False, "bot token set but TELEGRAM_CHAT_ID missing (optional — trade notifications)",
                            required=False, category="Automation")]
    else:
        return [CheckResult("Telegram", False, "not configured (optional — trade notifications)",
                            required=False, category="Automation")]


def check_disk() -> list[CheckResult]:
    try:
        usage = shutil.disk_usage(PROJECT_ROOT)
        free_gb = usage.free / (1024 ** 3)
        if free_gb >= 2.0:
            return [CheckResult("Disk space", True, f"{free_gb:.0f} GB free", category="System")]
        else:
            return [CheckResult("Disk space", False, f"{free_gb:.1f} GB free (need >= 2 GB)", category="System")]
    except Exception as e:
        return [CheckResult("Disk space", False, str(e), category="System")]


def check_strategy() -> list[CheckResult]:
    path = os.path.join(PROJECT_ROOT, "strategy.json")
    if os.path.isfile(path):
        return [CheckResult("Strategy file", True, "present", category="System")]
    else:
        return [CheckResult("Strategy file", False, "strategy.json not found", category="System")]


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> int:
    json_mode = "--json" in sys.argv

    all_results: list[CheckResult] = []

    # Run all checks
    all_results.extend(check_docker())
    all_results.extend(check_env_file())
    all_results.extend(check_containers())
    all_results.extend(check_api_health())
    all_results.extend(check_system_health())
    all_results.extend(check_cron())
    all_results.extend(check_telegram())
    all_results.extend(check_disk())
    all_results.extend(check_strategy())

    # JSON output
    if json_mode:
        output = {
            "results": [r.to_dict() for r in all_results],
            "total": len(all_results),
            "passed": sum(1 for r in all_results if r.passed),
            "required_failed": sum(1 for r in all_results if not r.passed and r.required),
            "optional_missing": sum(1 for r in all_results if not r.passed and not r.required),
        }
        print(json.dumps(output, indent=2))
        return 1 if output["required_failed"] > 0 else 0

    # Pretty output
    print()
    print(f"{BOLD}Scorched Tradebot — Setup Validation{RESET}")
    print("=" * 39)

    current_category = ""
    for r in all_results:
        if r.category != current_category:
            current_category = r.category
            print(f"\n{BOLD}{current_category}{RESET}")
        print(r.line())

    # Summary
    total = len(all_results)
    passed = sum(1 for r in all_results if r.passed)
    required_failed = sum(1 for r in all_results if not r.passed and r.required)
    optional_missing = sum(1 for r in all_results if not r.passed and not r.required)

    print()
    if required_failed == 0:
        detail = f"{passed}/{total} checks passed"
        if optional_missing > 0:
            detail += f" ({optional_missing} optional item{'s' if optional_missing != 1 else ''} not configured)"
        print(f"{BOLD}{GREEN}Result: {detail}{RESET}")
    else:
        print(f"{BOLD}{RED}Result: {passed}/{total} checks passed — "
              f"{required_failed} required check{'s' if required_failed != 1 else ''} failed{RESET}")

    print()
    return 1 if required_failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
