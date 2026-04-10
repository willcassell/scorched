import hmac

from fastapi import Header, HTTPException

from ..config import settings


def require_owner_pin(x_owner_pin: str = Header(default="")):
    """Guard for mutation endpoints. Pass the owner PIN in the X-Owner-Pin header.
    No-op when SETTINGS_PIN is unset (backward compat / local dev).
    """
    if settings.settings_pin and not hmac.compare_digest(x_owner_pin, settings.settings_pin):
        raise HTTPException(status_code=403, detail="Incorrect PIN")
