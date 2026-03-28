"""Tests for BrokerAdapter ABC — verifies interface contract."""
import pytest
from scorched.broker.base import BrokerAdapter


def test_broker_adapter_cannot_be_instantiated():
    with pytest.raises(TypeError):
        BrokerAdapter()


def test_broker_adapter_defines_required_methods():
    required = {"submit_buy", "submit_sell", "get_positions", "get_account", "get_order_status"}
    abstract_methods = BrokerAdapter.__abstractmethods__
    assert required.issubset(abstract_methods)
