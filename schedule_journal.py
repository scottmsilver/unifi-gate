"""
Schedule Journal

Append-only log of all schedule blocks we create.
Used to identify orphan blocks that we created but failed to remove.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class ScheduleJournal:
    """
    Append-only journal of schedule blocks we've created.

    Each entry is a JSON line with:
    {
        "timestamp": "ISO datetime",
        "action": "create" | "remove",
        "device_id": "...",
        "day": "0-6",
        "start_time": "HH:MM:SS",
        "end_time": "HH:MM:SS"
    }
    """

    def __init__(self, journal_file: str = "schedule_journal.log"):
        self.journal_file = Path(journal_file)
        # Ensure file exists
        if not self.journal_file.exists():
            self.journal_file.touch()

    def _append(self, entry: dict) -> None:
        """Append an entry to the journal."""
        entry["timestamp"] = datetime.now().isoformat()
        try:
            with open(self.journal_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except IOError as e:
            logger.error(f"Failed to write to journal: {e}")

    def log_create(self, device_id: str, day: str, start_time: str, end_time: str) -> None:
        """Log that we created a schedule block."""
        self._append(
            {
                "action": "create",
                "device_id": device_id,
                "day": day,
                "start_time": start_time,
                "end_time": end_time,
            }
        )
        logger.debug(f"Journal: logged create for {device_id} day={day} {start_time}-{end_time}")

    def log_remove(self, device_id: str, day: str, start_time: str, end_time: str) -> None:
        """Log that we removed a schedule block."""
        self._append(
            {
                "action": "remove",
                "device_id": device_id,
                "day": day,
                "start_time": start_time,
                "end_time": end_time,
            }
        )
        logger.debug(f"Journal: logged remove for {device_id} day={day} {start_time}-{end_time}")

    def get_active_blocks(self, device_id: str) -> List[dict]:
        """
        Get all blocks we created for a device that haven't been removed.

        Returns list of {day, start_time, end_time} dicts.
        """
        # Track creates and removes
        # Key: (day, start_time_prefix, end_time_prefix) -> count of creates - removes
        block_counts = {}

        try:
            with open(self.journal_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("device_id") != device_id:
                            continue

                        day = entry.get("day")
                        start_time = entry.get("start_time", "")
                        end_time = entry.get("end_time", "")

                        # Use prefixes for matching (handles UniFi time normalization)
                        start_prefix = start_time[:5] if len(start_time) >= 5 else start_time
                        end_prefix = end_time[:5] if len(end_time) >= 5 else end_time

                        key = (day, start_prefix, end_prefix)

                        if entry.get("action") == "create":
                            block_counts[key] = block_counts.get(key, 0) + 1
                        elif entry.get("action") == "remove":
                            block_counts[key] = block_counts.get(key, 0) - 1

                    except json.JSONDecodeError:
                        continue

        except IOError as e:
            logger.error(f"Failed to read journal: {e}")
            return []

        # Return blocks with positive count (created more than removed)
        active = []
        for (day, start_prefix, end_prefix), count in block_counts.items():
            if count > 0:
                active.append(
                    {
                        "day": day,
                        "start_prefix": start_prefix,
                        "end_prefix": end_prefix,
                        "count": count,
                    }
                )

        return active

    def is_our_block(self, device_id: str, day: str, start_time: str, end_time: str) -> bool:
        """
        Check if a block matches something we created.
        Uses prefix matching to handle UniFi time normalization.
        """
        active = self.get_active_blocks(device_id)

        start_prefix = start_time[:5] if len(start_time) >= 5 else start_time
        end_prefix = end_time[:5] if len(end_time) >= 5 else end_time

        for block in active:
            if block["day"] == day and block["start_prefix"] == start_prefix and block["end_prefix"] == end_prefix:
                return True

        return False

    def get_our_blocks_for_day(self, device_id: str, day: str) -> List[dict]:
        """Get all active blocks we created for a specific day."""
        active = self.get_active_blocks(device_id)
        return [b for b in active if b["day"] == day]

    def get_entries_for_device(self, device_id: str, limit: int = 20) -> List[dict]:
        """
        Get recent journal entries for a device (for debug display).

        Returns raw entries in reverse chronological order.
        """
        entries = []
        try:
            with open(self.journal_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("device_id") == device_id:
                            entries.append(entry)
                    except json.JSONDecodeError:
                        continue
        except IOError as e:
            logger.error(f"Failed to read journal: {e}")
            return []

        # Return most recent first, limited
        return entries[-limit:][::-1]
