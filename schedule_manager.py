"""Thin coordinator for hold-open operations.

The UniFi Access controller owns the hold timer (via the `lock_rule` endpoint)
and is the source of truth for whether a door is held and when the hold ends.
This module:
  - converts UI HH:MM end-times into epoch seconds,
  - delegates writes to the UniFiAccess client,
  - shapes the controller's lock_rule response into the UI-facing dict.

No local state, no journal, no periodic sync. The controller is authoritative.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

import pytz
from unifi_access import HoldType, UniFiAccess, UnifiAccessError

logger = logging.getLogger("schedule_manager")

EMPTY_HOLD_STATE: dict[str, Any] = {
    "hold_state": None,
    "hold_status": None,
    "is_held": False,
    "expires_at": None,
}


class ScheduleManager:
    def __init__(self, access: UniFiAccess, timezone: str):
        self.access = access
        self._tz = pytz.timezone(timezone)

    def now(self) -> datetime:
        return datetime.now(self._tz)

    def is_past_6pm(self) -> bool:
        return self.now().hour >= 18

    def _end_time_to_epoch(self, end_time: Optional[str]) -> float:
        """HH:MM (local TZ) → epoch for the *next* occurrence of that wall-clock
        time. If HH:MM already passed today, it's interpreted as tomorrow — so a
        user can hold until 1 AM at 9 PM and it means 1 AM tomorrow.
        Defaults to 18:00."""
        try:
            hh, mm = (end_time or "18:00").split(":")[:2]
            hh, mm = int(hh), int(mm)
        except (ValueError, IndexError):
            logger.warning("Invalid end_time %r; falling back to 18:00", end_time)
            hh, mm = 18, 0
        now = self.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.timestamp()

    def _call(self, op_name: str, door_id: str, fn, *args) -> bool:
        """Run an Access write; on UnifiAccessError log + return False, else True."""
        try:
            fn(*args)
        except UnifiAccessError as e:
            logger.error("%s failed for %s: %s", op_name, door_id, e)
            return False
        logger.info("%s ok for %s", op_name, door_id)
        return True

    def inject_hold_open(self, door_id: str, end_time: Optional[str] = None) -> bool:
        """Hold door until HH:MM today (default 18:00). Controller auto-expires."""
        return self._call("hold_until", door_id, self.access.hold_until, door_id, self._end_time_to_epoch(end_time))

    def inject_hold_open_forever(self, door_id: str) -> bool:
        return self._call("hold_indefinitely", door_id, self.access.hold_indefinitely, door_id)

    def remove_hold_open(self, door_id: str) -> bool:
        return self._call("release_hold", door_id, self.access.release_hold, door_id)

    def get_hold_state_data(self, door_id: str) -> dict[str, Any]:
        """Read controller's lock_rule and shape it for the UI.

        Returns:
            hold_state:  "hold_today" | "hold_forever" | None
            hold_status: human-readable string | None
            is_held:     bool
            expires_at:  epoch seconds | None  (None for forever)
        """
        try:
            state = self.access.get_hold_state(door_id)
        except UnifiAccessError as e:
            logger.warning("get_hold_state failed for %s: %s", door_id, e)
            return dict(EMPTY_HOLD_STATE)

        if state.type == HoldType.KEEP_UNLOCK:
            return {
                "hold_state": "hold_forever",
                "hold_status": "Held indefinitely",
                "is_held": True,
                "expires_at": None,
            }
        if state.type == HoldType.CUSTOM and state.ended_time:
            expiry_dt = datetime.fromtimestamp(state.ended_time, self._tz)
            return {
                "hold_state": "hold_today",
                "hold_status": f"Held until {expiry_dt.strftime('%-I:%M %p')}",
                "is_held": True,
                "expires_at": int(state.ended_time),
            }
        return dict(EMPTY_HOLD_STATE)
