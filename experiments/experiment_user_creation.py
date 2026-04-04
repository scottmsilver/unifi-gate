#!/usr/bin/env python3
"""
Standalone experiment script for user creation API.
Preserves session between runs so you only need 2FA once.

Usage:
    # First run - will prompt for 2FA
    .venv/bin/python3 experiment_user_creation.py

    # Subsequent runs - uses saved session
    .venv/bin/python3 experiment_user_creation.py

    # Force fresh login
    .venv/bin/python3 experiment_user_creation.py --fresh
"""

import json
import logging
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unifi_native_api import UniFiNativeAPI

# Setup logging
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def probe_api(api):
    """Probe various endpoints to understand the API structure."""

    print("\n" + "=" * 60)
    print("PROBING API STRUCTURE")
    print("=" * 60)

    # 1. Get current user
    print("\n--- 1. GET /api/users/self ---")
    resp = api._make_request("GET", "/api/users/self")
    if resp:
        # Show key fields for user creation insights
        print(f"  unique_id: {resp.get('unique_id')}")
        print(f"  username: {resp.get('username')!r}")
        print(f"  local_account_exist: {resp.get('local_account_exist')}")
        print(f"  only_local_account: {resp.get('only_local_account')}")
        print(f"  role: {resp.get('role')}")
        print(f"  roleId: {resp.get('roleId')}")
        if "roles" in resp:
            print(f"  roles: {json.dumps(resp['roles'], indent=4)}")
    else:
        print("  FAILED")

    # 2. Get roles
    print("\n--- 2. GET /proxy/users/api/v2/roles ---")
    resp = api._make_request("GET", "/proxy/users/api/v2/roles")
    if resp and "data" in resp:
        for role in resp["data"]:
            print(f"  - {role.get('name')}: {role.get('unique_id')}")
            print(f"    system_key: {role.get('system_key')}, level: {role.get('level')}")
        return resp["data"]  # Return roles for later use
    else:
        print("  FAILED")
        return []


def try_create_user(api, username, password, role_id, payload_override=None):
    """
    Try to create a user with a specific payload.

    Args:
        api: UniFiNativeAPI instance
        username: Username for new user
        password: Password for new user
        role_id: Role UUID to assign
        payload_override: Optional dict to completely override the payload

    Returns:
        (success: bool, response: dict or None)
    """

    if payload_override:
        payload = payload_override
    else:
        # Default payload structure
        payload = {
            "username": username,
            "password": password,
            "first_name": username,
            "last_name": "Admin",
            "local_account_exist": True,
            "only_local_account": True,
            "roles": [{"unique_id": role_id}],
        }

    print(f"\n--- Trying payload ---")
    print(json.dumps(payload, indent=2))

    # Try the endpoint
    endpoint = "/proxy/users/api/v2/user"

    try:
        url = f"{api.host}{endpoint}"
        headers = {"Content-Type": "application/json"}
        if api.csrf_token:
            headers["X-CSRF-Token"] = api.csrf_token

        response = api.session.post(url, json=payload, headers=headers, verify=api.verify_ssl, timeout=10)

        # Update CSRF token
        if "X-CSRF-Token" in response.headers:
            api.csrf_token = response.headers["X-CSRF-Token"]

        print(f"\nHTTP Status: {response.status_code}")

        try:
            data = response.json()
            print(f"Response: {json.dumps(data, indent=2)}")

            # Check for success
            if isinstance(data, dict):
                code = data.get("code")
                if code in ["SUCCESS", "OK", 0, 1, "0", "1"]:
                    return True, data
                else:
                    return False, data
        except:
            print(f"Response text: {response.text[:500]}")
            return False, None

    except Exception as e:
        print(f"Exception: {e}")
        return False, None


def try_endpoint(api, endpoint, payload):
    """Try a specific endpoint with a payload."""
    print(f"\n--- Trying {endpoint} ---")
    print(json.dumps(payload, indent=2))

    try:
        url = f"{api.host}{endpoint}"
        headers = {"Content-Type": "application/json"}
        if api.csrf_token:
            headers["X-CSRF-Token"] = api.csrf_token

        response = api.session.post(url, json=payload, headers=headers, verify=api.verify_ssl, timeout=10)

        if "X-CSRF-Token" in response.headers:
            api.csrf_token = response.headers["X-CSRF-Token"]

        print(f"HTTP Status: {response.status_code}")
        try:
            data = response.json()
            print(f"Response: {json.dumps(data, indent=2)}")
            if isinstance(data, dict):
                code = data.get("code")
                if code in ["SUCCESS", "OK", 0, 1, "0", "1"]:
                    return True, data
            return False, data
        except:
            print(f"Response text: {response.text[:500]}")
            return False, None
    except Exception as e:
        print(f"Exception: {e}")
        return False, None


def run_experiments(api, roles):
    """Run various user creation experiments."""

    print("\n" + "=" * 60)
    print("USER CREATION EXPERIMENTS")
    print("=" * 60)

    # Find Super Admin role
    super_admin_role = None
    for role in roles:
        if role.get("system_key") == "super_administrator":
            super_admin_role = role
            break

    if not super_admin_role:
        print("ERROR: Could not find Super Admin role")
        return

    role_id = super_admin_role["unique_id"]
    print(f"\nUsing Super Admin role: {role_id}")

    # Test username
    test_user = "testadmin_exp"
    test_pass = "TestPass123!"

    strategies = [
        # Strategy 1: CORRECT FORMAT from browser capture
        # Key findings: role_id (string), user_email, force_add_nfc, nfc_token, group_ids, pin_code
        (
            "Strategy 1: Browser-captured format (SHOULD WORK)",
            "/proxy/users/api/v2/user",
            {
                "first_name": test_user,
                "last_name": "Admin",
                "user_email": f"{test_user}@local.test",
                "force_add_nfc": True,
                "nfc_token": "",
                "group_ids": [],
                "pin_code": "",
                "role_id": role_id,  # SINGULAR STRING, not roles array!
                "username": test_user,
                "password": test_pass,
                "only_local_account": True,
            },
        ),
        # Strategy 2: Same but without email (test if email is required)
        (
            "Strategy 2: No email",
            "/proxy/users/api/v2/user",
            {
                "first_name": test_user,
                "last_name": "Admin",
                "force_add_nfc": True,
                "nfc_token": "",
                "group_ids": [],
                "pin_code": "",
                "role_id": role_id,
                "username": test_user,
                "password": test_pass,
                "only_local_account": True,
            },
        ),
        # Strategy 3: Minimal with just role_id
        (
            "Strategy 3: Minimal with role_id",
            "/proxy/users/api/v2/user",
            {
                "first_name": test_user,
                "last_name": "Admin",
                "role_id": role_id,
                "username": test_user,
                "password": test_pass,
                "only_local_account": True,
            },
        ),
    ]

    for name, endpoint, payload in strategies:
        print("\n" + "-" * 40)
        print(name)
        print("-" * 40)
        success, resp = try_endpoint(api, endpoint, payload)
        if success:
            print("\n*** SUCCESS! ***")
            return

    print("\n" + "=" * 60)
    print("All strategies failed. Check responses above for clues.")
    print("=" * 60)


def main():
    # Load credentials from parent directory
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    creds_file = os.path.join(parent_dir, "credentials_native.json")
    if not os.path.exists(creds_file):
        print(f"ERROR: {creds_file} not found")
        sys.exit(1)

    with open(creds_file) as f:
        creds = json.load(f)

    print(f"Connecting to {creds['host']}...")

    # Create API client
    api = UniFiNativeAPI(host=f"https://{creds['host']}", username=creds["username"], password=creds["password"])

    # Check for --fresh flag
    force_new = "--fresh" in sys.argv

    # Login (will use saved session if valid)
    if not api.login(force_new=force_new):
        print("Login failed!")
        sys.exit(1)

    print("Login successful!")

    try:
        # Probe the API first
        roles = probe_api(api)

        if roles:
            # Run experiments
            run_experiments(api, roles)

    finally:
        # Logout but preserve session for next run
        api.logout(clear_session=False)
        print("\nSession preserved for next run.")


if __name__ == "__main__":
    main()
