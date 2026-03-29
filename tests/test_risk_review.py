"""Tests for risk committee adversarial review."""
import pytest
import json
from scorched.services.risk_review import (
    build_risk_review_prompt,
    parse_risk_review_response,
    RISK_REVIEW_SYSTEM,
)


class TestBuildRiskReviewPrompt:
    def test_includes_recommendations(self):
        recs = [
            {"symbol": "AAPL", "action": "buy", "quantity": 10, "reasoning": "Strong momentum"},
        ]
        portfolio = {"cash_balance": 50000, "positions": []}
        prompt = build_risk_review_prompt(recs, portfolio, "Market looks good", "")
        assert "AAPL" in prompt
        assert "buy" in prompt.lower()

    def test_includes_portfolio_context(self):
        recs = []
        portfolio = {
            "cash_balance": 50000,
            "positions": [
                {"symbol": "NVDA", "shares": 50, "days_held": 5, "unrealized_gain": 500}
            ],
        }
        prompt = build_risk_review_prompt(recs, portfolio, "", "")
        assert "NVDA" in prompt

    def test_system_prompt_exists(self):
        assert "risk" in RISK_REVIEW_SYSTEM.lower()
        assert len(RISK_REVIEW_SYSTEM) > 100


class TestParseRiskReviewResponse:
    def test_parses_approved_trades(self):
        response = json.dumps({
            "decisions": [
                {"symbol": "AAPL", "action": "buy", "verdict": "approve", "reason": "Solid setup"},
                {"symbol": "TSLA", "action": "buy", "verdict": "reject", "reason": "Too risky"},
            ]
        })
        result = parse_risk_review_response(response)
        assert len(result) == 2
        assert result[0]["verdict"] == "approve"
        assert result[1]["verdict"] == "reject"

    def test_handles_malformed_json(self):
        result = parse_risk_review_response("not json at all")
        assert result == []

    def test_handles_empty_decisions(self):
        response = json.dumps({"decisions": []})
        result = parse_risk_review_response(response)
        assert result == []
