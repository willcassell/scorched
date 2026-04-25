"""Onboarding wizard API — key validation, config save, status check."""
import hmac
import logging
import os
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from ..config import settings
from ..services.strategy import save_strategy_json

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


def _onboarding_open() -> bool:
    """Return True if onboarding has not yet been completed."""
    return not Path(settings.onboarding_completed_path).exists()


def require_bootstrap_token(x_bootstrap_token: str = Header(default="")):
    """Guard for onboarding routes. Requires BOOTSTRAP_TOKEN header until first save completes.

    After /save succeeds, the sentinel file is written and all onboarding routes return 410.
    """
    if not _onboarding_open():
        raise HTTPException(status_code=410, detail="Onboarding already completed; route disabled")
    expected = settings.bootstrap_token
    if not expected:
        raise HTTPException(status_code=503, detail="BOOTSTRAP_TOKEN unset on server")
    if not hmac.compare_digest(x_bootstrap_token, expected):
        raise HTTPException(status_code=403, detail="Incorrect bootstrap token")

# Allowed env keys the wizard can write (whitelist for safety)
_ALLOWED_ENV_KEYS = {
    "ANTHROPIC_API_KEY",
    "FRED_API_KEY",
    "POLYGON_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
    "TWELVEDATA_API_KEY",
    "FINNHUB_API_KEY",
    "BROKER_MODE",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "STARTING_CAPITAL",
    "SETTINGS_PIN",
}


# ── Schemas ───────────────────────────────────────────────────────────────────


class ValidateKeyRequest(BaseModel):
    service: str
    key: str
    secret: Optional[str] = None


class SaveRequest(BaseModel):
    env: dict[str, str]
    strategy: dict
    confirm_live: bool = False


# ── Key validation ────────────────────────────────────────────────────────────


async def _validate_anthropic(key: str) -> tuple[bool, str]:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        if resp.status_code == 200:
            return True, "Connected successfully"
        if resp.status_code == 401:
            return False, "Invalid API key"
        return False, f"Unexpected response: {resp.status_code}"


async def _validate_fred(key: str) -> tuple[bool, str]:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://api.stlouisfed.org/fred/series",
            params={"series_id": "DFF", "api_key": key, "file_type": "json"},
        )
        if resp.status_code == 200:
            return True, "Connected to FRED"
        return False, f"Invalid key or error: {resp.status_code}"


async def _validate_polygon(key: str) -> tuple[bool, str]:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"https://api.polygon.io/v2/aggs/ticker/AAPL/prev?apiKey={key}"
        )
        if resp.status_code == 200:
            return True, "Connected to Polygon"
        if resp.status_code == 403:
            return False, "Invalid API key"
        return False, f"Error: {resp.status_code}"


async def _validate_alpha_vantage(key: str) -> tuple[bool, str]:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://www.alphavantage.co/query",
            params={
                "function": "RSI",
                "symbol": "AAPL",
                "interval": "daily",
                "time_period": "14",
                "series_type": "close",
                "apikey": key,
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            if "Error Message" in data or "Note" in data:
                return False, "Invalid key or rate limited"
            return True, "Connected to Alpha Vantage"
        return False, f"Error: {resp.status_code}"


async def _validate_twelvedata(key: str) -> tuple[bool, str]:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://api.twelvedata.com/rsi",
            params={"symbol": "AAPL", "interval": "1day", "time_period": 14, "apikey": key},
        )
        data = resp.json()
        if data.get("code") == 401 or "unauthorized" in str(data).lower():
            return False, "Invalid API key"
        if data.get("values"):
            return True, "Connected — RSI data available"
        return False, data.get("message", "Unknown error")


async def _validate_finnhub(key: str) -> tuple[bool, str]:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": "AAPL", "token": key},
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("c", 0) > 0:
                return True, "Connected to Finnhub"
            return False, "Invalid key (no data returned)"
        if resp.status_code == 401:
            return False, "Invalid API key"
        return False, f"Error: {resp.status_code}"


async def _validate_alpaca(key: str, secret: str) -> tuple[bool, str]:
    # Use Alpaca REST API directly to avoid sync SDK issues
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://paper-api.alpaca.markets/v2/account",
            headers={
                "APCA-API-KEY-ID": key,
                "APCA-API-SECRET-KEY": secret,
            },
        )
        if resp.status_code == 200:
            acct = resp.json()
            return True, f"Connected — ${float(acct.get('equity', 0)):,.2f} equity"
        if resp.status_code in (401, 403):
            return False, "Invalid API key or secret"
        return False, f"Error: {resp.status_code}"


_VALIDATORS = {
    "anthropic": lambda r: _validate_anthropic(r.key),
    "fred": lambda r: _validate_fred(r.key),
    "polygon": lambda r: _validate_polygon(r.key),
    "alpha_vantage": lambda r: _validate_alpha_vantage(r.key),
    "twelvedata": lambda r: _validate_twelvedata(r.key),
    "finnhub": lambda r: _validate_finnhub(r.key),
    "alpaca": lambda r: _validate_alpaca(r.key, r.secret or ""),
}


@router.post("/validate-key", dependencies=[Depends(require_bootstrap_token)])
async def validate_key(req: ValidateKeyRequest):
    validator = _VALIDATORS.get(req.service)
    if not validator:
        raise HTTPException(400, f"Unknown service: {req.service}")
    try:
        valid, message = await validator(req)
        return {"valid": valid, "message": message}
    except httpx.TimeoutException:
        return {"valid": False, "message": "Connection timed out"}
    except Exception as e:
        logger.exception("Key validation error for %s", req.service)
        return {"valid": False, "message": f"Validation error: {e}"}


# ── Save configuration ────────────────────────────────────────────────────────


def _env_path() -> Path:
    return Path.cwd() / ".env"


def _read_env() -> dict[str, str]:
    """Parse existing .env into a dict, preserving all keys."""
    path = _env_path()
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def _write_env(data: dict[str, str]) -> None:
    """Write .env with comments and grouping."""
    lines = [
        "# Generated by Scorched Onboarding Wizard",
        "",
        "# Required",
        f"ANTHROPIC_API_KEY={data.get('ANTHROPIC_API_KEY', '')}",
        f"DATABASE_URL={data.get('DATABASE_URL', 'postgresql+asyncpg://scorched:scorched@localhost:5432/scorched')}",
        "",
        "# Portfolio",
        f"STARTING_CAPITAL={data.get('STARTING_CAPITAL', '100000')}",
        "",
        "# Data Sources",
        f"FRED_API_KEY={data.get('FRED_API_KEY', '')}",
        f"POLYGON_API_KEY={data.get('POLYGON_API_KEY', '')}",
        f"ALPHA_VANTAGE_API_KEY={data.get('ALPHA_VANTAGE_API_KEY', '')}",
        f"TWELVEDATA_API_KEY={data.get('TWELVEDATA_API_KEY', '')}",
        f"FINNHUB_API_KEY={data.get('FINNHUB_API_KEY', '')}",
        "",
        "# Broker",
        f"BROKER_MODE={data.get('BROKER_MODE', 'paper')}",
        f"ALPACA_API_KEY={data.get('ALPACA_API_KEY', '')}",
        f"ALPACA_SECRET_KEY={data.get('ALPACA_SECRET_KEY', '')}",
        "",
        "# Server",
        f"HOST={data.get('HOST', '0.0.0.0')}",
        f"PORT={data.get('PORT', '8000')}",
    ]

    # Preserve any extra keys not in our template
    known = {
        "ANTHROPIC_API_KEY", "DATABASE_URL", "STARTING_CAPITAL",
        "FRED_API_KEY", "POLYGON_API_KEY", "ALPHA_VANTAGE_API_KEY",
        "TWELVEDATA_API_KEY", "FINNHUB_API_KEY", "BROKER_MODE", "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY", "HOST", "PORT", "SETTINGS_PIN",
    }
    extras = {k: v for k, v in data.items() if k not in known}
    if extras:
        lines.append("")
        lines.append("# Additional")
        for k, v in extras.items():
            lines.append(f"{k}={v}")

    if data.get("SETTINGS_PIN"):
        lines.append("")
        lines.append(f"SETTINGS_PIN={data['SETTINGS_PIN']}")

    path = _env_path()
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Docker volume may not allow chmod


@router.post("/save", dependencies=[Depends(require_bootstrap_token)])
async def save_config(req: SaveRequest):
    # Safety: require explicit confirmation for live trading
    broker_mode = req.env.get("BROKER_MODE", "paper")
    if broker_mode == "alpaca_live" and not req.confirm_live:
        raise HTTPException(
            400,
            "Live trading requires explicit confirmation. Set confirm_live: true.",
        )

    # Filter env keys to whitelist
    filtered_env = {k: v for k, v in req.env.items() if k in _ALLOWED_ENV_KEYS}

    # Merge with existing .env (preserve DATABASE_URL etc.)
    existing = _read_env()
    existing.update(filtered_env)

    try:
        _write_env(existing)
    except OSError as e:
        raise HTTPException(500, f"Failed to write .env: {e}")

    # Save strategy
    if req.strategy:
        try:
            save_strategy_json(req.strategy)
        except OSError as e:
            raise HTTPException(500, f"Failed to write strategy.json: {e}")

    # Mark onboarding as completed — subsequent calls to onboarding routes return 410
    try:
        Path(settings.onboarding_completed_path).touch(exist_ok=True)
    except OSError as e:
        logger.warning("Could not write onboarding sentinel: %s", e)

    return {
        "success": True,
        "restart_required": True,
        "message": "Configuration saved. Restart the server to apply changes.",
    }


# ── Status check ──────────────────────────────────────────────────────────────


@router.get("/status", dependencies=[Depends(require_bootstrap_token)])
async def onboarding_status():
    return {
        "configured_keys": {
            "anthropic": bool(settings.anthropic_api_key),
            "fred": bool(settings.fred_api_key),
            "alpha_vantage": bool(settings.alpha_vantage_api_key),
            "twelvedata": bool(settings.twelvedata_api_key),
            "finnhub": bool(settings.finnhub_api_key),
            "alpaca": bool(settings.alpaca_api_key and settings.alpaca_secret_key),
        },
        "broker_mode": settings.broker_mode,
        "starting_capital": float(settings.starting_capital),
    }
