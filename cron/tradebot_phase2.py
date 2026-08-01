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


def _cleanup_recs_file(path=None):
    """Remove the recommendations file, ignoring if already gone.

    Also clears the *other* file (gated vs original) — Phase 2 only reads
    the preferred one, so the unused sibling can sit around as a stale
    leftover that confuses the next session's date-mismatch check. `path`
    is optional: the DB-only rescue path has no file at all, so callers may
    pass None and just get the fixed GATED_FILE/ORIGINAL_FILE cleanup.
    """
    paths = {GATED_FILE, ORIGINAL_FILE}
    if path:
        paths.add(path)
    for p in paths:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def _load_valid_candidate(path: str, today_str: str) -> tuple[dict | None, str]:
    """Parse a recommendations file and validate it is usable for today.

    Returns (stored, "") when the file exists, parses, is dated today, and
    has status == "complete"; otherwise (None, reason). Selection must check
    BOTH candidate files this way — previously a stale gated file from
    yesterday masked a valid same-day original, forcing the DB-only rescue
    path and deleting the good file with it.
    """
    if not os.path.exists(path):
        return None, "missing"
    try:
        with open(path) as f:
            stored = json.load(f)
    except Exception as e:
        return None, f"unreadable ({e})"
    if stored.get("date") != today_str:
        return None, f"stale (dated {stored.get('date')})"
    if stored.get("status") != "complete":
        return None, f"incomplete (status={stored.get('status')})"
    return stored, ""


def _apply_circuit_breaker(recs: list[dict]) -> tuple[list[dict], str]:
    """Run the circuit breaker inline over DB-rescued recs.

    The rescue path executes recs that never went through Phase 1.5, so the
    gate must run here or gap-down/SPY/VIX protection is silently skipped.
    Mirrors cron/tradebot_phase1_5.py: sells always pass through
    run_circuit_breaker; buys are dropped when a gate fails. Fails CLOSED
    for buys — if the circuit breaker itself errors we cannot verify market
    conditions, so buys are dropped and sells proceed.

    Returns (surviving_recs, note) where note is a Telegram-ready line ("" if
    nothing noteworthy).
    """
    if not any(r.get("action") == "buy" for r in recs):
        return recs, ""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        import asyncio

        from scorched.circuit_breaker import run_circuit_breaker
        from scorched.services.strategy import load_strategy_json

        cb_config = load_strategy_json().get("circuit_breaker", {"enabled": False})
        if not cb_config.get("enabled", False):
            return recs, "circuit breaker disabled in strategy.json — rescue recs pass through\n"

        results = asyncio.run(run_circuit_breaker(list(recs), cb_config))
        passed, blocked = [], []
        for rec in results:
            gate = rec.pop("gate_result")
            if gate.passed:
                passed.append(rec)
            else:
                blocked.append((rec, getattr(gate, "reason", "") or "gate failed"))
        note = ""
        if blocked:
            note = (
                "CIRCUIT BREAKER blocked during rescue: "
                + ", ".join(f"{r['action']} {r['symbol']} ({reason})" for r, reason in blocked)
                + "\n"
            )
        return passed, note
    except Exception as e:
        sells = [r for r in recs if r.get("action") != "buy"]
        return sells, (
            f"⚠️ circuit breaker errored during rescue ({e}) — "
            f"buys dropped fail-closed, {len(sells)} sell(s) kept\n"
        )


def fetch_db_pending_recs(today_str: str, http_get_fn) -> list[dict]:
    """Fetch today's session recs from the DB and filter to status=='pending'.

    Pure-ish helper (only side effect is the injected http_get call) so it's
    reusable by both the DB-authority merge and the DB-only rescue path
    below. Returns [] on any fetch failure — callers treat that as "nothing
    to rescue", never as a reason to block.
    """
    try:
        db_sessions = http_get_fn(
            f"/api/v1/recommendations?session_date={today_str}&limit=1&include_recs=true"
        )
        db_recs = db_sessions[0]["recommendations"] if db_sessions else []
    except Exception as e:
        print(f"DB-pending fetch failed: {e}")
        return []
    return [r for r in db_recs if r.get("status") == "pending"]


def merge_pending(file_recs: list[dict], db_recs: list[dict]) -> tuple[list[dict], list[str]]:
    """Union file recs with today's DB-pending recs. Returns (merged, missing_from_file_symbols).
    DB rec shape comes from GET /api/v1/recommendations (id, symbol, action, status, suggested_price...).
    Only status == 'pending' DB recs are added; file entries win on duplicates (they carry gate results)."""
    by_symbol = {(r["symbol"], r["action"]): r for r in file_recs}
    missing = []
    merged = list(file_recs)
    for r in db_recs:
        if r.get("status") != "pending":
            continue
        key = (r["symbol"], r["action"])
        if key not in by_symbol:
            merged.append(r)
            missing.append(f"{r['action']} {r['symbol']}")
    return merged, missing


def gate_blocked_keys(original_recs: list[dict], gated_recs: list[dict]) -> set[tuple[str, str]]:
    """Return (symbol, action) keys present in the pre-circuit-breaker file
    but absent from the post-circuit-breaker (gated) file — i.e. deliberately
    blocked at 9:55, not lost by the pipeline.

    Phase 1.5 never updates DB status for blocked recs (they stay
    status='pending' in trade_recommendations), so without this exclusion
    `merge_pending` would treat a circuit-breaker rejection identically to a
    rec the file pipeline silently dropped, and Phase 2 would execute it
    ungated — exactly what the circuit breaker exists to prevent.
    """
    gated_keys = {(r["symbol"], r["action"]) for r in gated_recs}
    return {
        (r["symbol"], r["action"])
        for r in original_recs
        if (r["symbol"], r["action"]) not in gated_keys
    }


def main():
    now_est, today_str = now_et()
    check_expected_hour(10, "Phase 2")

    print(f"[{now_est.strftime('%Y-%m-%d %H:%M:%S %Z')}] Phase 2: confirming trades for {today_str}")

    # Prefer gated file (Phase 1.5 output); fall back to original (circuit
    # breaker disabled/not run). Each candidate is validated for TODAY before
    # selection — a stale gated file must never mask a valid same-day
    # original (previously that forced the DB rescue and deleted the good
    # file).
    recs_file = None
    stored = None
    no_file_reason = None
    gated_stored, gated_reason = _load_valid_candidate(GATED_FILE, today_str)
    orig_stored, orig_reason = _load_valid_candidate(ORIGINAL_FILE, today_str)
    if gated_stored is not None:
        recs_file, stored = GATED_FILE, gated_stored
    elif orig_stored is not None:
        recs_file, stored = ORIGINAL_FILE, orig_stored
        if gated_reason != "missing":
            print(f"Gated file unusable ({gated_reason}) — using valid original file")
    else:
        if gated_reason == "missing" and orig_reason == "missing":
            no_file_reason = "no Phase 1 data found"
        else:
            no_file_reason = f"gated: {gated_reason}; original: {orig_reason}"

    mismatch_warning = ""

    if no_file_reason is not None:
        # The Phase 1/1.5 file is missing, stale, or incomplete. Phase 1
        # persists recs to the DB via the API BEFORE the cron writes the
        # file, so a crash between those two steps (or a corrupt/stale
        # file) is exactly the dropped-trade class this task exists to
        # close. Check today's DB session for pending recs before giving up
        # — there is no gated file to diff here, so nothing to exclude via
        # gate_blocked_keys (blocked_keys is correctly empty: no circuit
        # breaker ran against a file that was never usable).
        rescue_recs = fetch_db_pending_recs(today_str, http_get)
        if not rescue_recs:
            send_telegram(f"TRADEBOT // {today_str} - Phase 2 skipped: {no_file_reason}.")
            print(f"Phase 2 skipped, no DB rescue available: {no_file_reason}")
            _cleanup_recs_file(recs_file)
            return

        # Phase 1.5 never saw these recs, so the circuit breaker must run
        # inline here — otherwise a Phase 1 file failure would submit buys
        # with gap-down/SPY/VIX protection silently skipped.
        rescue_recs, cb_note = _apply_circuit_breaker(rescue_recs)
        if not rescue_recs:
            send_telegram(
                f"TRADEBOT // {today_str} - Phase 2: DB rescue found pending recs "
                f"but none survived the circuit breaker.\n{cb_note}"
            )
            print(f"DB rescue: nothing survived circuit breaker ({no_file_reason})")
            _cleanup_recs_file(recs_file)
            return

        send_telegram(
            f"⚠️ PHASE 2: no usable Phase 1 file ({no_file_reason}) — "
            f"executing {len(rescue_recs)} DB-pending rec(s) (circuit breaker applied inline)\n{cb_note}"
        )
        print(f"DB-only rescue ({no_file_reason}): executing {len(rescue_recs)} DB-pending rec(s)")
        recs = rescue_recs
        mismatch_warning = (
            "⚠️ PHASE 2 FILE/DB MISMATCH — executing from DB: "
            + ", ".join(f"{r['action']} {r['symbol']}" for r in rescue_recs)
            + f" ({no_file_reason}; circuit breaker applied inline)\n"
            + cb_note + "\n"
        )
        _cleanup_recs_file(recs_file)
        recs_file = None  # nothing left on disk to clean up again later
    else:
        recs = stored["recommendations"]

        # DB is the source of truth: union file recs with today's DB-pending recs
        # so a rec that survived risk review but never made it into the Phase
        # 1/1.5 file (pipeline bug, crash, etc.) still gets a confirm attempt
        # instead of silently expiring (see ABBV 6/23, GS 7/14, AMZN 7/31).
        # Circuit-breaker-blocked buys are excluded via gate_blocked_keys — they
        # were deliberately rejected at 9:55, not lost by the plumbing, and
        # Phase 1.5 never updates their DB status so they'd otherwise look
        # identical to a dropped rec.
        blocked_keys: set[tuple[str, str]] = set()
        db_merge_skip_reason = ""
        if recs_file == GATED_FILE:
            try:
                with open(ORIGINAL_FILE) as f:
                    original_stored = json.load(f)
                if original_stored.get("date") == today_str:
                    blocked_keys = gate_blocked_keys(original_stored.get("recommendations", []), recs)
                else:
                    db_merge_skip_reason = "original (pre-gate) file date mismatch"
            except FileNotFoundError:
                db_merge_skip_reason = "original (pre-gate) file missing"
            except Exception as e:
                db_merge_skip_reason = f"original (pre-gate) file unreadable ({e})"

        if db_merge_skip_reason:
            # Fail closed: without the original file we can't tell a
            # circuit-breaker rejection from a plumbing drop, so skip the DB
            # merge entirely rather than risk resurrecting a blocked buy ungated.
            print(f"DB-pending merge skipped (fail-closed): {db_merge_skip_reason}")
            send_telegram(
                f"⚠️ PHASE 2: DB-authority merge SKIPPED ({db_merge_skip_reason}) — "
                f"DB-pending recs will not be rescued today."
            )
        else:
            db_recs = fetch_db_pending_recs(today_str, http_get)
            db_recs = [r for r in db_recs if (r["symbol"], r["action"]) not in blocked_keys]
            recs, missing = merge_pending(recs, db_recs)
            if missing:
                mismatch_warning = (
                    "⚠️ PHASE 2 FILE/DB MISMATCH — executing from DB: " + ", ".join(missing) +
                    " (DB-pending, missing from Phase 1/1.5 file; ungated — no circuit-breaker check)\n\n"
                )
                print(f"DB/file mismatch: {missing}")

    symbols = sorted({r["symbol"] for r in recs})
    pending = recs

    # Load execution config from strategy.json
    try:
        strat_path = Path(__file__).resolve().parent.parent / "strategy.json"
        with open(strat_path) as sf:
            strategy = json.load(sf)
    except Exception:
        strategy = {}
    exec_cfg = strategy.get("execution", {})
    buy_buffer_pct = exec_cfg.get("buy_limit_buffer_pct", 0.3) / 100  # match strategy.json default
    sell_buffer_pct = exec_cfg.get("sell_limit_buffer_pct", 0.3) / 100

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
        msg = mismatch_warning + f"TRADEBOT [{mode_label}] // {today_str} - Executed at open\n"
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
