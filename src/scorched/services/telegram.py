"""Async Telegram sender for use within the FastAPI process."""
import logging

import httpx

from ..config import settings

logger = logging.getLogger(__name__)


async def send_telegram(text: str) -> bool:
    """Send a message via Telegram Bot API. Returns True on success."""
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        logger.debug("Telegram not configured — skipping message")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "chat_id": chat_id,
                "text": text[:4096],  # Telegram message limit
            })
            resp.raise_for_status()
            return True
    except httpx.HTTPStatusError as exc:
        logger.warning("Telegram send failed: status=%s", exc.response.status_code)
        return False
    except Exception:
        # Log without exc_info to avoid leaking the bot token from the URL
        logger.warning("Telegram send failed: status=%s", "unknown")
        return False
