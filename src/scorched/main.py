"""FastAPI app with MCP mounted at /mcp."""
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select

from .api import broker_status, costs, guidance, intraday, market, onboarding, playbook, portfolio, prefetch, recommendations, strategy, system, trades
from .broker.pending_fills import get_pending_fills, release_stale_pending_fills
from .config import settings
from .database import AsyncSessionLocal
from .models import Portfolio
from .mcp_tools import mcp

STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MIN_LIVE_PIN_LEN = 16


def _assert_auth_safe() -> None:
    """Refuse to boot without a PIN in any mode that exposes mutation endpoints.

    Live mode additionally requires a strong PIN (>=16 chars).
    """
    pin = settings.settings_pin or ""
    if not pin:
        raise RuntimeError(
            "SETTINGS_PIN is unset — refusing to start. "
            "Mutation endpoints would be open. Set SETTINGS_PIN in .env."
        )
    if settings.broker_mode == "alpaca_live":
        if len(pin) < MIN_LIVE_PIN_LEN:
            raise RuntimeError(
                f"SETTINGS_PIN too short (len {len(pin)}) for alpaca_live — "
                f"need at least {MIN_LIVE_PIN_LEN} characters"
            )
        if not settings.live_trading_enabled:
            raise RuntimeError(
                "BROKER_MODE=alpaca_live but LIVE_TRADING_ENABLED is not true — "
                "refusing to start. Set LIVE_TRADING_ENABLED=true in .env to enable live trading."
            )


# Keep old name as alias for backward-compat with any existing references
_assert_live_mode_safe = _assert_auth_safe


# FastAPI doesn't propagate lifespan to mounted sub-apps, so we manually start
# the MCP session manager's task group inside FastAPI's own lifespan.
# Portfolio seeding also happens here (replaces the deprecated @on_event("startup")).
@asynccontextmanager
async def lifespan(app: FastAPI):
    _assert_auth_safe()
    if settings.broker_mode == "alpaca_live":
        logging.warning("LIVE TRADING ENABLED — submitting real orders to Alpaca")
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(select(Portfolio))).scalars().first()
        if existing is None:
            db.add(Portfolio(cash_balance=settings.starting_capital, starting_capital=settings.starting_capital))
            await db.commit()
            logging.info("Initialized portfolio with $%s", settings.starting_capital)
        else:
            logging.info("Portfolio exists: $%s cash", existing.cash_balance)
    # Cleanup old API call log records (>30 days)
    async with AsyncSessionLocal() as db:
        from .api_tracker import cleanup_old_records
        await cleanup_old_records(db)
    # Crash recovery: reconcile any pending fills from previous run, then
    # release anything reconcile couldn't resolve so a leaked reservation
    # doesn't strand cash forever.
    await _reconcile_pending_fills()
    await _release_stale_pending_fills_with_alert()
    async with mcp.session_manager.run():
        yield


async def _reconcile_pending_fills() -> None:
    """Reconcile pending fills on startup using Alpaca order status.

    With fire-and-forget orders, pending fills are written at submission
    time (before Alpaca confirms).  On restart we must check Alpaca for
    the actual fill status and price — never blindly replay the placeholder
    limit price from the pending record.
    """
    from .broker.alpaca import reconcile_pending_orders
    from .services.telegram import send_telegram

    try:
        async with AsyncSessionLocal() as db:
            pending = await get_pending_fills(db)
        if not pending:
            return

        logging.critical(
            "STARTUP RECONCILIATION: Found %d pending order(s) from previous run", len(pending)
        )

        async with AsyncSessionLocal() as db:
            results = await reconcile_pending_orders(db)

        if results:
            filled = [r for r in results if r["status"] == "filled"]
            other = [r for r in results if r["status"] != "filled"]
            parts = ["TRADEBOT // STARTUP RECONCILIATION"]
            if filled:
                parts.append(f"Recorded {len(filled)} fill(s):")
                parts.extend(
                    f"  - {r['action'].upper()} {r['symbol']} {r['filled_qty']}sh @ ${r['filled_price']}"
                    for r in filled
                )
            if other:
                parts.append(f"Not filled ({len(other)}):")
                parts.extend(f"  - {r['action'].upper()} {r['symbol']}: {r['status']}" for r in other)
            await send_telegram("\n".join(parts))
    except Exception as exc:
        logging.error("Startup reconciliation failed: %s", exc, exc_info=True)
        await send_telegram(f"TRADEBOT // STARTUP RECONCILIATION FAILED\n{exc}")


async def _release_stale_pending_fills_with_alert() -> None:
    """Release stale pending fills and alert the operator.

    Runs after `_reconcile_pending_fills` so anything still pending is a true
    lifecycle leak — the reconciler couldn't resolve it. The release does not
    touch Alpaca; the next reconcile cycle will recover any real order via the
    stored client_order_id. The point is to stop a leak from blocking new buys
    against the cash-reservation gate.
    """
    from .services.telegram import send_telegram

    try:
        async with AsyncSessionLocal() as db:
            released = await release_stale_pending_fills(db)
        if not released:
            return
        lines = [f"TRADEBOT // STARTUP RELEASED {len(released)} STALE RESERVATION(S)"]
        for r in released:
            age_h = (r["age_seconds"] or 0) / 3600
            lines.append(
                f"  - {r['action'].upper()} {r['symbol']} {r['qty']}sh @ ${r['limit_price']} "
                f"(age {age_h:.1f}h, client_oid={r['client_order_id']})"
            )
        lines.append("Cash reservation freed; next reconcile will recover real orders.")
        await send_telegram("\n".join(lines))
    except Exception as exc:
        logging.error("Stale-pending release failed: %s", exc, exc_info=True)
        await send_telegram(f"TRADEBOT // STALE-PENDING RELEASE FAILED\n{exc}")


app = FastAPI(
    title="Tradebot",
    description="Simulated stock trading MCP server",
    version="0.1.0",
    lifespan=lifespan,
)

# Mount MCP server — MCP clients connect to http://host:8000/mcp
# streamable_http_app() internally registers at /mcp; set to "/" so FastAPI
# mount at "/mcp" gives the correct full path without double-prefix.
mcp.settings.streamable_http_path = "/"
app.mount("/mcp", mcp.streamable_http_app())

# REST API routers
app.include_router(portfolio.router, prefix="/api/v1")
app.include_router(recommendations.router, prefix="/api/v1")
app.include_router(trades.router, prefix="/api/v1")
app.include_router(playbook.router, prefix="/api/v1")
app.include_router(strategy.router, prefix="/api/v1")
app.include_router(guidance.router, prefix="/api/v1")
app.include_router(costs.router, prefix="/api/v1")
app.include_router(market.router, prefix="/api/v1")
app.include_router(broker_status.router, prefix="/api/v1")
app.include_router(system.router, prefix="/api/v1")
app.include_router(onboarding.router, prefix="/api/v1")
app.include_router(intraday.router, prefix="/api/v1")
app.include_router(prefetch.router, prefix="/api/v1")


@app.get("/")

async def dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/strategy")
async def strategy_settings():
    return FileResponse(STATIC_DIR / "strategy.html")


@app.get("/guidance")
async def guidance_page():
    return FileResponse(STATIC_DIR / "guidance.html")


@app.get("/system")
async def system_page():
    return FileResponse(STATIC_DIR / "system.html")


@app.get("/analysis")
async def analysis_page():
    return FileResponse(STATIC_DIR / "analysis.html")


@app.get("/onboarding")
async def onboarding_page():
    return FileResponse(STATIC_DIR / "onboarding.html")


@app.get("/live")
async def liveness():
    """Process-only liveness check. Returns 200 if the app is running, regardless of dependencies."""
    return {"status": "ok"}


@app.get("/health")
async def health():
    """Dependency-aware readiness check. Returns 503 if the database is unreachable."""
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(select(Portfolio))
    except Exception:
        logger.exception("Health check database probe failed")
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "db": "error"},
        )
    return {"status": "ok", "db": "connected"}


