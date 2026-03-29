"""Tests for API call tracker."""
import pytest
import time
from scorched.api_tracker import ApiCallTracker, track_call, compute_service_health


class TestTrackCall:
    def test_records_success(self):
        tracker = ApiCallTracker()
        with track_call(tracker, "yfinance", "history", symbol="AAPL"):
            time.sleep(0.01)
        assert len(tracker.records) == 1
        rec = tracker.records[0]
        assert rec["service"] == "yfinance"
        assert rec["endpoint"] == "history"
        assert rec["status"] == "success"
        assert rec["symbol"] == "AAPL"
        assert rec["response_time_ms"] >= 10

    def test_records_error(self):
        tracker = ApiCallTracker()
        try:
            with track_call(tracker, "polygon", "news", symbol="NVDA"):
                raise ConnectionError("Connection refused")
        except ConnectionError:
            pass
        assert len(tracker.records) == 1
        rec = tracker.records[0]
        assert rec["status"] == "error"
        assert "Connection refused" in rec["error_message"]

    def test_records_timeout(self):
        tracker = ApiCallTracker()
        try:
            with track_call(tracker, "edgar", "submissions", symbol="AAPL"):
                raise TimeoutError("Request timed out")
        except TimeoutError:
            pass
        rec = tracker.records[0]
        assert rec["status"] == "timeout"

    def test_records_rate_limit(self):
        tracker = ApiCallTracker()
        try:
            with track_call(tracker, "polygon", "news"):
                raise Exception("429 Too Many Requests")
        except Exception:
            pass
        rec = tracker.records[0]
        assert rec["status"] == "rate_limited"

    def test_multiple_calls(self):
        tracker = ApiCallTracker()
        with track_call(tracker, "fred", "series"):
            pass
        with track_call(tracker, "fred", "series"):
            pass
        assert len(tracker.records) == 2


class TestComputeServiceHealth:
    def test_all_success(self):
        records = [
            {"service": "yfinance", "status": "success", "response_time_ms": 100, "error_message": None, "created_at": "2026-03-28T12:00:00"},
            {"service": "yfinance", "status": "success", "response_time_ms": 150, "error_message": None, "created_at": "2026-03-28T12:01:00"},
        ]
        health = compute_service_health(records)
        assert health["yfinance"]["status"] == "green"
        assert health["yfinance"]["today_pct"] == 100.0

    def test_degraded_service(self):
        records = [
            {"service": "polygon", "status": "success", "response_time_ms": 100, "error_message": None, "created_at": "2026-03-28T12:00:00"},
            {"service": "polygon", "status": "rate_limited", "response_time_ms": 50, "error_message": "429", "created_at": "2026-03-28T12:01:00"},
            {"service": "polygon", "status": "rate_limited", "response_time_ms": 50, "error_message": "429", "created_at": "2026-03-28T12:02:00"},
        ]
        health = compute_service_health(records)
        assert health["polygon"]["status"] == "yellow"

    def test_down_service(self):
        records = [
            {"service": "edgar", "status": "error", "response_time_ms": 10000, "error_message": "timeout", "created_at": "2026-03-28T12:00:00"},
        ]
        health = compute_service_health(records)
        assert health["edgar"]["status"] == "red"

    def test_empty_records(self):
        health = compute_service_health([])
        assert health == {}
