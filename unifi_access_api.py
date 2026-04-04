import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests


@dataclass
class Door:
    """Represents a UniFi Access door."""

    id: str
    name: str
    status: str  # lock status: "lock" or "unlock"
    position: str = "unknown"  # physical position: "open" or "close"
    lock_rule_type: Optional[str] = None
    is_bind_hub: bool = False

    @property
    def door_lock_relay_status(self) -> str:
        """Legacy property for backward compatibility."""
        return self.status

    @property
    def door_position_status(self) -> str:
        """Legacy property for backward compatibility."""
        return self.position

    @property
    def display_status(self) -> str:
        """Returns human-readable status for the door."""
        # Combine lock and position status
        lock_text = "Unlocked" if self.status == "unlock" else "Locked"
        position_text = "Open" if self.position == "open" else "Closed"

        # Special cases
        if self.lock_rule_type == "keep_unlock":
            lock_text = "Held Unlocked"
        elif hasattr(self, "_is_held_open") and self._is_held_open:
            lock_text = "Held Open"

        # Return combined status
        if self.position != "unknown":
            return f"{lock_text} ({position_text})"
        else:
            return lock_text


@dataclass
class EmergencyStatus:
    """Represents emergency system status."""

    evacuation: bool = False
    lockdown: bool = False


class UnifiAccessAPI:
    """API client for UniFi Access system."""

    def __init__(self, host: Optional[str] = None, token: Optional[str] = None):
        self.host = host
        self.token = token
        self.base_url = f"https://{host}:12445" if host else None
        self._load_credentials()

        # Hold open management
        self._hold_open_threads = {}  # Dict[door_id, thread]
        self._hold_open_stop_events = {}  # Dict[door_id, threading.Event]
        self._hold_open_lock = threading.Lock()

    def _load_credentials(self) -> None:
        """Load credentials from file if not provided."""
        if not self.host or not self.token:
            if os.path.exists("credentials.json"):
                with open("credentials.json", "r") as f:
                    creds = json.load(f)
                    if not self.host:
                        self.host = creds.get("host")
                    if not self.token:
                        self.token = creds.get("token")
                    if self.host and not self.base_url:
                        self.base_url = f"https://{self.host}:12445"

    def _debug_log(self, message: str) -> None:
        """Log debug messages to file."""
        with open("debug.log", "a") as f:
            f.write(f"{datetime.now()}: {message}\n")

    def _make_request(self, method: str, endpoint: str, data: Optional[Dict] = None) -> Optional[Any]:
        """Make an API request."""
        if not self.base_url or not self.token:
            self._debug_log("Missing credentials for API request")
            return None

        headers = {"Authorization": f"Bearer {self.token}"}
        if method in ["PUT", "POST"]:
            headers["Content-Type"] = "application/json"

        url = f"{self.base_url}{endpoint}"
        self._debug_log(f"Request: {method} {url}")
        if data:
            self._debug_log(f"Payload: {json.dumps(data)}")

        try:
            if method == "GET":
                response = requests.get(url, headers=headers, verify=False, timeout=2)
            elif method == "PUT":
                response = requests.put(url, headers=headers, json=data, verify=False, timeout=2)
            elif method == "POST":
                response = requests.post(url, headers=headers, json=data, verify=False, timeout=2)
            else:
                raise ValueError(f"Unsupported method: {method}")

            self._debug_log(f"Response Status: {response.status_code}")
            self._debug_log(f"Response Headers: {dict(response.headers)}")

            try:
                response_json = response.json()
                self._debug_log(f"Response Body: {json.dumps(response_json)}")
            except:
                self._debug_log(f"Response Body (text): {response.text}")
                response_json = None

            response.raise_for_status()

            # Check for API error codes in successful responses
            if response_json and isinstance(response_json, dict):
                code = response_json.get("code", "")
                # Check for actual error codes (not SUCCESS or OK)
                if code and code not in ["OK", "SUCCESS"]:
                    # This is an error response despite 200 status
                    error_msg = response_json.get("msg", "Unknown error")
                    self._debug_log(f"API Error: {code} - {error_msg}")
                    return None

            return response_json
        except requests.exceptions.RequestException as e:
            self._debug_log(f"Error {method} {endpoint}: {e}")
            if hasattr(e, "response") and e.response is not None:
                self._debug_log(f"Error Response Status: {e.response.status_code}")
                self._debug_log(f"Error Response Body: {e.response.text}")
            return None

    def get_doors(self) -> List[Door]:
        """Fetch all doors with their current status."""
        doors = []
        doors_data = self._make_request("GET", "/api/v1/developer/doors")

        if not doors_data or "data" not in doors_data:
            return doors

        # Get hold open status
        hold_open_status = self.get_hold_open_status()

        for door_data in doors_data["data"]:
            door_id = door_data["id"]

            # Get lock rule for this door
            lock_rule_data = self._make_request("GET", f"/api/v1/developer/doors/{door_id}/lock_rule")
            lock_rule_type = None
            if lock_rule_data and "data" in lock_rule_data:
                lock_rule_type = lock_rule_data["data"].get("type")

            door = Door(
                id=door_id,
                name=door_data.get("name", "Unknown"),
                status=door_data.get("door_lock_relay_status", "unknown"),
                position=door_data.get("door_position_status", "unknown"),
                lock_rule_type=lock_rule_type,
                is_bind_hub=door_data.get("is_bind_hub", False),
            )

            # Mark if this door is being held open
            door._is_held_open = door_id in hold_open_status and hold_open_status[door_id]

            doors.append(door)

        return doors

    def get_emergency_status(self) -> EmergencyStatus:
        """Get current emergency system status."""
        data = self._make_request("GET", "/api/v1/developer/doors/settings/emergency")

        if data and "data" in data:
            return EmergencyStatus(
                evacuation=data["data"].get("evacuation", False), lockdown=data["data"].get("lockdown", False)
            )

        return EmergencyStatus()

    def unlock_door(self, door_id: str) -> bool:
        """Unlock a specific door temporarily."""
        result = self._make_request("PUT", f"/api/v1/developer/doors/{door_id}/unlock", {})
        return result is not None

    def hold_unlock(self, door_id: str) -> bool:
        """Hold a door in unlocked state."""
        self._debug_log(f"\n=== HOLD UNLOCK CALLED for door_id: {door_id} ===")
        result = self._make_request("PUT", f"/api/v1/developer/doors/{door_id}/lock_rule", {"type": "keep_unlock"})
        self._debug_log(f"hold_unlock result: {result}")

        # Check if we got a valid success response
        success = result is not None and isinstance(result, dict) and "data" in result
        self._debug_log(f"hold_unlock returning: {success}")
        return success

    def temporary_unlock(self, door_id: str, duration_minutes: int = 10) -> bool:
        """Temporarily unlock a door for a specified duration.

        Args:
            door_id: The ID of the door to unlock
            duration_minutes: Duration in minutes to keep the door unlocked (default: 10)

        Returns:
            bool: True if successful, False otherwise
        """
        self._debug_log(f"\n=== TEMPORARY UNLOCK CALLED for door_id: {door_id}, duration: {duration_minutes} min ===")

        # Use the "custom" type with interval parameter for temporary unlock
        payload = {"type": "custom", "interval": duration_minutes}

        result = self._make_request("PUT", f"/api/v1/developer/doors/{door_id}/lock_rule", payload)
        self._debug_log(f"temporary_unlock result: {result}")

        # Check if we got a valid success response
        success = result is not None and isinstance(result, dict) and "data" in result

        if success and "data" in result:
            # Log the end time if available
            end_time = result["data"].get("ended_time")
            if end_time:
                from datetime import datetime

                end_datetime = datetime.fromtimestamp(end_time)
                self._debug_log(f"Door will be unlocked until: {end_datetime}")

        self._debug_log(f"temporary_unlock returning: {success}")
        return success

    def lock_door(self, door_id: str) -> bool:
        """Lock a door (reset to normal operation)."""
        result = self._make_request("PUT", f"/api/v1/developer/doors/{door_id}/lock_rule", {"type": "reset"})
        return result is not None

    def hold_open(self, door_id: str, interval_seconds: int = 10) -> bool:
        """
        Start holding a door/gate open by repeatedly sending unlock commands.

        This is a software-based solution for gates that don't support lock_rule API.
        It will send an unlock command every interval_seconds to keep the gate open.

        Args:
            door_id: The ID of the door/gate to hold open
            interval_seconds: How often to send unlock command (default: 10 seconds)

        Returns:
            bool: True if hold open started successfully, False otherwise
        """
        with self._hold_open_lock:
            # Stop any existing hold open for this door
            if door_id in self._hold_open_threads:
                self.stop_hold_open(door_id)

            # Create stop event for this door
            stop_event = threading.Event()
            self._hold_open_stop_events[door_id] = stop_event

            # Define the worker function
            def hold_worker():
                self._debug_log(f"\n=== HOLD OPEN STARTED for door_id: {door_id} (interval: {interval_seconds}s) ===")
                while not stop_event.is_set():
                    try:
                        # Send unlock command
                        result = self.unlock_door(door_id)
                        if result:
                            self._debug_log(f"Hold open unlock sent for {door_id}")
                        else:
                            self._debug_log(f"Hold open unlock failed for {door_id}")
                    except Exception as e:
                        self._debug_log(f"Error in hold open for {door_id}: {e}")

                    # Wait for interval or until stop event is set
                    stop_event.wait(interval_seconds)

                self._debug_log(f"=== HOLD OPEN STOPPED for door_id: {door_id} ===")

            # Start the worker thread
            thread = threading.Thread(target=hold_worker, daemon=True, name=f"hold_open_{door_id}")
            thread.start()
            self._hold_open_threads[door_id] = thread

            self._debug_log(f"Hold open thread started for {door_id}")
            return True

    def stop_hold_open(self, door_id: str) -> bool:
        """
        Stop holding a door/gate open.

        Args:
            door_id: The ID of the door/gate to stop holding open

        Returns:
            bool: True if stopped successfully, False if door wasn't being held open
        """
        with self._hold_open_lock:
            if door_id not in self._hold_open_threads:
                self._debug_log(f"No hold open active for door_id: {door_id}")
                return False

            # Signal the thread to stop
            self._hold_open_stop_events[door_id].set()

            # Wait for thread to finish (with timeout)
            thread = self._hold_open_threads[door_id]
            thread.join(timeout=2)

            # Clean up
            del self._hold_open_threads[door_id]
            del self._hold_open_stop_events[door_id]

            self._debug_log(f"Hold open stopped for door_id: {door_id}")
            return True

    def stop_all_hold_open(self) -> int:
        """
        Stop all active hold open operations.

        Returns:
            int: Number of hold opens stopped
        """
        with self._hold_open_lock:
            door_ids = list(self._hold_open_threads.keys())
            count = 0
            for door_id in door_ids:
                if self.stop_hold_open(door_id):
                    count += 1
            return count

    def get_hold_open_status(self) -> Dict[str, bool]:
        """
        Get the status of all doors with hold open active.

        Returns:
            Dict[door_id, is_active]: Dictionary of door IDs and whether hold open is active
        """
        with self._hold_open_lock:
            return {door_id: thread.is_alive() for door_id, thread in self._hold_open_threads.items()}

    def set_emergency_status(self, evacuation: bool = False, lockdown: bool = False) -> bool:
        """Set emergency system status."""
        result = self._make_request(
            "PUT", "/api/v1/developer/doors/settings/emergency", {"evacuation": evacuation, "lockdown": lockdown}
        )
        return result is not None

    def toggle_evacuation(self) -> bool:
        """Toggle evacuation mode."""
        current = self.get_emergency_status()
        return self.set_emergency_status(evacuation=not current.evacuation, lockdown=False)
