#!/usr/bin/env python3
"""
Native UniFi Access API Client

This implementation uses the native UniFi Access API (same as the web UI)
which allows for more advanced features like infinite hold times.
Based on the approach used by hjdhjd's unifi-access TypeScript library.
"""

import json
import logging
import os
import pickle
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Suppress SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


@dataclass
class NativeDoor:
    """Represents a door in the native API."""

    id: str
    name: str
    location_id: str
    unique_id: str
    is_online: bool
    lock_status: str
    door_position: str
    is_held_open: bool = False

    @property
    def display_status(self) -> str:
        """Get display-friendly status."""
        if self.is_held_open:
            return "Held Open"
        if not self.is_online:
            return "Offline"

        # Combine lock and door position
        lock = self.lock_status.title()
        position = f" ({self.door_position.title()})" if self.door_position else ""
        return f"{lock}{position}"


class UniFiNativeAPI:
    """Native UniFi Access API client with advanced capabilities."""

    def __init__(
        self, host: str, username: str, password: str, verify_ssl: bool = False, session_file: Optional[str] = None
    ):
        """
        Initialize the API client.

        Args:
            host: UniFi Access controller hostname/IP
            username: Admin username
            password: Admin password
            verify_ssl: Whether to verify SSL certificates
            session_file: Path to session persistence file
        """
        self.host = host.rstrip("/")
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl

        # Session persistence
        self.session_file = session_file or os.path.expanduser("~/.unifi_access_session")

        # Session setup with retry logic
        self.session = requests.Session()
        retry = Retry(total=3, backoff_factor=0.5)
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        # Will be set during login
        self.csrf_token = None
        self.logged_in = False

        # Bootstrap data cache
        self._bootstrap = None

    def _save_session(self) -> None:
        """Save session data to file for reuse."""
        session_data = {
            "cookies": self.session.cookies.get_dict(),
            "csrf_token": self.csrf_token,
            "host": self.host,
            "saved_at": datetime.now().isoformat(),
        }

        try:
            with open(self.session_file, "w") as f:
                json.dump(session_data, f)
            logger.debug(f"Session saved to {self.session_file}")
        except Exception as e:
            logger.warning(f"Could not save session: {e}")

    def _load_session(self) -> bool:
        """
        Load saved session from file.

        Returns:
            True if session loaded and appears valid, False otherwise
        """
        if not os.path.exists(self.session_file):
            return False

        try:
            with open(self.session_file, "r") as f:
                session_data = json.load(f)

            # Check if session is for same host
            if session_data.get("host") != self.host:
                logger.debug("Saved session is for different host")
                return False

            # Check if session is recent (within 24 hours)
            saved_at = datetime.fromisoformat(session_data.get("saved_at", ""))
            if datetime.now() - saved_at > timedelta(hours=24):
                logger.debug("Saved session is too old")
                return False

            # Restore session data
            cookies = session_data.get("cookies", {})
            self.session.cookies.update(cookies)
            self.csrf_token = session_data.get("csrf_token")

            logger.info(f"Loaded saved session (cookies: {len(cookies)} items)")
            logger.debug(f"Cookie names: {list(cookies.keys())}")
            return True

        except Exception as e:
            logger.debug(f"Could not load session: {e}")
            return False

    def _validate_session(self) -> bool:
        """
        Check if current session is still valid.

        Returns:
            True if session is valid, False otherwise
        """
        # Use the bootstrap endpoint per hjdhjd's implementation
        try:
            url = f"{self.host}/proxy/access/api/v2/devices/topology4"
            headers = {
                "Accept": "application/json",
            }
            if self.csrf_token:
                headers["X-CSRF-Token"] = self.csrf_token

            response = self.session.get(url, headers=headers, verify=self.verify_ssl, timeout=10)

            # Check if we get a successful response
            if response.status_code == 200:
                self.logged_in = True
                logger.info("Existing session is valid - no 2FA needed!")
                self._fetch_bootstrap()  # Ensure bootstrap is loaded
                return True
            else:
                logger.debug(f"Session validation failed with status {response.status_code}")
                return False

        except Exception as e:
            logger.debug(f"Session validation failed: {e}")
            return False

    def _make_request(
        self,
        method: str,
        endpoint: str,
        json_data: Optional[Dict] = None,
        require_auth: bool = True,
        _is_retry: bool = False,
    ) -> Optional[Dict]:
        """
        Make an API request with proper headers and error handling.
        Automatically re-logins and retries once on 401 (expired session).

        Args:
            method: HTTP method (GET, POST, PUT, etc.)
            endpoint: API endpoint (relative to base URL)
            json_data: JSON payload for request
            require_auth: Whether authentication is required
            _is_retry: Internal flag to prevent infinite retry loops

        Returns:
            Response JSON or None on error
        """
        if require_auth and not self.logged_in:
            logger.error("Not logged in")
            return None

        url = f"{self.host}{endpoint}"
        headers = {
            "Content-Type": "application/json",
        }

        # Add CSRF token if we have it
        if self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token

        try:
            response = self.session.request(
                method, url, json=json_data, headers=headers, verify=self.verify_ssl, timeout=10
            )

            # Update CSRF token from response if present
            if "X-CSRF-Token" in response.headers:
                self.csrf_token = response.headers["X-CSRF-Token"]

            response.raise_for_status()

            # Some endpoints return empty responses
            if response.text:
                data = response.json()

                # Check for application-level errors
                if isinstance(data, dict) and "code" in data:
                    code = data["code"]
                    # Known success codes: "SUCCESS", "OK", 0 (sometimes), 1 (sometimes seen)
                    # Error codes are usually non-zero numbers or strings like "CODE_..."
                    if code not in ["SUCCESS", "OK", 0, "0", 1, "1"]:
                        logger.error(f"API returned error code: {code} - {data.get('msg', 'No message')}")
                        if response.text:  # Print full response for debugging
                            logger.error(f"Full error response: {response.text}")
                        return None

                return data
            return {}

        except requests.exceptions.RequestException as e:
            # Auto-relogin on 401 (expired session), retry once
            if not _is_retry and e.response is not None and e.response.status_code == 401:
                logger.warning("Got 401 — session expired, attempting re-login...")
                if self.login(force_new=True):
                    logger.info("Re-login successful, retrying request")
                    return self._make_request(
                        method, endpoint, json_data=json_data, require_auth=require_auth, _is_retry=True
                    )
                else:
                    logger.error("Re-login failed, giving up")
                    return None

            logger.error(f"Request failed{' (retry)' if _is_retry else ''}: {e}")
            if e.response is not None:
                logger.error(f"Response text: {e.response.text}")
            return None

    def login(self, auth_code: Optional[str] = None, force_new: bool = False) -> bool:
        """
        Authenticate with the UniFi Access controller.

        Args:
            auth_code: Optional 2FA code from authenticator app
            force_new: Force new login even if saved session exists

        Returns:
            True if login successful, False otherwise
        """
        # Try to use saved session unless forced to get new one
        if not force_new:
            if self._load_session() and self._validate_session():
                return True
            logger.info("Saved session invalid or expired, logging in fresh")

        # First, get initial CSRF token
        logger.info("Getting initial CSRF token...")
        response = self.session.get(self.host, verify=self.verify_ssl, timeout=10)

        # Extract CSRF token from headers
        if "X-CSRF-Token" in response.headers:
            self.csrf_token = response.headers["X-CSRF-Token"]
            logger.debug("Got initial CSRF token")

        # Login with credentials
        logger.info(f"Logging in to {self.host}...")
        login_data = {"username": self.username, "password": self.password, "rememberMe": True}

        # Add 2FA token if provided
        if auth_code:
            login_data["token"] = auth_code
            logger.info("Using 2FA authentication code")
        else:
            login_data["token"] = ""

        # Attempt login
        try:
            url = f"{self.host}/api/auth/login"
            headers = {
                "Content-Type": "application/json",
            }
            if self.csrf_token:
                headers["X-CSRF-Token"] = self.csrf_token

            response = self.session.post(url, json=login_data, headers=headers, verify=self.verify_ssl, timeout=10)

            # Update CSRF token from response if present
            if "X-CSRF-Token" in response.headers:
                self.csrf_token = response.headers["X-CSRF-Token"]

            # Check for 2FA requirement (499 status code)
            if response.status_code == 499:
                logger.warning("2FA authentication required")

                # If no auth code provided, prompt for it
                if not auth_code:
                    auth_code_input = input("Enter 2FA code from authenticator app: ")
                    return self.login(auth_code=auth_code_input)
                else:
                    logger.error("Invalid 2FA code")
                    return False

            response.raise_for_status()

            # Parse response
            if response.text:
                result = response.json()
            else:
                result = {}

            self.logged_in = True
            logger.info("Login successful")

            # Save session for future use
            self._save_session()

            # Fetch bootstrap data immediately
            self._fetch_bootstrap()
            return True

        except requests.exceptions.RequestException as e:
            logger.error(f"Login failed: {e}")
            return False

    def logout(self, clear_session: bool = True) -> bool:
        """
        Logout from the UniFi Access controller.

        Args:
            clear_session: Whether to clear saved session (default True)

        Returns:
            True if logout successful, False otherwise
        """
        if not self.logged_in:
            return True

        result = self._make_request("POST", "/api/auth/logout")
        self.logged_in = False
        self.csrf_token = None
        self._bootstrap = None

        # Only clear saved session if requested
        if clear_session:
            self.clear_saved_session()

        return result is not None

    def clear_saved_session(self) -> None:
        """Clear any saved session data."""
        try:
            if os.path.exists(self.session_file):
                os.remove(self.session_file)
                logger.info("Cleared saved session")
        except Exception as e:
            logger.warning(f"Could not clear saved session: {e}")

    def _fetch_bootstrap(self) -> bool:
        """
        Fetch bootstrap data from UniFi Access.

        Returns:
            True if successful, False otherwise
        """
        logger.info("Fetching bootstrap data...")

        # Use the bootstrap endpoint as per hjdhjd's implementation
        bootstrap = self._make_request("GET", "/proxy/access/api/v2/devices/topology4")

        if bootstrap:
            # Store the bootstrap data
            self._bootstrap = bootstrap
            logger.info("Device data fetched successfully")
            return True

        logger.error("Failed to fetch device data")
        return False

    def get_site_name(self) -> str:
        """
        Get the site name from the bootstrap topology.

        Returns:
            Site name string (e.g. "Home") or "Home Access" default
        """
        if not self._bootstrap:
            self._fetch_bootstrap()

        if self._bootstrap and "data" in self._bootstrap:
            data = self._bootstrap["data"]
            if isinstance(data, list) and len(data) > 0:
                return data[0].get("name", "Home Access")

        return "Home Access"

    def get_site_timezone(self) -> Optional[str]:
        """
        Get the site timezone from the bootstrap topology.

        Returns:
            IANA timezone string (e.g. "America/Los_Angeles") or None
        """
        if not self._bootstrap:
            self._fetch_bootstrap()

        if self._bootstrap and "data" in self._bootstrap:
            data = self._bootstrap["data"]
            if isinstance(data, list) and len(data) > 0:
                return data[0].get("timezone")

        return None

    def get_doors(self) -> List[NativeDoor]:
        """
        Get list of all logical doors with their current status.
        Uses the Developer API endpoint for reliable door listing.

        Returns:
            List of NativeDoor objects
        """
        # Use the developer endpoint which reliably lists all doors (logical entities)
        response = self._make_request("GET", "/api/v1/developer/doors")

        if not response or "data" not in response:
            logger.error("Failed to get doors data from developer endpoint")
            return []

        doors = []
        for door_data in response["data"]:
            door = NativeDoor(
                id=door_data.get("id"),
                name=door_data.get("name", "Unknown"),
                location_id=door_data.get("floor_id", ""),  # or other location field
                unique_id=door_data.get("id"),
                is_online=door_data.get("is_bind_hub", False),  # Heuristic: if bound, it's online/active
                lock_status=door_data.get("door_lock_relay_status", "unknown"),
                door_position=door_data.get("door_position_status", "unknown"),
                is_held_open=False,
            )
            doors.append(door)

        logger.info(f"Found {len(doors)} logical doors via Developer API")
        return sorted(doors, key=lambda d: d.name)

    def unlock_door(self, door_id: str, duration_minutes: Optional[float] = None, use_location: bool = False) -> bool:
        """
        Unlock a door for a specified duration.

        Args:
            door_id: Door unique ID or location ID
            duration_minutes: How long to unlock (None = default, 0 = reset, float('inf') = permanent)
            use_location: If True, use location endpoint for standard unlock

        Returns:
            True if successful, False otherwise
        """
        logger.info(f"Unlocking door {door_id} for {duration_minutes} minutes...")

        # Based on hjdhjd's unifi-access implementation exactly
        if duration_minutes is None and use_location:
            # Standard unlock using location endpoint (as hjdhjd does)
            endpoint = f"/proxy/access/api/v2/location/{door_id}/unlock"
            logger.info("Performing standard unlock via location")
            result = self._make_request("PUT", endpoint)

        elif duration_minutes is not None:
            # Use lock_rule endpoint for any duration-based operation
            # Add get_result parameter as hjdhjd does
            endpoint = f"/proxy/access/api/v2/device/{door_id}/lock_rule?get_result=true"

            if duration_minutes == float("inf"):
                # Keep unlocked indefinitely
                payload = {"type": "keep_unlock"}
                logger.info("Setting door to stay unlocked indefinitely")

            elif duration_minutes == 0:
                # Reset to secure/locked state
                payload = {"type": "reset"}
                logger.info("Resetting door to secure state")

            else:
                # Custom duration in minutes
                payload = {"type": "custom", "interval": int(duration_minutes)}
                logger.info(f"Unlocking for {duration_minutes} minutes")

            result = self._make_request("PUT", endpoint, json_data=payload)

        else:
            # Try standard device unlock first
            endpoint = f"/proxy/access/api/v2/device/{door_id}/unlock"
            logger.info("Performing standard unlock via device")
            result = self._make_request("PUT", endpoint)

        if result is not None:
            logger.info(f"Successfully unlocked door {door_id}")
            return True

        logger.error(f"Failed to unlock door {door_id}")
        return False

    def hold_open(self, door_id: str) -> bool:
        """
        Hold a door open indefinitely using keep_unlock.

        Args:
            door_id: Door unique ID

        Returns:
            True if successful, False otherwise
        """
        return self.unlock_door(door_id, float("inf"))

    def stop_hold_open(self, door_id: str) -> bool:
        """
        Stop holding a door open (reset to secure).

        Args:
            door_id: Door unique ID

        Returns:
            True if successful, False otherwise
        """
        return self.unlock_door(door_id, 0)

    def get_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Get recent access events.

        Args:
            limit: Maximum number of events to return

        Returns:
            List of event dictionaries
        """
        # Try UniFi Access v2 events endpoint
        result = self._make_request("GET", f"/proxy/access/api/v2/event?limit={limit}")

        if result:
            # Check if it's wrapped in a data field or returned directly
            if isinstance(result, dict) and "data" in result:
                return result["data"]
            elif isinstance(result, list):
                return result

        return []

    def _get_device_config(self, device_id: str) -> Optional[Dict[str, Any]]:
        """
        Get raw device configuration.

        Args:
            device_id: Device unique ID

        Returns:
            Device configuration dictionary or None
        """
        # Fetch all devices and find the specific one
        # Note: There isn't a direct single-device endpoint that returns full config
        # in the same format as the bulk endpoint usually.
        response = self._make_request("GET", "/proxy/access/api/v2/devices")

        if not response:
            return None

        devices = response.get("data", []) if isinstance(response, dict) else response

        for device in devices:
            if device.get("unique_id") == device_id or device.get("id") == device_id:
                return device

        return None

    def _set_device_config(self, device_id: str, config_data: Dict[str, Any]) -> bool:
        """
        Update device configuration.

        Args:
            device_id: Device unique ID
            config_data: Full configuration payload

        Returns:
            True if successful
        """
        # Try plural endpoint first
        endpoint = f"/proxy/access/api/v2/devices/{device_id}"
        result = self._make_request("PUT", endpoint, json_data=config_data)

        if result is None:
            # Fallback to singular
            logger.debug("Plural endpoint failed, trying singular...")
            endpoint = f"/proxy/access/api/v2/device/{device_id}"
            result = self._make_request("PUT", endpoint, json_data=config_data)

        if result is None:
            # Fallback to /basic
            logger.debug("Singular endpoint failed, trying /basic...")
            endpoint = f"/proxy/access/api/v2/device/{device_id}/basic"
            result = self._make_request("PUT", endpoint, json_data=config_data)

        return result is not None

    def get_unlock_schedule(self, device_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the unlock schedule for a device (door).

        Args:
            device_id: Door unique ID

        Returns:
            Schedule configuration dictionary or None
        """
        # Use the discovered endpoint
        endpoint = f"/proxy/access/api/v2/unlock_schedule/{device_id}"
        response = self._make_request("GET", endpoint)

        if response and isinstance(response, dict) and "data" in response:
            return response["data"]

        return response

    def update_unlock_schedule(self, device_id: str, schedule_payload: Dict[str, Any]) -> bool:
        """
        Update the unlock schedule for a device (door).

        Args:
            device_id: Door unique ID
            schedule_payload: Full schedule payload matching the API requirements

        Returns:
            True if successful
        """
        endpoint = f"/proxy/access/api/v2/unlock_schedule/{device_id}"
        logger.info(f"Updating unlock schedule for {device_id}...")
        result = self._make_request("PUT", endpoint, json_data=schedule_payload)
        return result is not None

    def get_self(self) -> Optional[Dict[str, Any]]:
        """
        Get information about the currently logged-in user.

        Returns:
            User dictionary or None
        """
        # Try the Core OS endpoint first - this is the source of truth for the User ID
        # needed for system-level operations like API keys.
        logger.debug("Fetching self user from /api/users/self")
        response = self._make_request("GET", "/api/users/self")

        if response and isinstance(response, dict):
            # Core endpoint usually returns data directly
            if "data" in response:
                # Sometimes wrapped in data
                return response["data"]
            if "id" in response:
                return response

        # Fallback to Access Proxy endpoint
        logger.debug("Fallback: Fetching self user from /proxy/access/api/v2/user/self")
        response = self._make_request("GET", "/proxy/access/api/v2/user/self?isAccess=1")

        if response and isinstance(response, dict):
            if "data" in response:
                return response["data"]
            if "id" in response:
                return response

        logger.error(
            f"Failed to find user ID in self response. Response keys: {list(response.keys()) if response else 'None'}"
        )
        return None

    def create_api_token(self, name: str) -> Optional[str]:
        """
        Create a new UniFi Access Developer API Token.

        Args:
            name: Name for the new token

        Returns:
            The token string if successful, None otherwise
        """
        # UniFi Access Developer API tokens are created via the Access proxy endpoint
        endpoint = "/proxy/access/api/v1/developer/tokens"
        logger.info(f"Creating Access Developer API token '{name}'...")

        # Full scopes for developer API access
        payload = {
            "name": name,
            "validity_period": 0,  # 0 = never expires
            "scopes": [
                "edit:user",
                "edit:space",
                "edit:visitor",
                "edit:credential",
                "view:system_log",
                "edit:policy",
                "view:device",
            ],
        }

        result = self._make_request("POST", endpoint, json_data=payload)

        # Extract token from response (field is 'api_key', not 'token')
        if result and "data" in result:
            token = result["data"].get("api_key")
            if token:
                logger.info("Successfully created Access Developer API token")
                return token

        logger.error(f"Failed to create API token. Result: {result}")
        return None

    def get_roles(self) -> List[Dict[str, Any]]:
        """
        Get available user roles.

        Returns:
            List of role dictionaries with unique_id, name, system_key, level
        """
        endpoint = "/proxy/users/api/v2/roles"
        logger.debug("Fetching roles...")

        result = self._make_request("GET", endpoint)

        if result and "data" in result:
            return result["data"]

        return []

    def create_user(
        self,
        username: str,
        password: str,
        first_name: str,
        last_name: str,
        role_id: str,
        email: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Create a new local admin user.

        Args:
            username: Username for the new user
            password: Password for the new user
            first_name: User's first name
            last_name: User's last name
            role_id: Role UUID to assign (get from get_roles())
            email: Optional email address

        Returns:
            Created user dictionary if successful, None otherwise
        """
        endpoint = "/proxy/users/api/v2/user"

        # Payload format discovered via browser traffic capture
        # Key insight: use role_id (string), NOT roles (array)
        payload = {
            "first_name": first_name,
            "last_name": last_name,
            "user_email": email or f"{username}@local.internal",
            "force_add_nfc": True,
            "nfc_token": "",
            "group_ids": [],
            "pin_code": "",
            "role_id": role_id,
            "username": username,
            "password": password,
            "only_local_account": True,
        }

        logger.info(f"Creating user '{username}'...")
        result = self._make_request("POST", endpoint, json_data=payload)

        if result and "data" in result:
            logger.info(f"Successfully created user '{username}'")
            return result["data"]

        logger.error(f"Failed to create user '{username}'")
        return None

    def get_super_admin_role_id(self) -> Optional[str]:
        """
        Get the Super Admin role UUID.

        Returns:
            Super Admin role UUID string or None
        """
        roles = self.get_roles()
        for role in roles:
            if role.get("system_key") == "super_administrator":
                return role.get("unique_id")
        return None

    # Legacy methods kept for compatibility if needed, but redirecting to new ones where appropriate
    def get_device_schedule(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Alias for get_unlock_schedule."""
        return self.get_unlock_schedule(device_id)

    def set_device_schedule(self, device_id: str, schedule_data: Any) -> bool:
        """Alias for update_unlock_schedule."""
        return self.update_unlock_schedule(device_id, schedule_data)


if __name__ == "__main__":
    # Test the API client
    import json
    import os

    # Try multiple credential file options
    creds = None
    for cred_file in ["credentials_native.json", "credentials.json"]:
        if os.path.exists(cred_file):
            with open(cred_file, "r") as f:
                creds = json.load(f)
                print(f"Using credentials from {cred_file}")
                break

    if not creds:
        print("✗ No credentials file found")
        print("Please create credentials_native.json with:")
        print("{")
        print('    "host": "your-controller-ip",')
        print('    "username": "admin",')
        print('    "password": "your-password"')
        print("}")
        exit(1)

    # Create client
    api = UniFiNativeAPI(
        host=f"https://{creds['host']}",
        username=creds.get("username", "admin"),
        password=creds.get("password", creds.get("token", "")),
    )

    # Test login
    if api.login():
        print("✓ Login successful")

        # Get doors
        doors = api.get_doors()
        print(f"\n✓ Found {len(doors)} doors:")

        for door in doors:
            print(f"  - {door.name}: {door.display_status} (ID: {door.id})")

        # Logout
        api.logout()
        print("\n✓ Logged out")
    else:
        print("✗ Login failed")
