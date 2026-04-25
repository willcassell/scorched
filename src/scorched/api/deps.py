import hmac

from fastapi import Header, HTTPException

from ..config import settings


def require_owner_pin(x_owner_pin: str = Header(default="")):
    """Guard for sensitive endpoints. Requires X-Owner-Pin header to match SETTINGS_PIN.

    SETTINGS_PIN must be configured at startup — _assert_auth_safe enforces this.
    No fail-open path here.
    """
    pin = settings.settings_pin or ""
    if not pin:
        raise HTTPException(status_code=503, detail="Server misconfigured: SETTINGS_PIN unset")
    if not hmac.compare_digest(x_owner_pin, pin):
        raise HTTPException(status_code=403, detail="Incorrect PIN")
