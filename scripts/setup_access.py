#!/usr/bin/env python3
"""
Cloudflare Access Setup Script

Configures Cloudflare Access Application and Policies via API.
Reads CLOUDFLARE_API_TOKEN from .env file.
Idempotent - safe to run multiple times.
"""

import json
import os
import sys
from pathlib import Path

import requests

# Path to .env file (in project root)
ENV_FILE = Path(__file__).parent.parent / ".env"


def load_env():
    """Load environment variables from .env file."""
    env = {}
    if ENV_FILE.exists():
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    value = value.strip().strip("'\"")
                    env[key.strip()] = value
                    os.environ[key.strip()] = value
    return env


def get_input(prompt, default=None):
    """Get user input with optional default."""
    if default:
        user_input = input(f"{prompt} [{default}]: ").strip()
        return user_input if user_input else default
    return input(f"{prompt}: ").strip()


def print_header(title):
    """Print a section header."""
    print("\n" + "=" * 60)
    print(f" {title}")
    print("=" * 60)


class CloudflareAccess:
    """Cloudflare Access API client."""

    BASE_URL = "https://api.cloudflare.com/client/v4"

    def __init__(self, api_token, account_id=None):
        self.api_token = api_token
        self.account_id = account_id
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }

    def _request(self, method, endpoint, json_data=None):
        """Make an API request."""
        url = f"{self.BASE_URL}{endpoint}"
        try:
            resp = requests.request(method, url, headers=self.headers, json=json_data)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            if e.response is not None:
                try:
                    error_data = e.response.json()
                    errors = error_data.get("errors", [])
                    if errors:
                        print(f"  API Error: {errors[0].get('message', str(e))}")
                except Exception:
                    print(f"  API Error: {e.response.text}")
            return None

    def get_account_id(self):
        """Fetch account ID from API if not provided."""
        if self.account_id:
            return self.account_id

        print("--> Fetching Account ID from API...")
        result = self._request("GET", "/accounts")
        if result and result.get("result"):
            account = result["result"][0]
            self.account_id = account["id"]
            print(f"✓ Found Account: {account['name']} ({self.account_id})")
            return self.account_id
        print("✗ Could not fetch Account ID")
        return None

    def list_apps(self):
        """List all Access Applications."""
        result = self._request("GET", f"/accounts/{self.account_id}/access/apps")
        return result.get("result", []) if result else []

    def find_app_by_domain(self, domain):
        """Find an Access Application by domain."""
        apps = self.list_apps()
        for app in apps:
            if app.get("domain") == domain:
                return app
        return None

    def create_app(self, name, domain):
        """Create an Access Application."""
        print(f"--> Creating Access Application: {name} ({domain})...")
        payload = {
            "name": name,
            "domain": domain,
            "type": "self_hosted",
            "session_duration": "24h",
            "auto_redirect_to_identity": True,
        }
        result = self._request("POST", f"/accounts/{self.account_id}/access/apps", payload)
        if result and result.get("result"):
            app = result["result"]
            print(f"✓ Application created. UID: {app['uid']}")
            return app
        return None

    def list_policies(self, app_id):
        """List policies for an application."""
        result = self._request("GET", f"/accounts/{self.account_id}/access/apps/{app_id}/policies")
        return result.get("result", []) if result else []

    def find_policy_by_name(self, app_id, name):
        """Find a policy by name."""
        policies = self.list_policies(app_id)
        for policy in policies:
            if policy.get("name") == name:
                return policy
        return None

    def create_or_update_policy(self, app_id, name, email, service_token_id=None):
        """Create or update an Access Policy."""
        existing = self.find_policy_by_name(app_id, name)

        # Build include rules
        include = [{"email": {"email": email}}]
        if service_token_id:
            include.append({"service_token": {"token_id": service_token_id}})

        payload = {
            "name": name,
            "decision": "allow",
            "include": include,
        }

        if existing:
            print(f"--> Updating existing policy: {name}...")
            endpoint = f"/accounts/{self.account_id}/access/apps/{app_id}/policies/{existing['id']}"
            result = self._request("PUT", endpoint, payload)
        else:
            print(f"--> Creating new policy: {name}...")
            endpoint = f"/accounts/{self.account_id}/access/apps/{app_id}/policies"
            result = self._request("POST", endpoint, payload)

        if result and result.get("result"):
            print(f"✓ Policy configured for {email}")
            return result["result"]
        return None

    def list_service_tokens(self):
        """List all service tokens."""
        result = self._request("GET", f"/accounts/{self.account_id}/access/service_tokens")
        return result.get("result", []) if result else []

    def find_service_token(self, name):
        """Find a service token by name."""
        tokens = self.list_service_tokens()
        for token in tokens:
            if token.get("name") == name:
                return token
        return None

    def create_service_token(self, name):
        """Create a service token (for API access)."""
        print(f"--> Creating Service Token: {name}...")
        payload = {"name": name, "duration": "8760h"}  # 1 year
        result = self._request("POST", f"/accounts/{self.account_id}/access/service_tokens", payload)
        if result and result.get("result"):
            token = result["result"]
            print(f"✓ Service Token created: {token['name']}")
            print(f"  Client ID: {token['client_id']}")
            print(f"  Client Secret: {token['client_secret']}")
            return token
        return None


def main(hostname_arg=None, email_arg=None):
    print_header("Cloudflare Access Setup")
    print("This script configures Cloudflare Access to secure your tunnel.")

    # Load existing config
    env = load_env()

    # Check for required credentials
    api_token = env.get("CLOUDFLARE_API_TOKEN")
    if not api_token:
        print("\n✗ CLOUDFLARE_API_TOKEN not found in .env")
        print("Please run the setup wizard first or add it manually.")
        return 1

    account_id = env.get("CLOUDFLARE_ACCOUNT_ID")

    # Prompt for hostname
    print("\nEnter the hostname for your tunnel (e.g., home.yourdomain.com)")
    hostname = hostname_arg if hostname_arg else get_input("Hostname")
    if not hostname:
        print("✗ Hostname is required")
        return 1

    # Prompt for email
    email = email_arg if email_arg else get_input("\nYour email address (for access)")
    if not email:
        print("✗ Email is required")
        return 1

    # Initialize API client
    cf = CloudflareAccess(api_token, account_id)

    # Get/verify account ID
    if not cf.get_account_id():
        return 1

    # Save account ID if we fetched it
    if not account_id:
        env["CLOUDFLARE_ACCOUNT_ID"] = cf.account_id

    print(f"\n--- Configuring Access for {hostname} ---")

    # 1. Check/Create Access Application
    app_name = "UniFi Gate"
    print("\n--> Checking for existing Access Application...")

    app = cf.find_app_by_domain(hostname)
    if app:
        print(f"✓ Application already exists: {app['name']} (UID: {app['uid']})")
    else:
        app = cf.create_app(app_name, hostname)
        if not app:
            print("✗ Failed to create Access Application")
            return 1

    app_id = app["uid"]

    # 2. Check/Create Service Token (optional, for API access)
    service_token_name = "UniFi Gate API"
    service_token = cf.find_service_token(service_token_name)
    service_token_id = None

    if service_token:
        print(f"\n✓ Service Token already exists: {service_token['name']}")
        service_token_id = service_token["id"]
    else:
        print("\n--> Creating Service Token for API access...")
        token_result = cf.create_service_token(service_token_name)
        if token_result:
            service_token_id = token_result["id"]
            # Save token details
            token_file = Path(__file__).parent.parent / "service_token.json"
            with open(token_file, "w") as f:
                json.dump(token_result, f, indent=2)
            print(f"  (Saved credentials to {token_file})")
        else:
            print("  Warning: Could not create service token (permission error?)")
            print("  Continuing with email-only access...")

    # 3. Create/Update Policy
    policy_name = "Allow UniFi Gate Access"
    cf.create_or_update_policy(app_id, policy_name, email, service_token_id)

    # Done!
    print_header("Access Configuration Complete!")
    print(
        f"""
Your Cloudflare Access is configured:

  Application: {app_name}
  Domain: {hostname}
  Allowed Email: {email}

When you visit https://{hostname}, you'll be prompted to verify
your email address before accessing the application.

Next steps:
1. Start the tunnel: cloudflared tunnel run <your-tunnel-name>
2. Start the server: .venv/bin/python server.py
3. Visit: https://{hostname}
"""
    )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\nSetup cancelled.")
        sys.exit(1)
