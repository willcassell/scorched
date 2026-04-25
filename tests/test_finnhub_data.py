"""Tests for Finnhub analyst consensus data fetching."""
import pytest
from unittest.mock import MagicMock, patch
from scorched.services.finnhub_data import (
    fetch_analyst_consensus_sync,
    build_analyst_context,
    _normalize_sector,
)


def _mock_client():
    client = MagicMock()
    client.recommendation_trends.return_value = [
        MagicMock(
            buy=10, hold=5, sell=2, strong_buy=3, strong_sell=1,
            period="2026-03-01",
        )
    ]
    return client


class TestFetchAnalystConsensus:
    def test_returns_data_for_symbol(self):
        client = _mock_client()
        result = fetch_analyst_consensus_sync(["AAPL"], client)
        assert "AAPL" in result
        assert result["AAPL"]["strong_buy"] == 3
        assert result["AAPL"]["buy"] == 10

    def test_empty_on_no_client(self):
        result = fetch_analyst_consensus_sync(["AAPL"], None)
        assert result == {}

    def test_handles_api_error_gracefully(self):
        client = MagicMock()
        client.recommendation_trends.side_effect = Exception("API error")
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


class TestNormalizeSector:
    def test_handles_canonical_form_passthrough(self):
        assert _normalize_sector("Technology") == "Technology"

    def test_normalizes_financial_services(self):
        assert _normalize_sector("Financial Services") == "Financials"

    def test_normalizes_health_care(self):
        assert _normalize_sector("Health Care") == "Healthcare"

    def test_normalizes_consumer_cyclical(self):
        assert _normalize_sector("Consumer Cyclical") == "Consumer Discretionary"

    def test_returns_none_on_unknown(self):
        assert _normalize_sector("Software") is None

    def test_returns_none_on_empty(self):
        assert _normalize_sector("") is None
        assert _normalize_sector(None) is None


class TestSectorFallback:
    def test_returns_finnhub_industry_when_available(self):
        """Financial Services from Finnhub normalizes to canonical 'Financials'."""
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "ticker": "GS",
            "finnhubIndustry": "Financial Services",
            "name": "Goldman Sachs",
        }
        with patch("scorched.services.finnhub_data.retry_call", return_value=fake_response), \
             patch("scorched.services.finnhub_data.settings") as mock_s:
            mock_s.finnhub_api_key = "fake-key"
            from scorched.services.finnhub_data import fetch_sector_for_symbol
            sector = fetch_sector_for_symbol("GS")
        assert sector == "Financials"

    def test_normalizes_health_care_to_healthcare(self):
        """Finnhub's 'Health Care' (with space) normalizes to canonical 'Healthcare'."""
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "ticker": "JNJ",
            "finnhubIndustry": "Health Care",
            "name": "Johnson & Johnson",
        }
        with patch("scorched.services.finnhub_data.retry_call", return_value=fake_response), \
             patch("scorched.services.finnhub_data.settings") as mock_s:
            mock_s.finnhub_api_key = "fake-key"
            from scorched.services.finnhub_data import fetch_sector_for_symbol
            sector = fetch_sector_for_symbol("JNJ")
        assert sector == "Healthcare"

    def test_returns_none_for_unrecognized_sector(self):
        """Granular industry names like 'Software' return None (fail closed)."""
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "ticker": "NOW",
            "finnhubIndustry": "Software",
            "name": "ServiceNow",
        }
        with patch("scorched.services.finnhub_data.retry_call", return_value=fake_response), \
             patch("scorched.services.finnhub_data.settings") as mock_s:
            mock_s.finnhub_api_key = "fake-key"
            from scorched.services.finnhub_data import fetch_sector_for_symbol
            sector = fetch_sector_for_symbol("NOW")
        assert sector is None

    def test_returns_none_when_no_industry(self):
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {"ticker": "WEIRD"}
        with patch("scorched.services.finnhub_data.retry_call", return_value=fake_response), \
             patch("scorched.services.finnhub_data.settings") as mock_s:
            mock_s.finnhub_api_key = "fake-key"
            from scorched.services.finnhub_data import fetch_sector_for_symbol
            sector = fetch_sector_for_symbol("WEIRD")
        assert sector is None

    def test_returns_none_when_no_api_key(self):
        with patch("scorched.services.finnhub_data.settings") as mock_s:
            mock_s.finnhub_api_key = ""
            from scorched.services.finnhub_data import fetch_sector_for_symbol
            sector = fetch_sector_for_symbol("AAPL")
        assert sector is None

    def test_returns_none_on_http_error(self):
        fake_response = MagicMock()
        fake_response.status_code = 500
        with patch("scorched.services.finnhub_data.retry_call", return_value=fake_response), \
             patch("scorched.services.finnhub_data.settings") as mock_s:
            mock_s.finnhub_api_key = "fake-key"
            from scorched.services.finnhub_data import fetch_sector_for_symbol
            sector = fetch_sector_for_symbol("AAPL")
        assert sector is None
