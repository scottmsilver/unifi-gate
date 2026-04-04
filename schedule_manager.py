import json
import logging
import time as time_module
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import pytz

from hold_state_manager import HoldStateManager
from schedule_journal import ScheduleJournal

# Configure specific logger for schedule injection
logger = logging.getLogger("schedule_manager")
logger.setLevel(logging.DEBUG)
handler = logging.FileHandler("schedule_injection.log")
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)

# Constants
ALL_DAYS = ["0", "1", "2", "3", "4", "5", "6"]


class ScheduleManager:
    """
    Manages "Schedule Injection" logic for holding doors open.
    Uses HoldStateManager for persistent state with expiry tracking.
    """

    def __init__(self, native_api, state_file: str = "hold_state.json", journal_file: str = "schedule_journal.log"):
        self.api = native_api
        self.site_timezone = None  # Store fetched timezone
        self.state_manager = HoldStateManager(state_file)
        self.journal = ScheduleJournal(journal_file)

    def _log(self, message: str):
        """Log to both file and console (if needed)."""
        logger.info(message)

    def _get_existing_schedule(self, device_id: str) -> Tuple[Optional[Dict], Dict]:
        """
        Fetch existing schedule from UniFi and extract week_schedule.

        Returns:
            Tuple of (existing_info, week_schedule) where:
            - existing_info: The raw schedule_info from API, or None
            - week_schedule: The week_schedule dict, or empty dict with all days
        """
        get_response = self.api.get_unlock_schedule(device_id)

        if get_response is None:
            return None, {day: [] for day in ALL_DAYS}

        existing_info = None
        if isinstance(get_response, dict) and "schedule_info" in get_response:
            existing_info = get_response["schedule_info"]

        week_schedule = {day: [] for day in ALL_DAYS}
        if existing_info:
            utz = existing_info.get("user_timezone", {})
            if utz and "week_schedule" in utz:
                week_schedule = utz["week_schedule"]

        return existing_info, week_schedule

    def _build_schedule_payload(self, week_schedule: Dict, name_prefix: str = "unlock") -> Dict:
        """
        Build a standard schedule payload for UniFi API.

        Args:
            week_schedule: The week_schedule dict with day keys "0"-"6"
            name_prefix: Prefix for the schedule name (e.g., "unlock", "unlock-forever")

        Returns:
            Complete payload dict for update_unlock_schedule API call
        """
        timestamp = int(time_module.time() * 1000)

        return {
            "unlock_enable": True,
            "create_schedule": {
                "is_private": True,
                "name": f"{name_prefix}-{timestamp}",
                "schedule_type": "access",
                "type": "leave_unlocked",
                "holiday_group_id": "",
                "holiday_timezone": {"day_schedule": [], "name": f"holiday {timestamp}"},
                "user_timezone": {"name": f"user {timestamp}", "week_schedule": week_schedule},
            },
        }

    def get_device_time(self) -> datetime:
        """
        Get the current time in the device's timezone.
        Fetches timezone from API, falls back to UTC-8.
        """
        if not self.site_timezone:
            # Fetch once and store
            self.site_timezone = self.api.get_site_timezone()
            # Update state manager timezone too
            if self.site_timezone:
                self.state_manager.set_timezone(self.site_timezone)

        if self.site_timezone:
            try:
                tz = pytz.timezone(self.site_timezone)
                return datetime.now(tz)
            except pytz.UnknownTimeZoneError:
                self._log(f"Warning: Unknown timezone '{self.site_timezone}'. Falling back to UTC-8.")

        # Fallback to hardcoded UTC-8 (PST) if timezone not found or invalid
        pst = pytz.timezone("America/Los_Angeles")
        return datetime.now(pst)

    def _get_unifi_weekday(self) -> str:
        """
        Map Python weekday to UniFi format.
        Python: 0=Mon, 1=Tue, ... 6=Sun
        UniFi: 0=Sun, 1=Mon, ... 6=Sat
        """
        python_day = self.get_device_time().weekday()
        # Map: 0->1, 1->2, ... 5->6, 6->0
        if python_day == 6:
            return "0"
        else:
            return str(python_day + 1)

    def inject_hold_open(self, device_id: str, end_time: str = None) -> bool:
        """
        Inject a 'hold open' schedule for the rest of the current day.

        Args:
            device_id: Device unique ID
            end_time: Optional end time in "HH:MM" format (24-hour). Defaults to "18:00".
        """
        self._log(f"Request to inject hold open for device {device_id} (end_time={end_time})")

        # 1. Get current schedule
        existing_info, week_schedule = self._get_existing_schedule(device_id)
        if existing_info is None and not week_schedule:
            self._log("Failed to fetch existing schedule. Aborting inject to prevent overwrite.")
            return False

        if existing_info:
            self._log("Found existing schedule info. extracting week_schedule.")

        # 2. Calculate time block
        now = self.get_device_time()
        unifi_day = self._get_unifi_weekday()

        start_time_str = now.strftime("%H:%M:%S")
        # Use provided end_time or default to 6 PM
        if end_time:
            end_time_str = f"{end_time}:00" if len(end_time) == 5 else end_time
        else:
            end_time_str = "18:00:00"

        new_block = {"start_time": start_time_str, "end_time": end_time_str}

        # 3. Inject
        if unifi_day not in week_schedule:
            week_schedule[unifi_day] = []

        self._log(f"Injecting block into day {unifi_day}: {json.dumps(new_block)}")
        week_schedule[unifi_day].append(new_block)

        # 4. Build payload and push
        put_payload = self._build_schedule_payload(week_schedule)
        self._log("Pushing updated schedule...")
        success = self.api.update_unlock_schedule(device_id, put_payload)

        if success:
            self._log("Successfully injected hold open schedule.")
            # Save state with expiry time and schedule block details
            schedule_block = {
                "day": unifi_day,
                "start_time": start_time_str,
                "end_time": end_time_str,
            }
            self.state_manager.set_hold_today(device_id, end_time=end_time, schedule_block=schedule_block)
            # Journal the block we created
            self.journal.log_create(device_id, unifi_day, start_time_str, end_time_str)
        else:
            self._log("Failed to push updated schedule.")

        return success

    def inject_hold_open_forever(self, device_id: str) -> bool:
        """
        Inject a 'hold open' schedule for ALL days (24/7).
        """
        self._log(f"Request to inject hold open FOREVER for device {device_id}")

        # 1. Get current schedule
        existing_info, week_schedule = self._get_existing_schedule(device_id)
        if existing_info is None and not week_schedule:
            self._log("Failed to fetch existing schedule. Aborting inject to prevent overwrite.")
            return False

        if existing_info:
            self._log("Found existing schedule info. extracting week_schedule.")

        # 2. Inject full-day block into ALL days
        new_block = {"start_time": "00:00:00", "end_time": "23:59:59"}
        for day_key in ALL_DAYS:
            if day_key not in week_schedule:
                week_schedule[day_key] = []
            self._log(f"Injecting full day block into day {day_key}")
            week_schedule[day_key].append(new_block)

        # 3. Build payload and push
        put_payload = self._build_schedule_payload(week_schedule, name_prefix="unlock-forever")
        self._log("Pushing updated schedule (forever)...")
        success = self.api.update_unlock_schedule(device_id, put_payload)

        if success:
            self._log("Successfully injected hold open forever schedule.")
            # Save state with no expiry (forever) and schedule block details
            # For forever, we add blocks to all days, so store a representative block
            schedule_block = {
                "day": "all",  # Special marker for forever holds
                "start_time": "00:00:00",
                "end_time": "23:59:59",
            }
            self.state_manager.set_hold_forever(device_id, schedule_block=schedule_block)
            # Journal all 7 blocks we created
            for day_key in ["0", "1", "2", "3", "4", "5", "6"]:
                self.journal.log_create(device_id, day_key, "00:00:00", "23:59:59")
        else:
            self._log("Failed to push updated schedule.")

        return success

    def remove_hold_open(self, device_id: str) -> bool:
        """
        Remove any injected 'hold open' schedules from ALL days.
        This handles both 'Hold for Today' and 'Hold Forever'.
        """
        self._log(f"Request to remove hold open for device {device_id}")

        # 1. Get current schedule
        existing_info, week_schedule = self._get_existing_schedule(device_id)

        if not existing_info:
            self._log("No schedule to remove from.")
            return True

        # 2. Get hold info from local state to know what to remove
        hold_data = self.state_manager.get_hold(device_id)
        schedule_block = hold_data.get("schedule_block") if hold_data else None

        if not schedule_block:
            self._log("No local state schedule_block found. Cannot safely identify which schedule to remove. Aborting.")
            # We explicitly do NOT return True here because we failed to remove what we were asked to.
            # However, if the goal is "stop holding", and we don't know what we held, maybe we just clear state?
            # Safe approach: Just clear state, but touch nothing in UniFi.
            self.state_manager.remove_hold(device_id)
            return True

        target_day = schedule_block.get("day")
        target_start_time = schedule_block.get("start_time")
        target_end_time = schedule_block.get("end_time")

        self._log(f"Strict removal target: day={target_day}, start={target_start_time}, end={target_end_time}")

        total_removed = 0

        # Determine which days to check
        if target_day == "all":
            # Forever hold - remove from all days
            days_to_check = ALL_DAYS
        elif target_day:
            # Specific day hold
            days_to_check = [target_day]
        else:
            # Should not happen with valid schedule_block, but safety net
            days_to_check = []

        for day_key in days_to_check:
            if day_key in week_schedule:
                original_count = len(week_schedule[day_key])

                # strict matching: must match both start and end time
                new_blocks = []
                for block in week_schedule[day_key]:
                    # Check if this block matches our target
                    # We match loosely on seconds if needed, but exact string match is best if we stored it exactly
                    is_match = block.get("start_time") == target_start_time and block.get("end_time") == target_end_time

                    if not is_match:
                        new_blocks.append(block)

                week_schedule[day_key] = new_blocks

                removed = original_count - len(week_schedule[day_key])
                if removed > 0:
                    total_removed += removed
                    self._log(f"Removed {removed} blocks from day {day_key}")

        # Fallback: if strict match failed, try matching by end_time prefix
        # This handles cases where start_time drifted or UniFi normalized the time
        if total_removed == 0 and target_end_time:
            # Extract prefix (e.g., "18:00" from "18:00:00")
            target_prefix = target_end_time[:5] if len(target_end_time) >= 5 else target_end_time
            self._log(f"Strict match failed. Trying fallback: match by end_time prefix={target_prefix}")
            for day_key in days_to_check:
                if day_key in week_schedule:
                    original_count = len(week_schedule[day_key])

                    new_blocks = []
                    for block in week_schedule[day_key]:
                        # Match by end_time prefix (handles UniFi normalization like 18:00:00 -> 18:00:59)
                        block_end = block.get("end_time", "")
                        if not block_end.startswith(target_prefix):
                            new_blocks.append(block)

                    week_schedule[day_key] = new_blocks

                    removed = original_count - len(week_schedule[day_key])
                    if removed > 0:
                        total_removed += removed
                        self._log(f"Fallback: Removed {removed} blocks from day {day_key}")

        if total_removed == 0:
            self._log("No matching hold-open blocks found to remove (strict or fallback).")
            # Still remove from state in case it's out of sync
            self.state_manager.remove_hold(device_id)
            return True

        self._log(f"Removed total {total_removed} blocks across all days.")

        # 3. Build payload and push
        put_payload = self._build_schedule_payload(week_schedule)
        self._log("Pushing updated schedule...")
        success = self.api.update_unlock_schedule(device_id, put_payload)

        if success:
            self._log("Successfully removed hold open schedule.")
            # Remove from state
            self.state_manager.remove_hold(device_id)
            # Journal the removal
            if target_day == "all":
                for day_key in ALL_DAYS:
                    self.journal.log_remove(device_id, day_key, target_start_time or "", target_end_time or "")
            elif target_day:
                self.journal.log_remove(device_id, target_day, target_start_time or "", target_end_time or "")
        else:
            self._log("Failed to push updated schedule.")

        return success

    def get_hold_status_text(self, device_id: str) -> Optional[str]:
        """
        Get a human-readable status string if a hold open schedule is active.
        Uses local state manager as source of truth.
        """
        # Use state manager for accurate status with expiry time
        return self.state_manager.get_hold_status_text(device_id)

    def get_hold_state_data(self, device_id: str) -> Dict[str, Any]:
        """
        Get structured hold state data for a device.
        Uses local state manager as source of truth.

        Returns:
            Dict with keys:
            - hold_state: "hold_today" | "hold_forever" | None
            - hold_status: Human-readable string or None
            - is_held: bool
            - expires_at: Unix timestamp or None
        """
        return self.state_manager.get_hold_state_data(device_id)

    def is_past_6pm(self) -> bool:
        """Check if current time is past 6 PM in the site timezone."""
        # Ensure timezone is loaded
        self.get_device_time()
        return self.state_manager.is_past_6pm()

    def is_hold_open_active(self, device_id: str) -> bool:
        """
        Check if a hold open schedule is currently active for the device.

        Args:
            device_id: Device unique ID

        Returns:
            True if active
        """
        try:
            get_response = self.api.get_unlock_schedule(device_id)

            if not get_response or "schedule_info" not in get_response:
                return False

            schedule_info = get_response["schedule_info"]
            if not schedule_info:
                return False

            user_timezone = schedule_info.get("user_timezone")
            if not user_timezone:
                return False

            week_schedule = user_timezone.get("week_schedule")
            if not week_schedule:
                return False

            unifi_day = self._get_unifi_weekday()

            if unifi_day not in week_schedule:
                return False

            today_blocks = week_schedule[unifi_day]
            if not today_blocks:
                return False

            now_str = self.get_device_time().strftime("%H:%M:%S")

            for block in today_blocks:
                # Check for our injected block signature (18:00:00 new, 23:59:59 legacy)
                end_time = block.get("end_time")
                if end_time in ("18:00:00", "23:59:59"):
                    start = block.get("start_time")

                    # Simple string comparison works for HH:MM:SS
                    if start and end_time and start <= now_str <= end_time:
                        return True

            return False

        except Exception as e:
            self._log(f"Error checking hold open status: {e}")
            return False

    def sync_state(self) -> Dict[str, Any]:
        """
        Synchronize local state with UniFi schedules.

        1. Remove expired entries from state and clean their schedules from UniFi
        2. For each active entry, verify schedule exists in UniFi (re-inject if missing)

        Returns:
            Dict with sync results: {expired: [...], reinjected: [...], migrated: [...], errors: [...]}
        """
        self._log("Starting state sync...")
        results = {"expired": [], "reinjected": [], "migrated": [], "errors": []}

        # 1. Handle expired entries
        expired_devices = self.state_manager.get_expired_devices()
        for device_id in expired_devices:
            self._log(f"Found expired hold for {device_id}, cleaning up...")
            try:
                # Remove from UniFi
                self._cleanup_our_schedules(device_id)
                # Remove from state (cleanup_expired will handle this)
                results["expired"].append(device_id)
            except Exception as e:
                self._log(f"Error cleaning expired hold for {device_id}: {e}")
                results["errors"].append({"device_id": device_id, "error": str(e)})

        # Clean expired from state file
        self.state_manager.cleanup_expired()

        # 2. Verify active entries still have schedules
        active_holds = self.state_manager.get_all_holds()
        for device_id, state in active_holds.items():
            if device_id in expired_devices:
                continue  # Already handled

            state_type = state.get("state")
            self._log(f"Verifying hold for {device_id} (type: {state_type})")

            try:
                # Check if schedule exists in UniFi
                if not self.is_hold_open_active(device_id):
                    self._log(f"Schedule missing for {device_id}, re-injecting...")

                    # Re-inject based on state type
                    if state_type == "hold_forever":
                        # Don't use inject_hold_open_forever as it would update state
                        self._inject_schedule_only(device_id, forever=True)
                    else:
                        self._inject_schedule_only(device_id, forever=False)

                    results["reinjected"].append(device_id)

            except Exception as e:
                self._log(f"Error verifying hold for {device_id}: {e}")
                results["errors"].append({"device_id": device_id, "error": str(e)})

        # 3. Orphan cleanup removed for safety.
        # We no longer scan for "magic number" schedules to delete, as this risks deleting user schedules.
        # If state is lost, the schedule remains until manually removed.
        # results["orphans_cleaned"] = []

        self._log(
            f"State sync complete: {len(results['migrated'])} migrated, "
            f"{len(results['expired'])} expired, {len(results['reinjected'])} reinjected, "
            f"{len(results.get('orphans_cleaned', []))} orphans cleaned, "
            f"{len(results['errors'])} errors"
        )

        return results

    def _cleanup_our_schedules(self, device_id: str) -> bool:
        """
        Remove our unlock-* schedules from a device without touching other schedules.
        """
        # For now, use existing remove_hold_open which clears all 23:59:59 and 18:00:00 blocks
        # In the future, could be smarter about only removing our named schedules
        return self.remove_hold_open(device_id)

    def _inject_schedule_only(self, device_id: str, forever: bool = False) -> bool:
        """
        Inject a schedule without updating state (used during sync).
        """
        self._log(f"Re-injecting schedule for {device_id} (forever={forever})")

        # Get current schedule
        existing_info, week_schedule = self._get_existing_schedule(device_id)

        if existing_info is None and not week_schedule:
            self._log("Failed to fetch existing schedule")
            return False

        if forever:
            # Add full day block to all days
            new_block = {"start_time": "00:00:00", "end_time": "23:59:59"}
            for day_key in ALL_DAYS:
                if day_key not in week_schedule:
                    week_schedule[day_key] = []
                week_schedule[day_key].append(new_block)
            name_prefix = "unlock-forever"
        else:
            # Add block for rest of today (until 6 PM)
            now = self.get_device_time()
            unifi_day = self._get_unifi_weekday()
            start_time_str = now.strftime("%H:%M:%S")
            new_block = {"start_time": start_time_str, "end_time": "18:00:00"}

            if unifi_day not in week_schedule:
                week_schedule[unifi_day] = []
            week_schedule[unifi_day].append(new_block)
            name_prefix = "unlock"

        put_payload = self._build_schedule_payload(week_schedule, name_prefix=name_prefix)
        success = self.api.update_unlock_schedule(device_id, put_payload)

        if success:
            self._log(f"Successfully re-injected schedule for {device_id}")
            # Journal the re-injected blocks
            if forever:
                for day_key in ALL_DAYS:
                    self.journal.log_create(device_id, day_key, "00:00:00", "23:59:59")
            else:
                self.journal.log_create(device_id, unifi_day, start_time_str, "18:00:00")
        else:
            self._log(f"Failed to re-inject schedule for {device_id}")

        return success

    def force_sync_device(self, device_id: str) -> Dict[str, Any]:
        """
        Force sync a device's schedule to match local state.

        If local state says "no hold", removes any orphan schedules from UniFi.
        If local state says "hold", ensures the schedule exists in UniFi.

        Returns:
            Dict with sync results: {action: str, removed: int, success: bool, error: str|None}
        """
        self._log(f"Force sync requested for device {device_id}")
        result = {"action": "none", "removed": 0, "success": True, "error": None}

        # Check local state
        hold_data = self.state_manager.get_hold(device_id)
        is_held_locally = self.state_manager.is_held(device_id)

        if is_held_locally:
            # Local state says hold - ensure schedule exists
            self._log(f"Local state says hold for {device_id}, verifying schedule...")
            if not self.is_hold_open_active(device_id):
                self._log("Schedule missing, re-injecting...")
                state_type = hold_data.get("state") if hold_data else "hold_today"
                success = self._inject_schedule_only(device_id, forever=(state_type == "hold_forever"))
                result["action"] = "reinjected"
                result["success"] = success
                if not success:
                    result["error"] = "Failed to re-inject schedule"
            else:
                result["action"] = "verified"
        else:
            # Local state says no hold - remove any orphan schedules
            self._log(f"Local state says no hold for {device_id}, cleaning orphans...")
            result["action"] = "cleaned"

            # Get current schedule from UniFi
            existing_info, week_schedule = self._get_existing_schedule(device_id)

            if not existing_info:
                self._log("No schedule found on device")
                return result

            # Log what we see on the device
            self._log(f"Current schedule on device: {json.dumps(week_schedule)}")

            total_removed = 0
            removed_blocks = []  # Track what we removed for journaling

            # Remove blocks that we created (according to journal) but are now orphaned
            for day_key in ALL_DAYS:
                if day_key in week_schedule:
                    original_count = len(week_schedule[day_key])

                    new_blocks = []
                    for block in week_schedule[day_key]:
                        start_time = block.get("start_time", "")
                        end_time = block.get("end_time", "")
                        # Check journal to see if this is our block
                        is_our_block = self.journal.is_our_block(device_id, day_key, start_time, end_time)
                        if is_our_block:
                            removed_blocks.append({"day": day_key, "start": start_time, "end": end_time})
                        else:
                            new_blocks.append(block)

                    week_schedule[day_key] = new_blocks

                    removed = original_count - len(week_schedule[day_key])
                    if removed > 0:
                        total_removed += removed
                        self._log(f"Force sync: Removed {removed} orphan blocks from day {day_key}")

            result["removed"] = total_removed

            if total_removed > 0:
                # Build payload and push update
                put_payload = self._build_schedule_payload(week_schedule)
                self._log(f"Pushing cleaned schedule (removed {total_removed} orphan blocks)...")
                success = self.api.update_unlock_schedule(device_id, put_payload)
                result["success"] = success
                if success:
                    # Journal the removals
                    for rb in removed_blocks:
                        self.journal.log_remove(device_id, rb["day"], rb["start"], rb["end"])
                else:
                    result["error"] = "Failed to push cleaned schedule"
            else:
                self._log("No orphan blocks found to remove")

        return result
