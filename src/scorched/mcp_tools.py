"""MCP tool definitions. openclaw connects to /mcp and calls these tools."""
import json
from datetime import date, datetime, timezone
from decimal import Decimal

from mcp.server.fastmcp import FastMCP

from .broker import get_broker
from .database import AsyncSessionLocal
from .schemas import ConfirmTradeRequest, RejectTradeRequest
from .services import portfolio as portfolio_svc
from .services import recommender as recommender_svc
from .services.playbook import get_playbook
from .config import settings
from .services.research import fetch_opening_prices, fetch_market_eod

mcp = FastMCP("tradebot", instructions=(
    "Simulated stock trading bot. Call get_recommendations each morning to research stocks and "
    "generate trade picks, confirm_trade after each executed trade, and get_portfolio for a "
    "current snapshot."
))


def _check_pin(pin: str | None) -> str | None:
    """Validate owner PIN for MCP mutation tools.
    Returns an error JSON string if PIN is required but missing/wrong, else None.
    """
    if not settings.settings_pin:
        return None  # No PIN configured — allow all
    if pin != settings.settings_pin:
        return json.dumps({"error": "Incorrect or missing PIN. Set the 'pin' parameter to your SETTINGS_PIN."})
    return None


def _decimal_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def _to_json(obj) -> str:
    if hasattr(obj, "model_dump"):
        return json.dumps(obj.model_dump(), default=_decimal_default)
    return json.dumps(obj, default=_decimal_default)


@mcp.tool(
    description="Research stocks and generate up to 3 buy/sell trade recommendations. "
    "Call this each morning before market open. Tradebot autonomously fetches market context "
    "(index levels, sector moves, upcoming earnings, macro news) — no context needed from you. "
    "Returns cached results if already called today. "
    "Requires the owner PIN if SETTINGS_PIN is configured.",
)
async def get_recommendations(date: str | None = None, pin: str | None = None) -> str:
    """
    Args:
        date: ISO date string (YYYY-MM-DD). Defaults to today.
        pin: Owner PIN. Required if SETTINGS_PIN is configured.
    """
    err = _check_pin(pin)
    if err:
        return err
    session_date = None
    if date:
        from datetime import date as date_cls
        session_date = date_cls.fromisoformat(date)

    async with AsyncSessionLocal() as db:
        result = await recommender_svc.generate_recommendations(
            db, session_date=session_date
        )
    return _to_json(result)


@mcp.tool(
    description="Fetch the actual opening auction prices for a list of symbols on a given date. "
    "Call this at ~9:45 AM ET after market open, then pass each price to confirm_trade "
    "for accurate simulation fills. Returns None for a symbol if it did not trade that day.",
)
async def get_opening_prices(symbols: list[str], date: str | None = None) -> str:
    """
    Args:
        symbols: List of ticker symbols, e.g. ["AAPL", "NVDA"].
        date: ISO date string (YYYY-MM-DD). Defaults to today.
    """
    from datetime import date as date_cls
    from .tz import market_today
    trade_date = date_cls.fromisoformat(date) if date else market_today()
    prices = await fetch_opening_prices(symbols, trade_date)
    return json.dumps({"date": trade_date.isoformat(), "opening_prices": prices})


@mcp.tool(
    description="Confirm that a recommended trade was executed. Routes through the configured broker "
    "(paper or Alpaca) for order submission and fill tracking. Updates portfolio cash and positions. "
    "Call this once per trade after execution. "
    "Requires the owner PIN if SETTINGS_PIN is configured.",
)
async def confirm_trade(recommendation_id: int, execution_price: float, shares: float, pin: str | None = None) -> str:
    """
    Args:
        recommendation_id: The id from a get_recommendations response.
        execution_price: Actual fill price per share.
        shares: Number of shares traded (may differ slightly from suggested quantity).
        pin: Owner PIN. Required if SETTINGS_PIN is configured.
    """
    err = _check_pin(pin)
    if err:
        return err
    from sqlalchemy import select
    from .models import TradeRecommendation

    exec_price = Decimal(str(execution_price))
    exec_shares = Decimal(str(shares))

    async with AsyncSessionLocal() as db:
        rec = (
            await db.execute(
                select(TradeRecommendation).where(TradeRecommendation.id == recommendation_id)
            )
        ).scalars().first()

        if rec is None:
            return json.dumps({"error": f"No recommendation found with id {recommendation_id}"})
        if rec.status != "pending":
            return json.dumps({"error": f"Recommendation {recommendation_id} is already {rec.status}"})

        broker = get_broker(db)

        try:
            if rec.action == "buy":
                result = await broker.submit_buy(
                    symbol=rec.symbol,
                    qty=exec_shares,
                    limit_price=exec_price,
                    recommendation_id=recommendation_id,
                )
            else:
                result = await broker.submit_sell(
                    symbol=rec.symbol,
                    qty=exec_shares,
                    limit_price=exec_price,
                    recommendation_id=recommendation_id,
                )
        except Exception as exc:
            return json.dumps({"error": f"Broker order failed for {rec.action} {rec.symbol}: {exc}"})

        if result["status"] != "filled":
            return json.dumps({"error": f"Order not filled: status={result['status']} for {rec.symbol}"})

    return _to_json(result)


@mcp.tool(
    description="Get the current portfolio state: cash balance, all positions with live prices, "
    "unrealized P&L, tax classification, and overall return.",
)
async def get_portfolio() -> str:
    async with AsyncSessionLocal() as db:
        result = await portfolio_svc.get_portfolio_state(db)
    return _to_json(result)


@mcp.tool(
    description="Mark a pending recommendation as rejected (you decided not to trade it). "
    "This keeps the audit trail clean. "
    "Requires the owner PIN if SETTINGS_PIN is configured.",
)
async def reject_recommendation(recommendation_id: int, reason: str | None = None, pin: str | None = None) -> str:
    """
    Args:
        recommendation_id: The id from a get_recommendations response.
        reason: Optional reason for rejection.
        pin: Owner PIN. Required if SETTINGS_PIN is configured.
    """
    err = _check_pin(pin)
    if err:
        return err
    from sqlalchemy import select
    from .models import TradeRecommendation

    async with AsyncSessionLocal() as db:
        rec = (
            await db.execute(
                select(TradeRecommendation).where(TradeRecommendation.id == recommendation_id)
            )
        ).scalars().first()

        if rec is None:
            return json.dumps({"error": f"No recommendation found with id {recommendation_id}"})
        if rec.status != "pending":
            return json.dumps({"error": f"Recommendation {recommendation_id} is already {rec.status}"})

        rec.status = "rejected"
        await db.commit()

    return json.dumps({"recommendation_id": recommendation_id, "status": "rejected", "reason": reason})


@mcp.tool(
    description="Fetch end-of-day market performance: major indices (S&P 500, NASDAQ, Dow, Russell 2000) "
    "and all S&P 500 sector ETFs with price and day change %. Call after 4 PM ET.",
)
async def get_market_summary(date: str | None = None) -> str:
    """
    Args:
        date: ISO date string (YYYY-MM-DD). Defaults to today.
    """
    from datetime import date as date_cls
    from .tz import market_today
    target_date = date_cls.fromisoformat(date) if date else market_today()
    result = await fetch_market_eod(target_date)
    return json.dumps({"date": target_date.isoformat(), **result})


@mcp.tool(
    description="Read the bot's current trading playbook — the living strategy document "
    "that accumulates learnings from past trades.",
)
async def read_playbook() -> str:
    async with AsyncSessionLocal() as db:
        pb = await get_playbook(db)
    return json.dumps({
        "version": pb.version,
        "updated_at": pb.updated_at.isoformat(),
        "content": pb.content,
    })
