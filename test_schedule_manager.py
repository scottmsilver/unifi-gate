"""
Tests for schedule_manager.py

Run with: python -m pytest test_schedule_manager.py -v
"""

import json
import os
import tempfile
import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from schedule_manager import ScheduleManager


class MockNativeAPI:
    """Mock UniFi Native API for testing."""

    def __init__(self):
        self.schedules = {}  # device_id -> schedule_info
        self.update_calls = []  # Track calls to update_unlock_schedule

    def get_site_timezone(self):
        return "America/Los_Angeles"

    def get_unlock_schedule(self, device_id):
        if device_id in self.schedules:
            return {"schedule_info": self.schedules[device_id]}
        return {"schedule_info": None}

    def update_unlock_schedule(self, device_id, payload):
        self.update_calls.append({"device_id": device_id, "payload": payload})
        # Store the schedule
        week_schedule = payload.get("create_schedule", {}).get("user_timezone", {}).get("week_schedule", {})
        self.schedules[device_id] = {"user_timezone": {"week_schedule": week_schedule}}
        return True


@pytest.fixture
def temp_state_files():
    """Create temporary state and journal files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = os.path.join(tmpdir, "hold_state.json")
        journal_file = os.path.join(tmpdir, "schedule_journal.log")
        yield state_file, journal_file


@pytest.fixture
def mock_api():
    """Create a mock API instance."""
    return MockNativeAPI()


@pytest.fixture
def schedule_manager(mock_api, temp_state_files):
    """Create a ScheduleManager with mock API and temp files."""
    state_file, journal_file = temp_state_files
    return ScheduleManager(mock_api, state_file=state_file, journal_file=journal_file)


class TestPayloadConstruction:
    """Test that schedule payloads are constructed correctly."""

    def test_inject_hold_open_creates_valid_payload(self, schedule_manager, mock_api):
        """Test inject_hold_open creates a valid schedule payload."""
        device_id = "test-device-123"

        with patch.object(schedule_manager, "get_device_time") as mock_time:
            mock_time.return_value = datetime(2026, 1, 5, 10, 30, 0)  # Monday 10:30 AM
            with patch.object(schedule_manager, "_get_unifi_weekday", return_value="1"):  # Monday
                result = schedule_manager.inject_hold_open(device_id)

        assert result is True
        assert len(mock_api.update_calls) == 1

        payload = mock_api.update_calls[0]["payload"]
        assert payload["unlock_enable"] is True
        assert "create_schedule" in payload

        create_schedule = payload["create_schedule"]
        assert create_schedule["is_private"] is True
        assert create_schedule["schedule_type"] == "access"
        assert create_schedule["type"] == "leave_unlocked"
        assert "user_timezone" in create_schedule
        assert "week_schedule" in create_schedule["user_timezone"]

    def test_inject_hold_open_forever_creates_blocks_for_all_days(self, schedule_manager, mock_api):
        """Test inject_hold_open_forever creates blocks for all 7 days."""
        device_id = "test-device-456"

        result = schedule_manager.inject_hold_open_forever(device_id)

        assert result is True
        assert len(mock_api.update_calls) == 1

        payload = mock_api.update_calls[0]["payload"]
        week_schedule = payload["create_schedule"]["user_timezone"]["week_schedule"]

        # Should have blocks for all 7 days
        for day in ["0", "1", "2", "3", "4", "5", "6"]:
            assert day in week_schedule
            assert len(week_schedule[day]) >= 1
            # Check for full-day block
            has_full_day = any(
                b.get("start_time") == "00:00:00" and b.get("end_time") == "23:59:59" for b in week_schedule[day]
            )
            assert has_full_day, f"Day {day} missing full-day block"

    def test_inject_hold_open_with_custom_end_time(self, schedule_manager, mock_api):
        """Test inject_hold_open respects custom end_time."""
        device_id = "test-device-789"

        with patch.object(schedule_manager, "get_device_time") as mock_time:
            mock_time.return_value = datetime(2026, 1, 5, 14, 0, 0)  # 2 PM
            with patch.object(schedule_manager, "_get_unifi_weekday", return_value="1"):
                result = schedule_manager.inject_hold_open(device_id, end_time="20:00")

        assert result is True

        payload = mock_api.update_calls[0]["payload"]
        week_schedule = payload["create_schedule"]["user_timezone"]["week_schedule"]

        # Check the block has correct end time
        day_blocks = week_schedule.get("1", [])
        assert len(day_blocks) >= 1
        assert any(b.get("end_time") == "20:00:00" for b in day_blocks)


class TestStateManagement:
    """Test state management (hold state tracking)."""

    def test_inject_hold_open_saves_state(self, schedule_manager):
        """Test that inject_hold_open saves state correctly."""
        device_id = "test-device-state"

        with patch.object(schedule_manager, "get_device_time") as mock_time:
            mock_time.return_value = datetime(2026, 1, 5, 10, 0, 0)
            with patch.object(schedule_manager, "_get_unifi_weekday", return_value="1"):
                schedule_manager.inject_hold_open(device_id)

        # Check state was saved
        assert schedule_manager.state_manager.is_held(device_id)
        hold_data = schedule_manager.state_manager.get_hold(device_id)
        assert hold_data is not None
        assert hold_data.get("state") == "hold_today"

    def test_inject_hold_forever_saves_state(self, schedule_manager):
        """Test that inject_hold_open_forever saves state correctly."""
        device_id = "test-device-forever"

        schedule_manager.inject_hold_open_forever(device_id)

        assert schedule_manager.state_manager.is_held(device_id)
        hold_data = schedule_manager.state_manager.get_hold(device_id)
        assert hold_data is not None
        assert hold_data.get("state") == "hold_forever"

    def test_remove_hold_open_clears_state(self, schedule_manager):
        """Test that remove_hold_open clears state."""
        device_id = "test-device-remove"

        # First create a hold
        with patch.object(schedule_manager, "get_device_time") as mock_time:
            mock_time.return_value = datetime(2026, 1, 5, 10, 0, 0)
            with patch.object(schedule_manager, "_get_unifi_weekday", return_value="1"):
                schedule_manager.inject_hold_open(device_id)

        assert schedule_manager.state_manager.is_held(device_id)

        # Now remove it
        result = schedule_manager.remove_hold_open(device_id)

        assert result is True
        assert not schedule_manager.state_manager.is_held(device_id)


class TestJournaling:
    """Test schedule journal tracking."""

    def test_inject_logs_to_journal(self, schedule_manager, temp_state_files):
        """Test that inject_hold_open logs to journal."""
        _, journal_file = temp_state_files
        device_id = "test-journal-inject"

        with patch.object(schedule_manager, "get_device_time") as mock_time:
            mock_time.return_value = datetime(2026, 1, 5, 10, 0, 0)
            with patch.object(schedule_manager, "_get_unifi_weekday", return_value="1"):
                schedule_manager.inject_hold_open(device_id)

        # Check journal file has entry
        with open(journal_file, "r") as f:
            lines = f.readlines()

        assert len(lines) >= 1
        entry = json.loads(lines[-1])
        assert entry["action"] == "create"
        assert entry["device_id"] == device_id
        assert entry["day"] == "1"

    def test_inject_forever_logs_all_days_to_journal(self, schedule_manager, temp_state_files):
        """Test that inject_hold_open_forever logs all 7 days."""
        _, journal_file = temp_state_files
        device_id = "test-journal-forever"

        schedule_manager.inject_hold_open_forever(device_id)

        with open(journal_file, "r") as f:
            lines = f.readlines()

        # Should have 7 entries (one per day)
        assert len(lines) == 7

        days_logged = set()
        for line in lines:
            entry = json.loads(line)
            assert entry["action"] == "create"
            assert entry["device_id"] == device_id
            days_logged.add(entry["day"])

        assert days_logged == {"0", "1", "2", "3", "4", "5", "6"}

    def test_remove_logs_to_journal(self, schedule_manager, temp_state_files):
        """Test that remove_hold_open logs to journal."""
        _, journal_file = temp_state_files
        device_id = "test-journal-remove"

        # Create then remove
        with patch.object(schedule_manager, "get_device_time") as mock_time:
            mock_time.return_value = datetime(2026, 1, 5, 10, 0, 0)
            with patch.object(schedule_manager, "_get_unifi_weekday", return_value="1"):
                schedule_manager.inject_hold_open(device_id)
                schedule_manager.remove_hold_open(device_id)

        with open(journal_file, "r") as f:
            lines = f.readlines()

        # Should have create and remove entries
        assert len(lines) >= 2

        actions = [json.loads(line)["action"] for line in lines]
        assert "create" in actions
        assert "remove" in actions


class TestForceSyncDevice:
    """Test force_sync_device functionality."""

    def test_force_sync_with_no_hold_removes_orphans(self, schedule_manager, mock_api, temp_state_files):
        """Test force_sync removes orphan blocks when no local hold."""
        _, journal_file = temp_state_files
        device_id = "test-force-sync"

        # Simulate an orphan: journal says we created a block, but no local hold state
        with open(journal_file, "a") as f:
            entry = {
                "timestamp": "2026-01-05T10:00:00",
                "action": "create",
                "device_id": device_id,
                "day": "1",
                "start_time": "10:00:00",
                "end_time": "18:00:00",
            }
            f.write(json.dumps(entry) + "\n")

        # Put matching block on mock device
        mock_api.schedules[device_id] = {
            "user_timezone": {"week_schedule": {"1": [{"start_time": "10:00:00", "end_time": "18:00:00"}]}}
        }

        result = schedule_manager.force_sync_device(device_id)

        assert result["action"] == "cleaned"
        assert result["removed"] == 1
        assert result["success"] is True

    def test_force_sync_with_hold_verifies_schedule(self, schedule_manager, mock_api):
        """Test force_sync verifies schedule exists when hold is active."""
        device_id = "test-force-sync-verify"

        # Create a hold first
        schedule_manager.inject_hold_open_forever(device_id)

        # Clear API update calls
        mock_api.update_calls.clear()

        result = schedule_manager.force_sync_device(device_id)

        # Should verify (schedule exists)
        assert result["action"] == "verified"
        assert result["success"] is True


class TestUnifiWeekdayMapping:
    """Test weekday mapping between Python and UniFi."""

    def test_weekday_mapping(self, schedule_manager):
        """Test Python weekday maps correctly to UniFi weekday."""
        # Python: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
        # UniFi:  Sun=0, Mon=1, Tue=2, Wed=3, Thu=4, Fri=5, Sat=6

        test_cases = [
            (0, "1"),  # Python Monday -> UniFi 1
            (1, "2"),  # Python Tuesday -> UniFi 2
            (2, "3"),  # Python Wednesday -> UniFi 3
            (3, "4"),  # Python Thursday -> UniFi 4
            (4, "5"),  # Python Friday -> UniFi 5
            (5, "6"),  # Python Saturday -> UniFi 6
            (6, "0"),  # Python Sunday -> UniFi 0
        ]

        for python_day, expected_unifi in test_cases:
            mock_dt = MagicMock()
            mock_dt.weekday.return_value = python_day

            with patch.object(schedule_manager, "get_device_time", return_value=mock_dt):
                result = schedule_manager._get_unifi_weekday()
                assert (
                    result == expected_unifi
                ), f"Python day {python_day} should map to UniFi {expected_unifi}, got {result}"


class TestSchedulePayloadStructure:
    """Test the structure of schedule payloads."""

    def test_payload_has_required_fields(self, schedule_manager, mock_api):
        """Test payload contains all required fields."""
        device_id = "test-payload-fields"

        with patch.object(schedule_manager, "get_device_time") as mock_time:
            mock_time.return_value = datetime(2026, 1, 5, 10, 0, 0)
            with patch.object(schedule_manager, "_get_unifi_weekday", return_value="1"):
                schedule_manager.inject_hold_open(device_id)

        payload = mock_api.update_calls[0]["payload"]

        # Required top-level fields
        assert "unlock_enable" in payload
        assert "create_schedule" in payload

        # Required create_schedule fields
        cs = payload["create_schedule"]
        assert "is_private" in cs
        assert "name" in cs
        assert "schedule_type" in cs
        assert "type" in cs
        assert "holiday_group_id" in cs
        assert "holiday_timezone" in cs
        assert "user_timezone" in cs

        # Required user_timezone fields
        ut = cs["user_timezone"]
        assert "name" in ut
        assert "week_schedule" in ut

    def test_payload_name_is_unique(self, schedule_manager, mock_api):
        """Test each payload gets a unique name (timestamp-based)."""
        device_id = "test-unique-name"

        with patch.object(schedule_manager, "get_device_time") as mock_time:
            mock_time.return_value = datetime(2026, 1, 5, 10, 0, 0)
            with patch.object(schedule_manager, "_get_unifi_weekday", return_value="1"):
                schedule_manager.inject_hold_open(device_id)
                time.sleep(0.01)  # Small delay to ensure different timestamp
                schedule_manager.inject_hold_open(device_id)

        assert len(mock_api.update_calls) == 2

        name1 = mock_api.update_calls[0]["payload"]["create_schedule"]["name"]
        name2 = mock_api.update_calls[1]["payload"]["create_schedule"]["name"]

        assert name1 != name2, "Payload names should be unique"


class TestSyncStateDoesNotDestroyCustomEndTime:
    """Regression test: sync_state must not destroy hold_today with custom end_time past 6 PM."""

    def test_hold_today_with_late_end_time_survives_sync(self, schedule_manager, mock_api, temp_state_files):
        """A hold_today ending at 22:55 should NOT be removed by sync_state.

        Bug: The legacy migration flagged any hold_today expiring after 6 PM as
        'legacy' and forcibly migrated it to 6 PM, then immediately expired it.
        """
        device_id = "test-device-late-hold"

        # Inject a hold_today with end_time 22:55 (like the user did)
        with patch.object(schedule_manager, "get_device_time") as mock_time:
            mock_time.return_value = datetime(2026, 4, 4, 18, 1, 50)  # 6:01 PM
            with patch.object(schedule_manager, "_get_unifi_weekday", return_value="5"):  # Friday
                result = schedule_manager.inject_hold_open(device_id, end_time="22:55")

        assert result is True
        assert schedule_manager.state_manager.is_held(device_id)

        # Now run sync_state (with time still before 22:55)
        with patch.object(schedule_manager, "get_device_time") as mock_time:
            mock_time.return_value = datetime(2026, 4, 4, 18, 2, 30)  # 6:02 PM
            with patch.object(schedule_manager, "_get_unifi_weekday", return_value="5"):
                sync_results = schedule_manager.sync_state()

        # The hold should still be active — NOT expired or migrated
        assert schedule_manager.state_manager.is_held(
            device_id
        ), "hold_today with end_time 22:55 was destroyed by sync_state"
        assert len(sync_results["migrated"]) == 0, "hold_today with custom end_time should not be flagged as legacy"
        assert len(sync_results["expired"]) == 0, "hold_today with end_time 22:55 should not be expired at 6:02 PM"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
