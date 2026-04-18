"""api_tracker must redact common API-key patterns before storing error_message."""


def test_redact_apikey_query_param():
    from scorched.api_tracker import _redact_secrets

    msg = "HTTPError: https://www.alphavantage.co/query?function=RSI&symbol=AAPL&apikey=REALKEY123 500 Server Error"
    out = _redact_secrets(msg)
    assert "REALKEY123" not in out
    assert "apikey=REDACTED" in out or "apikey=***" in out


def test_redact_api_key_underscore_variant():
    from scorched.api_tracker import _redact_secrets
    assert "REALKEY" not in _redact_secrets("bad url ?api_key=REALKEY&foo=bar")


def test_redact_token_query_param():
    from scorched.api_tracker import _redact_secrets
    assert "REALTOKEN" not in _redact_secrets(
        "https://finnhub.io/api/v1/stock/recommendation?symbol=AAPL&token=REALTOKEN"
    )


def test_redact_telegram_bot_path():
    from scorched.api_tracker import _redact_secrets
    assert "1234567:REAL_TOKEN" not in _redact_secrets(
        "POST https://api.telegram.org/bot1234567:REAL_TOKEN/sendMessage failed"
    )


def test_redact_is_idempotent():
    from scorched.api_tracker import _redact_secrets
    once = _redact_secrets("?apikey=X")
    twice = _redact_secrets(once)
    assert once == twice


def test_redact_preserves_non_secret_content():
    from scorched.api_tracker import _redact_secrets
    msg = "HTTP 500 Server Error at /api/v1/data — try again later"
    out = _redact_secrets(msg)
    assert out == msg  # no patterns to redact, pass-through intact


def test_redact_handles_none_and_empty():
    from scorched.api_tracker import _redact_secrets
    assert _redact_secrets("") == ""
    assert _redact_secrets(None) is None or _redact_secrets(None) == ""
