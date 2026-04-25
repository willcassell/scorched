"""Broker abstraction layer — paper or live trading."""
from .base import BrokerAdapter
from .paper import PaperBroker

__all__ = ["BrokerAdapter", "PaperBroker", "get_broker"]


def get_broker(db_session):
    """Factory: returns the broker configured in settings.broker_mode."""
    from ..config import settings

    if settings.broker_mode in ("alpaca_paper", "alpaca_live"):
        if settings.broker_mode == "alpaca_live" and not settings.live_trading_enabled:
            raise RuntimeError(
                "Cannot construct AlpacaBroker in alpaca_live mode without LIVE_TRADING_ENABLED=true"
            )
        from alpaca.trading.client import TradingClient
        from .alpaca import AlpacaBroker

        is_paper = settings.broker_mode == "alpaca_paper"
        client = TradingClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
            paper=is_paper,
        )
        return AlpacaBroker(db_session, client)

    return PaperBroker(db_session)
