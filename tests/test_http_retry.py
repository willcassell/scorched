"""Tests for the HTTP retry wrapper (http_retry.py)."""
from unittest.mock import MagicMock, patch

import pytest
import requests

from scorched.http_retry import is_transient_error, retry_call, retry_get


# ---------- is_transient_error ----------

class TestIsTransientError:
    def test_timeout_is_transient(self):
        assert is_transient_error(requests.exceptions.Timeout()) is True

    def test_connection_error_is_transient(self):
        assert is_transient_error(ConnectionError("reset")) is True

    def test_requests_connection_error_is_transient(self):
        assert is_transient_error(requests.exceptions.ConnectionError()) is True

    def test_http_500_is_transient(self):
        resp = MagicMock()
        resp.status_code = 500
        exc = requests.exceptions.HTTPError(response=resp)
        assert is_transient_error(exc) is True

    def test_http_502_is_transient(self):
        resp = MagicMock()
        resp.status_code = 502
        exc = requests.exceptions.HTTPError(response=resp)
        assert is_transient_error(exc) is True

    def test_http_503_is_transient(self):
        resp = MagicMock()
        resp.status_code = 503
        exc = requests.exceptions.HTTPError(response=resp)
        assert is_transient_error(exc) is True

    def test_http_400_not_transient(self):
        resp = MagicMock()
        resp.status_code = 400
        exc = requests.exceptions.HTTPError(response=resp)
        assert is_transient_error(exc) is False

    def test_http_401_not_transient(self):
        resp = MagicMock()
        resp.status_code = 401
        exc = requests.exceptions.HTTPError(response=resp)
        assert is_transient_error(exc) is False

    def test_http_404_not_transient(self):
        resp = MagicMock()
        resp.status_code = 404
        exc = requests.exceptions.HTTPError(response=resp)
        assert is_transient_error(exc) is False

    def test_http_429_not_transient(self):
        resp = MagicMock()
        resp.status_code = 429
        exc = requests.exceptions.HTTPError(response=resp)
        assert is_transient_error(exc) is False

    def test_generic_timeout_string_is_transient(self):
        assert is_transient_error(Exception("Connection timeout")) is True

    def test_generic_unrelated_not_transient(self):
        assert is_transient_error(ValueError("bad value")) is False


# ---------- retry_get ----------

class TestRetryGet:
    @patch("scorched.http_retry.time.sleep")
    @patch("scorched.http_retry.requests.get")
    def test_success_on_first_attempt(self, mock_get, mock_sleep):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp

        result = retry_get("https://example.com", label="test")

        assert result is resp
        mock_get.assert_called_once()
        mock_sleep.assert_not_called()

    @patch("scorched.http_retry.time.sleep")
    @patch("scorched.http_retry.requests.get")
    def test_retries_on_transient_then_succeeds(self, mock_get, mock_sleep):
        good_resp = MagicMock()
        good_resp.raise_for_status = MagicMock()

        bad_resp = MagicMock()
        bad_resp.status_code = 503
        bad_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=bad_resp
        )

        mock_get.side_effect = [bad_resp, bad_resp, good_resp]

        result = retry_get("https://example.com", label="test")

        assert result is good_resp
        assert mock_get.call_count == 3
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(3)

    @patch("scorched.http_retry.time.sleep")
    @patch("scorched.http_retry.requests.get")
    def test_non_transient_raises_immediately(self, mock_get, mock_sleep):
        bad_resp = MagicMock()
        bad_resp.status_code = 404
        bad_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=bad_resp
        )
        mock_get.return_value = bad_resp

        with pytest.raises(requests.exceptions.HTTPError):
            retry_get("https://example.com", label="test")

        mock_get.assert_called_once()
        mock_sleep.assert_not_called()

    @patch("scorched.http_retry.time.sleep")
    @patch("scorched.http_retry.requests.get")
    def test_exhausts_retries_then_raises(self, mock_get, mock_sleep):
        bad_resp = MagicMock()
        bad_resp.status_code = 500
        bad_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=bad_resp
        )
        mock_get.return_value = bad_resp

        with pytest.raises(requests.exceptions.HTTPError):
            retry_get("https://example.com", label="test")

        assert mock_get.call_count == 4  # 1 initial + 3 retries
        assert mock_sleep.call_count == 3

    @patch("scorched.http_retry.time.sleep")
    @patch("scorched.http_retry.requests.get")
    def test_passes_kwargs_through(self, mock_get, mock_sleep):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp

        retry_get(
            "https://example.com",
            label="test",
            headers={"X-Custom": "yes"},
            timeout=30,
            params={"q": "hello"},
        )

        mock_get.assert_called_once_with(
            "https://example.com",
            headers={"X-Custom": "yes"},
            timeout=30,
            params={"q": "hello"},
        )


# ---------- retry_call ----------

class TestRetryCall:
    @patch("scorched.http_retry.time.sleep")
    def test_success_on_first_attempt(self, mock_sleep):
        func = MagicMock(return_value="data")

        result = retry_call(func, "arg1", label="test")

        assert result == "data"
        func.assert_called_once_with("arg1")
        mock_sleep.assert_not_called()

    @patch("scorched.http_retry.time.sleep")
    def test_retries_on_transient_then_succeeds(self, mock_sleep):
        func = MagicMock(
            side_effect=[requests.exceptions.Timeout(), "data"]
        )

        result = retry_call(func, "arg1", label="test")

        assert result == "data"
        assert func.call_count == 2
        mock_sleep.assert_called_once_with(1)

    @patch("scorched.http_retry.time.sleep")
    def test_non_transient_raises_immediately(self, mock_sleep):
        func = MagicMock(side_effect=ValueError("bad"))

        with pytest.raises(ValueError, match="bad"):
            retry_call(func, "arg1", label="test")

        func.assert_called_once()
        mock_sleep.assert_not_called()

    @patch("scorched.http_retry.time.sleep")
    def test_exhausts_retries_then_raises(self, mock_sleep):
        func = MagicMock(side_effect=ConnectionError("refused"))

        with pytest.raises(ConnectionError):
            retry_call(func, "arg1", label="test")

        assert func.call_count == 4
        assert mock_sleep.call_count == 3

    @patch("scorched.http_retry.time.sleep")
    def test_passes_args_and_kwargs(self, mock_sleep):
        func = MagicMock(return_value=42)

        result = retry_call(func, "a", "b", label="test", key="val")

        # label should NOT be passed to the inner function
        func.assert_called_once_with("a", "b", key="val")
        assert result == 42
