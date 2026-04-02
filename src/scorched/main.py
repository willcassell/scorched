"""FastAPI app with MCP mounted at /mcp."""
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from sqlalchemy import select

from .api import broker_status, costs, intraday, market, onboarding, playbook, portfolio, prefetch, recommendations, strategy, system, trades
from .broker.pending_fills import get_pending_fills, remove_pending_fill
from .config import settings
from .database import AsyncSessionLocal
from .models import Portfolio
from .mcp_tools import mcp

STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(level=logging.INFO)

# FastAPI doesn't propagate lifespan to mounted sub-apps, so we manually start
# the MCP session manager's task group inside FastAPI's own lifespan.
# Portfolio seeding also happens here (replaces the deprecated @on_event("startup")).
@asynccontextmanager
async def lifespan(app: FastAPI):
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
    # Crash recovery: reconcile any pending fills from previous run
    await _reconcile_pending_fills()
    async with mcp.session_manager.run():
        yield


async def _reconcile_pending_fills() -> None:
    """Replay pending fills that were confirmed by Alpaca but not yet recorded in DB."""
    from .services.portfolio import apply_buy, apply_sell
    from .services.telegram import send_telegram

    pending = get_pending_fills()
    if not pending:
        return

    logging.critical(
        "CRASH RECOVERY: Found %d pending fill(s) from previous run", len(pending)
    )

    recovered = []
    failed = []
    for fill in pending:
        order_id = fill["order_id"]
        symbol = fill["symbol"]
        action = fill["action"]
        qty = Decimal(fill["qty"])
        price = Decimal(fill["fill_price"])
        rec_id = fill.get("recommendation_id")

        try:
            async with AsyncSessionLocal() as db:
                if action == "buy":
                    await apply_buy(
                        db,
                        recommendation_id=rec_id,
                        symbol=symbol,
                        shares=qty,
                        execution_price=price,
                        executed_at=datetime.now(timezone.utc),
                    )
                elif action == "sell":
                    await apply_sell(
                        db,
                        recommendation_id=rec_id,
                        symbol=symbol,
                        shares=qty,
                        execution_price=price,
                        executed_at=datetime.now(timezone.utc),
                    )
                else:
                    logging.error("Unknown action '%s' in pending fill %s", action, order_id)
                    failed.append(f"{action} {symbol} (unknown action)")
                    continue

            remove_pending_fill(order_id)
            recovered.append(f"{action} {qty} {symbol} @ ${price}")
            logging.info("Recovered pending fill: order=%s %s %s x%s @ %s", order_id, action, symbol, qty, price)

        except ValueError as exc:
            # e.g. insufficient cash for buy, no position for sell
            logging.error(
                "Failed to recover pending fill order=%s: %s", order_id, exc
            )
            failed.append(f"{action} {symbol} ({exc})")
            # Don't remove — leave for manual inspection
        except Exception as exc:
            logging.error(
                "Unexpected error recovering pending fill order=%s: %s", order_id, exc,
                exc_info=True,
            )
            failed.append(f"{action} {symbol} ({type(exc).__name__}: {exc})")

    # Send Telegram summary
    parts = ["TRADEBOT // CRASH RECOVERY"]
    if recovered:
        parts.append(f"Recovered {len(recovered)} fill(s):")
        parts.extend(f"  - {r}" for r in recovered)
    if failed:
        parts.append(f"FAILED to recover {len(failed)} fill(s):")
        parts.extend(f"  - {f}" for f in failed)
        parts.append("Manual intervention required — check /app/logs/pending_fills.json")

    await send_telegram("\n".join(parts))

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


@app.get("/system")
async def system_page():
    return FileResponse(STATIC_DIR / "system.html")


@app.get("/analysis")
async def analysis_page():
    return FileResponse(STATIC_DIR / "analysis.html")


@app.get("/onboarding")
async def onboarding_page():
    return FileResponse(STATIC_DIR / "onboarding.html")


@app.get("/health")
async def health():
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(select(Portfolio))
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {e}"
    return {"status": "ok", "db": db_status}


