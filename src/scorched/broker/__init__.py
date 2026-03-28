"""Broker abstraction layer — paper or live trading."""
from .base import BrokerAdapter
from .paper import PaperBroker

__all__ = ["BrokerAdapter", "PaperBroker", "get_broker"]


def get_broker(db_session, alpaca_client=None):
    """Factory: returns AlpacaBroker if alpaca_client is provided, else PaperBroker."""
    if alpaca_client is not None:
        from .alpaca import AlpacaBroker
        return AlpacaBroker(db_session, alpaca_client)
    return PaperBroker(db_session)
