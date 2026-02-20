"""
Tests for ProfitProtector — auto profit transfer to Funding Account.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from src.config import Settings
from src.risk.profit_protector import ProfitProtector


def make_settings(**kwargs) -> Settings:
    defaults = dict(
        TRADING_MODE="dry-run",
        BYBIT_API_KEY="",
        BYBIT_API_SECRET="",
        PROFIT_PROTECT_ENABLED=True,
        PROFIT_PROTECT_THRESHOLD_PCT=0.20,
        PROFIT_PROTECT_TRANSFER_PCT=0.50,
        PROFIT_PROTECT_MIN_CAPITAL_PCT=1.10,
        PROFIT_PROTECT_CHECK_HOURS=0,   # 0 = always check (no time gate for tests)
    )
    defaults.update(kwargs)
    return Settings(**defaults)


def make_protector(settings=None, initial_capital=10_000.0):
    if settings is None:
        settings = make_settings()
    client = MagicMock()
    slack = MagicMock()
    return ProfitProtector(client=client, settings=settings, slack=slack,
                           initial_capital=initial_capital)


# ── Threshold tests ───────────────────────────────────────────────────────────

class TestThreshold:

    def test_no_transfer_below_threshold(self):
        """Equity at 110% of initial (10% profit) should NOT trigger (threshold=20%)."""
        p = make_protector()
        result = p.check_and_protect(current_equity=11_000.0)
        assert result is None

    def test_no_transfer_at_exactly_threshold(self):
        """Exactly at 20% profit — trigger (profit_pct >= threshold)."""
        p = make_protector()
        result = p.check_and_protect(current_equity=12_000.0)
        assert result is not None  # 20% profit → triggers

    def test_transfer_above_threshold(self):
        """50% profit should trigger a transfer."""
        p = make_protector()
        result = p.check_and_protect(current_equity=15_000.0)
        assert result is not None
        assert result > 0

    def test_correct_transfer_amount(self):
        """
        equity=14000, initial=10000
        profit = 4000
        candidate = 4000 × 0.50 = 2000
        min_keep = 10000 × 1.10 = 11000
        after_transfer = 14000 - 2000 = 12000 > 11000 → transfer=2000
        """
        p = make_protector()
        result = p.check_and_protect(current_equity=14_000.0)
        assert result == pytest.approx(2000.0, abs=1.0)

    def test_transfer_capped_by_min_capital(self):
        """
        If transfer would leave account below min_capital, cap it.
        equity=11500, initial=10000, threshold=0.10 (lowered)
        profit=1500, candidate=750
        min_keep=11000, max_transferable=500 → transfer=500
        """
        s = make_settings(
            PROFIT_PROTECT_THRESHOLD_PCT=0.10,
            PROFIT_PROTECT_TRANSFER_PCT=0.50,
            PROFIT_PROTECT_MIN_CAPITAL_PCT=1.10,
        )
        p = make_protector(settings=s)
        result = p.check_and_protect(current_equity=11_500.0)
        assert result is not None
        assert result == pytest.approx(500.0, abs=1.0)


# ── Disabled / no profit ─────────────────────────────────────────────────────

class TestDisabled:

    def test_disabled_never_transfers(self):
        s = make_settings(PROFIT_PROTECT_ENABLED=False)
        p = make_protector(settings=s)
        result = p.check_and_protect(current_equity=20_000.0)
        assert result is None

    def test_no_transfer_below_initial(self):
        """Equity below initial capital (in drawdown) should never transfer."""
        p = make_protector()
        result = p.check_and_protect(current_equity=8_000.0)
        assert result is None

    def test_no_transfer_equal_to_initial(self):
        """Flat (no profit) should not trigger."""
        p = make_protector()
        result = p.check_and_protect(current_equity=10_000.0)
        assert result is None


# ── Accumulation tracking ─────────────────────────────────────────────────────

class TestAccumulation:

    def test_total_transferred_accumulates(self):
        p = make_protector()
        r1 = p.check_and_protect(current_equity=15_000.0)
        assert r1 is not None
        assert p.total_transferred() == pytest.approx(r1, abs=0.01)

    def test_transfer_count_increments(self):
        p = make_protector()
        assert p.transfer_count() == 0
        p.check_and_protect(current_equity=15_000.0)
        assert p.transfer_count() == 1


# ── Rate limiting ─────────────────────────────────────────────────────────────

class TestRateLimit:

    def test_rate_limit_prevents_second_immediate_call(self):
        """With CHECK_HOURS=1, second call immediately after first should return None."""
        s = make_settings(PROFIT_PROTECT_CHECK_HOURS=1)
        p = make_protector(settings=s)
        r1 = p.check_and_protect(current_equity=15_000.0)
        assert r1 is not None   # First call succeeds
        r2 = p.check_and_protect(current_equity=15_000.0)
        assert r2 is None       # Second call blocked by rate limit


# ── Mode behaviour ────────────────────────────────────────────────────────────

class TestModeBehaviour:

    def test_paper_mode_simulates_no_real_api(self):
        """In paper mode, no real transfer should be called."""
        s = make_settings(TRADING_MODE="paper")
        p = make_protector(settings=s)
        result = p.check_and_protect(current_equity=15_000.0)
        assert result is not None
        p._client.transfer_to_funding.assert_not_called()

    def test_dry_run_simulates_no_real_api(self):
        """In dry-run mode, no real transfer should be called."""
        s = make_settings(TRADING_MODE="dry-run")
        p = make_protector(settings=s)
        result = p.check_and_protect(current_equity=15_000.0)
        assert result is not None
        p._client.transfer_to_funding.assert_not_called()

    def test_live_mode_calls_api(self):
        """In live mode, transfer_to_funding should be called."""
        s = make_settings(TRADING_MODE="live", QUALITY_GATE_PASSED=True)
        p = make_protector(settings=s)
        p._client.transfer_to_funding.return_value = {"transferId": "abc"}
        result = p.check_and_protect(current_equity=15_000.0)
        assert result is not None
        p._client.transfer_to_funding.assert_called_once()


# ── Slack notification ────────────────────────────────────────────────────────

class TestSlackNotification:

    def test_slack_called_on_transfer(self):
        p = make_protector()
        p.check_and_protect(current_equity=15_000.0)
        p._slack.send_profit_transfer.assert_called_once()

    def test_slack_not_called_when_no_transfer(self):
        p = make_protector()
        p.check_and_protect(current_equity=10_500.0)   # < threshold
        p._slack.send_profit_transfer.assert_not_called()


# ── Status dict ───────────────────────────────────────────────────────────────

class TestStatusDict:

    def test_status_dict_keys(self):
        p = make_protector()
        s = p.status_dict()
        assert "enabled" in s
        assert "initial_capital" in s
        assert "threshold_pct" in s
        assert "transfer_pct" in s
        assert "total_transferred" in s
        assert "transfer_count" in s
