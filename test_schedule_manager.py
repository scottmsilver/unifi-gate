"""Tests for schedule_manager.py — the thin coordinator over UniFiAccess.lock_rule.

The controller owns the hold timer; this module just converts UI inputs
(HH:MM end-times) into API calls and shapes the controller's response for
the UI. No local state, no journal, no periodic sync.

Run: python -m pytest test_schedule_manager.py -v
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from schedule_manager import ScheduleManager
from unifi_access import HoldState, HoldType, UnifiAccessError

TZ = "America/Los_Angeles"


@pytest.fixture
def mock_access():
    client = MagicMock()
    client.hold_until.return_value = 5
    client.hold_indefinitely.return_value = None
    client.release_hold.return_value = None
    client.get_hold_state.return_value = HoldState(type=HoldType.NONE, ended_time=None)
    return client


@pytest.fixture
def sm(mock_access):
    return ScheduleManager(mock_access, timezone=TZ)


# ---- inject_hold_open -----------------------------------------------------


def test_inject_hold_open_calls_hold_until_with_today_epoch(sm, mock_access):
    fixed_now = datetime(2026, 5, 11, 14, 0, tzinfo=sm._tz)  # 2pm local
    with patch.object(sm, "now", return_value=fixed_now):
        assert sm.inject_hold_open("door-1", end_time="18:00") is True
    args, _ = mock_access.hold_until.call_args
    assert args[0] == "door-1"
    expected_epoch = fixed_now.replace(hour=18, minute=0, second=0, microsecond=0).timestamp()
    assert args[1] == expected_epoch


def test_inject_hold_open_default_end_time_is_6pm(sm, mock_access):
    fixed_now = datetime(2026, 5, 11, 14, 0, tzinfo=sm._tz)
    with patch.object(sm, "now", return_value=fixed_now):
        sm.inject_hold_open("door-1")
    args, _ = mock_access.hold_until.call_args
    expected_epoch = fixed_now.replace(hour=18, minute=0, second=0, microsecond=0).timestamp()
    assert args[1] == expected_epoch


def test_inject_hold_open_past_time_rolls_to_tomorrow(sm, mock_access):
    """At 11 PM, picking 1 AM should mean 1 AM tomorrow (next occurrence)."""
    from datetime import timedelta

    fixed_now = datetime(2026, 5, 11, 23, 0, tzinfo=sm._tz)
    with patch.object(sm, "now", return_value=fixed_now):
        assert sm.inject_hold_open("door-1", end_time="01:00") is True
    args, _ = mock_access.hold_until.call_args
    expected_today = fixed_now.replace(hour=1, minute=0, second=0, microsecond=0)
    expected_tomorrow = expected_today + timedelta(days=1)
    assert args[1] == expected_tomorrow.timestamp()


def test_inject_hold_open_same_time_treated_as_tomorrow(sm, mock_access):
    """If now == HH:MM exactly, treat it as tomorrow (else 0-minute hold)."""
    from datetime import timedelta

    fixed_now = datetime(2026, 5, 11, 14, 0, tzinfo=sm._tz)
    with patch.object(sm, "now", return_value=fixed_now):
        sm.inject_hold_open("door-1", end_time="14:00")
    args, _ = mock_access.hold_until.call_args
    expected = (fixed_now + timedelta(days=1)).timestamp()
    assert args[1] == expected


def test_inject_hold_open_invalid_end_time_falls_back_to_6pm(sm, mock_access):
    fixed_now = datetime(2026, 5, 11, 14, 0, tzinfo=sm._tz)
    with patch.object(sm, "now", return_value=fixed_now):
        assert sm.inject_hold_open("door-1", end_time="garbage") is True
    args, _ = mock_access.hold_until.call_args
    expected_epoch = fixed_now.replace(hour=18, minute=0, second=0, microsecond=0).timestamp()
    assert args[1] == expected_epoch


def test_inject_hold_open_returns_false_on_api_error(sm, mock_access):
    mock_access.hold_until.side_effect = UnifiAccessError("boom")
    assert sm.inject_hold_open("door-1") is False


# ---- inject_hold_open_forever ---------------------------------------------


def test_inject_hold_open_forever_calls_hold_indefinitely(sm, mock_access):
    assert sm.inject_hold_open_forever("door-1") is True
    mock_access.hold_indefinitely.assert_called_once_with("door-1")


def test_inject_hold_open_forever_returns_false_on_api_error(sm, mock_access):
    mock_access.hold_indefinitely.side_effect = UnifiAccessError("boom")
    assert sm.inject_hold_open_forever("door-1") is False


# ---- remove_hold_open -----------------------------------------------------


def test_remove_hold_open_calls_release_hold(sm, mock_access):
    assert sm.remove_hold_open("door-1") is True
    mock_access.release_hold.assert_called_once_with("door-1")


def test_remove_hold_open_returns_false_on_api_error(sm, mock_access):
    mock_access.release_hold.side_effect = UnifiAccessError("boom")
    assert sm.remove_hold_open("door-1") is False


# ---- get_hold_state_data --------------------------------------------------


def test_get_hold_state_data_no_hold(sm, mock_access):
    mock_access.get_hold_state.return_value = HoldState(type=HoldType.NONE, ended_time=None)
    assert sm.get_hold_state_data("door-1") == {
        "hold_state": None,
        "hold_status": None,
        "is_held": False,
        "expires_at": None,
    }


def test_get_hold_state_data_forever(sm, mock_access):
    mock_access.get_hold_state.return_value = HoldState(type=HoldType.KEEP_UNLOCK, ended_time=None)
    result = sm.get_hold_state_data("door-1")
    assert result["hold_state"] == "hold_forever"
    assert result["is_held"] is True
    assert result["expires_at"] is None


def test_get_hold_state_data_timed(sm, mock_access):
    ended = int(sm._tz.localize(datetime(2026, 5, 11, 18, 0)).timestamp())
    mock_access.get_hold_state.return_value = HoldState(type=HoldType.CUSTOM, ended_time=ended)
    result = sm.get_hold_state_data("door-1")
    assert result["hold_state"] == "hold_today"
    assert result["is_held"] is True
    assert result["expires_at"] == ended
    assert "6:00 PM" in result["hold_status"]


def test_get_hold_state_data_lock_returns_not_held(sm, mock_access):
    mock_access.get_hold_state.return_value = HoldState(type=HoldType.KEEP_LOCK, ended_time=None)
    assert sm.get_hold_state_data("door-1")["is_held"] is False


def test_get_hold_state_data_api_error_returns_empty(sm, mock_access):
    mock_access.get_hold_state.side_effect = UnifiAccessError("network down")
    assert sm.get_hold_state_data("door-1") == {
        "hold_state": None,
        "hold_status": None,
        "is_held": False,
        "expires_at": None,
    }


# ---- is_past_6pm ----------------------------------------------------------


def test_is_past_6pm_false_at_noon(sm):
    with patch.object(sm, "now", return_value=datetime(2026, 5, 11, 12, 0, tzinfo=sm._tz)):
        assert sm.is_past_6pm() is False


def test_is_past_6pm_true_at_8pm(sm):
    with patch.object(sm, "now", return_value=datetime(2026, 5, 11, 20, 0, tzinfo=sm._tz)):
        assert sm.is_past_6pm() is True


def test_is_past_6pm_true_at_exactly_6pm(sm):
    with patch.object(sm, "now", return_value=datetime(2026, 5, 11, 18, 0, tzinfo=sm._tz)):
        assert sm.is_past_6pm() is True
