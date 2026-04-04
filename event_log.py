"""
Event Log

Append-only log of user actions and system events.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


class EventLog:
    """
    Append-only event log for tracking user actions and system events.

    Each entry is a JSON line with:
    {
        "timestamp": "ISO datetime",
        "event": "unlock" | "hold_today" | "hold_forever" | "stop_hold" | "sync" | "login" | ...,
        "device_id": "..." (optional),
        "device_name": "..." (optional),
        "user": "email or system",
        "details": "..." (optional)
    }
    """

    def __init__(self, log_file: str = "event_log.jsonl"):
        self.log_file = Path(log_file)
        if not self.log_file.exists():
            self.log_file.touch()

    def _append(self, entry: dict) -> None:
        """Append an entry to the log."""
        entry["timestamp"] = datetime.now().isoformat()
        try:
            with open(self.log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except IOError as e:
            logger.error(f"Failed to write to event log: {e}")

    def log_action(
        self,
        event: str,
        user: str,
        device_id: Optional[str] = None,
        device_name: Optional[str] = None,
        details: Optional[str] = None,
    ) -> None:
        """Log a user action or system event."""
        entry = {"event": event, "user": user}
        if device_id:
            entry["device_id"] = device_id
        if device_name:
            entry["device_name"] = device_name
        if details:
            entry["details"] = details
        self._append(entry)

    def log_unlock(self, user: str, device_id: str, device_name: Optional[str] = None) -> None:
        """Log an unlock (open once) action."""
        self.log_action("unlock", user, device_id, device_name)

    def log_hold_today(
        self, user: str, device_id: str, device_name: Optional[str] = None, end_time: Optional[str] = None
    ) -> None:
        """Log a hold today action."""
        details = f"until {end_time}" if end_time else None
        self.log_action("hold_today", user, device_id, device_name, details)

    def log_hold_forever(self, user: str, device_id: str, device_name: Optional[str] = None) -> None:
        """Log a hold forever action."""
        self.log_action("hold_forever", user, device_id, device_name)

    def log_stop_hold(self, user: str, device_id: str, device_name: Optional[str] = None) -> None:
        """Log a stop hold (close) action."""
        self.log_action("stop_hold", user, device_id, device_name)

    def log_login(self, user: str, success: bool = True) -> None:
        """Log a login event."""
        event = "login_success" if success else "login_failed"
        self.log_action(event, user)

    def log_sync(self, details: str) -> None:
        """Log a sync event."""
        self.log_action("sync", "system", details=details)

    def log_orphan_cleanup(self, device_id: str, blocks_removed: int) -> None:
        """Log orphan cleanup."""
        self.log_action("orphan_cleanup", "system", device_id=device_id, details=f"removed {blocks_removed} blocks")

    def log_ws_event(
        self, event_type: str, device_id: str, device_name: Optional[str] = None, details: Optional[str] = None
    ) -> None:
        """Log a WebSocket event (real-time from UniFi)."""
        self.log_action(event_type, "unifi", device_id=device_id, device_name=device_name, details=details)

    def log_admin_action(self, admin_user: str, action: str, target: str) -> None:
        """Log an admin action (user management)."""
        self.log_action(f"admin_{action}", admin_user, details=target)

    def get_recent(self, limit: int = 50) -> List[dict]:
        """
        Get the most recent log entries.

        Args:
            limit: Maximum number of entries to return

        Returns:
            List of log entries, most recent first
        """
        entries = []
        try:
            with open(self.log_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except IOError as e:
            logger.error(f"Failed to read event log: {e}")
            return []

        # Return most recent first
        return list(reversed(entries[-limit:]))
