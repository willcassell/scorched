"""Tests for Finnhub analyst consensus data fetching."""
import pytest
from unittest.mock import MagicMock, patch
from scorched.services.finnhub_data import (
    fetch_analyst_consensus_sync,
    build_analyst_context,
)


def _mock_client():
    client = MagicMock()
    client.recommendation_trends.return_value = [
        MagicMock(
            buy=10, hold=5, sell=2, strong_buy=3, strong_sell=1,
            period="2026-03-01",
        )
    ]
    client.price_target.return_value = MagicMock(
        target_high=200.0, target_low=140.0, target_mean=175.0, target_median=172.0,
    )
    return client


class TestFetchAnalystConsensus:
    def test_returns_data_for_symbol(self):
        client = _mock_client()
        result = fetch_analyst_consensus_sync(["AAPL"], client)
        assert "AAPL" in result
        assert result["AAPL"]["strong_buy"] == 3
        assert result["AAPL"]["buy"] == 10
        assert result["AAPL"]["target_mean"] == 175.0

    def test_empty_on_no_client(self):
        result = fetch_analyst_consensus_sync(["AAPL"], None)
        assert result == {}

    def test_handles_api_error_gracefully(self):
        client = MagicMock()
        client.recommendation_trends.side_effect = Exception("API error")
        client.price_target.side_effect = Exception("API error")
        result = fetch_analyst_consensus_sync(["AAPL"], client)
        assert result.get("AAPL") is None or result == {}


class TestBuildAnalystContext:
    def test_formats_output(self):
        data = {
            "AAPL": {
                "strong_buy": 3, "buy": 10, "hold": 5, "sell": 2, "strong_sell": 1,
                "target_high": 200.0, "target_low": 140.0, "target_mean": 175.0,
            }
        }
        text = build_analyst_context(data)
        assert "AAPL" in text
        assert "Strong Buy: 3" in text
        assert "175" in text

    def test_empty_data_returns_empty(self):
        assert build_analyst_context({}) == ""
