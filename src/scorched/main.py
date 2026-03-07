"""FastAPI app with MCP mounted at /mcp."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from sqlalchemy import select

from .api import costs, market, playbook, portfolio, recommendations, strategy, trades
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
    async with mcp.session_manager.run():
        yield

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


@app.get("/")
async def dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/strategy")
async def strategy_settings():
    return FileResponse(STATIC_DIR / "strategy.html")


@app.get("/health")
async def health():
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(select(Portfolio))
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {e}"
    return {"status": "ok", "db": db_status}


