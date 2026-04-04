"""
Hold State Manager

Manages persistent state for door hold-open operations.
Stores desired state in a local JSON file with expiry timestamps.
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytz

logger = logging.getLogger(__name__)


class HoldStateManager:
    """
    Manages persistent hold-open state in a local JSON file.

    State format:
    {
        "device-uuid": {
            "state": "hold_today" | "hold_forever",
            "expires_at": <unix_timestamp> | null,
            "created_at": <unix_timestamp>
        }
    }
    """

    def __init__(self, state_file: str = "hold_state.json", timezone: Optional[str] = None):
        """
        Initialize the state manager.

        Args:
            state_file: Path to the state file
            timezone: IANA timezone string (e.g., "America/Los_Angeles")
        """
        self.state_file = Path(state_file)
        self.timezone = timezone or "America/Los_Angeles"
        self._state: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        """Load state from file."""
        if self.state_file.exists():
            try:
                with open(self.state_file, "r") as f:
                    self._state = json.load(f)
                logger.info(f"Loaded hold state: {len(self._state)} entries")
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load state file: {e}. Starting fresh.")
                self._state = {}
        else:
            self._state = {}

    def _save(self) -> None:
        """Save state to file atomically."""
        # Write to temp file first, then rename (atomic on most filesystems)
        temp_file = self.state_file.with_suffix(".tmp")
        try:
            with open(temp_file, "w") as f:
                json.dump(self._state, f, indent=2)
            temp_file.rename(self.state_file)
            logger.debug(f"Saved hold state: {len(self._state)} entries")
        except IOError as e:
            logger.error(f"Failed to save state file: {e}")
            if temp_file.exists():
                temp_file.unlink()

    def _get_6pm_timestamp(self) -> int:
        """Get Unix timestamp for 6 PM today in the configured timezone."""
        return self._get_timestamp_for_time("18:00")

    def _get_timestamp_for_time(self, time_str: str) -> int:
        """
        Convert HH:MM to unix timestamp for today in the configured timezone.

        Args:
            time_str: Time in "HH:MM" format (24-hour)

        Returns:
            Unix timestamp for that time today
        """
        try:
            tz = pytz.timezone(self.timezone)
        except pytz.UnknownTimeZoneError:
            tz = pytz.timezone("America/Los_Angeles")

        now = datetime.now(tz)
        hour, minute = map(int, time_str.split(":"))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return int(target.timestamp())

    def is_past_6pm(self) -> bool:
        """Check if current time is past 6 PM in the configured timezone."""
        try:
            tz = pytz.timezone(self.timezone)
        except pytz.UnknownTimeZoneError:
            tz = pytz.timezone("America/Los_Angeles")

        now = datetime.now(tz)
        return now.hour >= 18

    def set_hold_today(
        self, device_id: str, end_time: str = None, schedule_block: Optional[Dict[str, str]] = None
    ) -> None:
        """
        Set hold-open until a specific time for a device.

        Args:
            device_id: Device unique ID
            end_time: Optional end time in "HH:MM" format (24-hour). Defaults to 6 PM.
            schedule_block: Optional dict with day, start_time, end_time for the UniFi schedule block
        """
        if end_time:
            expires_at = self._get_timestamp_for_time(end_time)
        else:
            expires_at = self._get_6pm_timestamp()

        self._state[device_id] = {
            "state": "hold_today",
            "expires_at": expires_at,
            "created_at": int(time.time()),
            "schedule_block": schedule_block,
        }
        self._save()
        logger.info(f"Set hold_today for {device_id}, expires at {expires_at}")

    def update_hold_expiry(self, device_id: str, expires_at: int) -> None:
        """
        Update the expiry time for an existing hold.

        Args:
            device_id: Device unique ID
            expires_at: New expiry timestamp
        """
        if device_id in self._state:
            self._state[device_id]["expires_at"] = expires_at
            self._save()
            logger.info(f"Updated hold expiry for {device_id} to {expires_at}")

    def set_hold_forever(self, device_id: str, schedule_block: Optional[Dict[str, str]] = None) -> None:
        """
        Set hold-open forever for a device.

        Args:
            device_id: Device unique ID
            schedule_block: Optional dict with day, start_time, end_time for the UniFi schedule block
        """
        self._state[device_id] = {
            "state": "hold_forever",
            "expires_at": None,
            "created_at": int(time.time()),
            "schedule_block": schedule_block,
        }
        self._save()
        logger.info(f"Set hold_forever for {device_id}")

    def remove_hold(self, device_id: str) -> bool:
        """
        Remove hold state for a device.

        Args:
            device_id: Device unique ID

        Returns:
            True if removed, False if didn't exist
        """
        if device_id in self._state:
            del self._state[device_id]
            self._save()
            logger.info(f"Removed hold for {device_id}")
            return True
        return False

    def get_hold(self, device_id: str) -> Optional[Dict[str, Any]]:
        """
        Get hold state for a device.

        Args:
            device_id: Device unique ID

        Returns:
            State dict or None if not held
        """
        return self._state.get(device_id)

    def get_all_holds(self) -> Dict[str, Dict[str, Any]]:
        """Get all hold states."""
        return self._state.copy()

    def get_expired_devices(self) -> List[str]:
        """
        Get list of device IDs with expired holds.

        Returns:
            List of device IDs that have expired
        """
        now = int(time.time())
        expired = []

        for device_id, state in self._state.items():
            expires_at = state.get("expires_at")
            # None means never expires (hold_forever)
            if expires_at is not None and expires_at < now:
                expired.append(device_id)

        return expired

    def cleanup_expired(self) -> List[str]:
        """
        Remove all expired entries from state.

        Returns:
            List of device IDs that were removed
        """
        expired = self.get_expired_devices()

        for device_id in expired:
            del self._state[device_id]
            logger.info(f"Cleaned up expired hold for {device_id}")

        if expired:
            self._save()

        return expired

    def is_held(self, device_id: str) -> bool:
        """
        Check if a device is currently held (and not expired).

        Args:
            device_id: Device unique ID

        Returns:
            True if held and not expired
        """
        state = self.get_hold(device_id)
        if not state:
            return False

        expires_at = state.get("expires_at")
        if expires_at is None:
            return True  # hold_forever

        return expires_at > int(time.time())

    def get_hold_status_text(self, device_id: str) -> Optional[str]:
        """
        Get human-readable hold status for a device.

        Args:
            device_id: Device unique ID

        Returns:
            Status string or None if not held
        """
        state = self.get_hold(device_id)
        if not state:
            return None

        state_type = state.get("state")
        expires_at = state.get("expires_at")

        if state_type == "hold_forever":
            return "Held (Forever)"

        if state_type == "hold_today":
            if expires_at and expires_at > int(time.time()):
                # Format expiry time
                try:
                    tz = pytz.timezone(self.timezone)
                except pytz.UnknownTimeZoneError:
                    tz = pytz.timezone("America/Los_Angeles")

                expiry_dt = datetime.fromtimestamp(expires_at, tz)
                expiry_str = expiry_dt.strftime("%I:%M %p").lstrip("0")
                return f"Held (until {expiry_str})"
            else:
                return None  # Expired

        return None

    def set_timezone(self, timezone: str) -> None:
        """Update the timezone used for expiry calculations."""
        self.timezone = timezone

    def get_6pm_timestamp(self) -> int:
        """Public method to get 6 PM timestamp for migration."""
        return self._get_6pm_timestamp()

    def get_hold_state_data(self, device_id: str) -> Dict[str, Any]:
        """
        Get structured hold state data for a device.

        Args:
            device_id: Device unique ID

        Returns:
            Dict with keys:
            - hold_state: "hold_today" | "hold_forever" | None
            - hold_status: Human-readable string or None
            - is_held: bool
            - expires_at: Unix timestamp or None
        """
        state = self.get_hold(device_id)

        if not state:
            return {
                "hold_state": None,
                "hold_status": None,
                "is_held": False,
                "expires_at": None,
            }

        state_type = state.get("state")
        expires_at = state.get("expires_at")

        # Check for expiration
        if state_type == "hold_today":
            if expires_at and expires_at < int(time.time()):
                # Expired
                return {
                    "hold_state": None,
                    "hold_status": None,
                    "is_held": False,
                    "expires_at": None,
                }

        return {
            "hold_state": state_type,  # "hold_today" or "hold_forever"
            "hold_status": self.get_hold_status_text(device_id),
            "is_held": True,
            "expires_at": expires_at,
        }
