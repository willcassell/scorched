#!/usr/bin/env python3
"""Automated cron setup for Scorched trading bot.

Usage:
    python3 scripts/setup_cron.py              # Auto-detect timezone, install cron jobs
    python3 scripts/setup_cron.py --check      # Check current cron status
    python3 scripts/setup_cron.py --remove     # Remove Scorched cron jobs
    python3 scripts/setup_cron.py --dry-run    # Show what would be installed
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Project root (parent of scripts/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The trading schedule in ET (Eastern Time)
# Each entry: (ET hour, ET minute, cron day-of-week, script, description)
SCHEDULE = [
    (7,  30, "1-5", "cron/tradebot_phase0.py",         "Phase 0: Data prefetch"),
    (8,  30, "1-5", "cron/tradebot_phase1.py",          "Phase 1: Claude analysis"),
    (9,  30, "1-5", "cron/tradebot_phase1_5.py",        "Phase 1.5: Circuit breaker"),
    (9,  35, "1-5", "cron/tradebot_phase2.py",          "Phase 2: Execute trades"),
    (16,  1, "1-5", "cron/tradebot_phase3.py",          "Phase 3: EOD review"),
    (18,  0, "0",   "cron/tradebot_weekly_reflection.py","Weekly reflection (Sun)"),
]

# Intraday monitor runs every 5 min during market hours
INTRADAY_ET_START = 9   # 9:35 AM ET (cron can't do :35, so starts at :00 of hour 9, script self-gates)
INTRADAY_ET_END = 15    # through 3:55 PM ET (hour 15)

MARKER = "# SCORCHED-TRADEBOT"


def get_utc_offset_hours():
    """Get the current UTC offset for US Eastern time.

    Returns 4 during EDT (Mar-Nov) and 5 during EST (Nov-Mar).
    """
    try:
        import pytz
        from datetime import datetime
        et = pytz.timezone("America/New_York")
        now = datetime.now(et)
        offset = now.utcoffset()
        return int(-offset.total_seconds() / 3600)
    except ImportError:
        # Fallback: check if we're in DST by looking at current month
        month = datetime.now().month
        if 3 <= month <= 10:
            return 4  # EDT
        return 5  # EST


def et_to_utc(et_hour, et_minute, utc_offset):
    """Convert ET hour:minute to UTC hour:minute."""
    utc_hour = (et_hour + utc_offset) % 24
    return utc_hour, et_minute


def build_cron_lines(project_dir, utc_offset):
    """Build the crontab lines for all phases."""
    lines = []
    lines.append(f"{MARKER} — AUTO-GENERATED (UTC offset: -{utc_offset}h from ET)")
    lines.append(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} | DST active: {utc_offset == 4}")
    lines.append("")

    for et_h, et_m, dow, script, desc in SCHEDULE:
        utc_h, utc_m = et_to_utc(et_h, et_m, utc_offset)
        line = f"{utc_m} {utc_h} * * {dow} cd {project_dir} && python3 {script} >> {project_dir}/cron.log 2>&1"
        lines.append(f"# {desc} ({et_h}:{et_m:02d} ET = {utc_h}:{utc_m:02d} UTC)")
        lines.append(line)

    # Intraday monitor
    intraday_start_utc = (INTRADAY_ET_START + utc_offset) % 24
    intraday_end_utc = (INTRADAY_ET_END + utc_offset) % 24
    lines.append(f"# Intraday monitor (every 5 min, {INTRADAY_ET_START} AM–{INTRADAY_ET_END}:55 PM ET, self-gates)")
    lines.append(f"*/5 {intraday_start_utc}-{intraday_end_utc} * * 1-5 cd {project_dir} && python3 cron/intraday_monitor.py >> {project_dir}/cron.log 2>&1")

    lines.append(f"{MARKER}-END")
    lines.append("")

    return "\n".join(lines)


def get_current_crontab():
    """Get current crontab contents."""
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout
        return ""
    except FileNotFoundError:
        return ""


def remove_scorched_lines(crontab_text):
    """Remove existing Scorched cron lines from crontab text."""
    lines = crontab_text.split("\n")
    result = []
    inside_block = False
    for line in lines:
        if MARKER in line and "END" not in line:
            inside_block = True
            continue
        if f"{MARKER}-END" in line:
            inside_block = False
            continue
        if not inside_block:
            result.append(line)
    # Remove trailing blank lines
    while result and result[-1].strip() == "":
        result.pop()
    return "\n".join(result)


def install_crontab(new_content):
    """Install new crontab content."""
    proc = subprocess.run(
        ["crontab", "-"],
        input=new_content,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(f"ERROR: Failed to install crontab: {proc.stderr}")
        sys.exit(1)


def check_prerequisites():
    """Check that required tools and files exist."""
    issues = []

    # Check pytz
    try:
        import pytz
    except ImportError:
        issues.append("pytz not installed. Run: pip3 install pytz")

    # Check cron scripts exist
    for _, _, _, script, desc in SCHEDULE:
        path = PROJECT_ROOT / script
        if not path.exists():
            issues.append(f"Missing script: {script}")

    intraday = PROJECT_ROOT / "cron" / "intraday_monitor.py"
    if not intraday.exists():
        issues.append("Missing script: cron/intraday_monitor.py")

    # Check .env exists
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        issues.append(".env file not found — run the onboarding wizard first")

    return issues


def main():
    parser = argparse.ArgumentParser(description="Set up cron jobs for Scorched trading bot")
    parser.add_argument("--check", action="store_true", help="Show current cron status")
    parser.add_argument("--remove", action="store_true", help="Remove Scorched cron jobs")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be installed")
    parser.add_argument("--project-dir", type=str, default=None,
                        help="Project directory (default: auto-detect)")
    args = parser.parse_args()

    project_dir = args.project_dir or str(PROJECT_ROOT)

    if args.check:
        current = get_current_crontab()
        if MARKER in current:
            print("Scorched cron jobs are INSTALLED:")
            print()
            in_block = False
            for line in current.split("\n"):
                if MARKER in line:
                    in_block = "END" not in line
                    print(line)
                    continue
                if in_block:
                    print(line)
                if f"{MARKER}-END" in line:
                    in_block = False
        else:
            print("Scorched cron jobs are NOT installed.")
            print("Run: python3 scripts/setup_cron.py")
        return

    if args.remove:
        current = get_current_crontab()
        if MARKER not in current:
            print("No Scorched cron jobs found — nothing to remove.")
            return
        cleaned = remove_scorched_lines(current)
        install_crontab(cleaned + "\n")
        print("Scorched cron jobs removed.")
        return

    # Prerequisites check
    issues = check_prerequisites()
    if issues:
        print("Prerequisites check failed:")
        for issue in issues:
            print(f"  - {issue}")
        if not args.dry_run:
            sys.exit(1)

    utc_offset = get_utc_offset_hours()
    dst_active = utc_offset == 4

    print(f"Timezone: US Eastern ({'EDT (UTC-4)' if dst_active else 'EST (UTC-5)'})")
    print(f"Project directory: {project_dir}")
    print()

    cron_block = build_cron_lines(project_dir, utc_offset)

    if args.dry_run:
        print("Would install the following cron jobs:\n")
        print(cron_block)
        return

    # Install
    current = get_current_crontab()
    cleaned = remove_scorched_lines(current)

    if cleaned and not cleaned.endswith("\n"):
        cleaned += "\n"

    new_content = cleaned + "\n" + cron_block + "\n"
    install_crontab(new_content)

    print("Cron jobs installed successfully!")
    print()
    print("Schedule (all times ET):")
    for et_h, et_m, dow, script, desc in SCHEDULE:
        day_label = "Mon-Fri" if dow == "1-5" else "Sunday"
        print(f"  {et_h}:{et_m:02d} {day_label} — {desc}")
    print(f"  Every 5 min during market hours — Intraday monitor")
    print()
    print(f"DST is {'ACTIVE' if dst_active else 'INACTIVE'}.")
    print("When US clocks change, re-run this script to auto-update UTC times.")
    print()
    print("Verify with: python3 scripts/setup_cron.py --check")


if __name__ == "__main__":
    main()
