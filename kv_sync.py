"""
Cloudflare KV Sync - Generic KV client and user sync functions.

This module separates concerns:
- CloudflareKV: Generic key-value operations (no domain knowledge)
- sync_approved_users_to_kv(): User-specific sync logic
"""

import os
from typing import Optional

try:
    import requests

    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


class CloudflareKV:
    """Generic Cloudflare KV client - just key-value operations."""

    def __init__(
        self,
        account_id: Optional[str] = None,
        api_token: Optional[str] = None,
        namespace_id: Optional[str] = None,
    ):
        self.account_id = account_id or os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        self.api_token = api_token or os.environ.get("CLOUDFLARE_API_TOKEN")
        self.namespace_id = namespace_id or os.environ.get("CLOUDFLARE_KV_NAMESPACE_ID")

    def is_configured(self) -> bool:
        """Check if Cloudflare KV is properly configured."""
        return bool(self.account_id and self.api_token and self.namespace_id)

    def get_missing_config(self) -> list[str]:
        """Return list of missing configuration variables."""
        missing = []
        if not self.account_id:
            missing.append("CLOUDFLARE_ACCOUNT_ID")
        if not self.api_token:
            missing.append("CLOUDFLARE_API_TOKEN")
        if not self.namespace_id:
            missing.append("CLOUDFLARE_KV_NAMESPACE_ID")
        return missing

    def _base_url(self) -> str:
        return (
            f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/storage/kv/namespaces/{self.namespace_id}"
        )

    def _headers(self, content_type: bool = True) -> dict:
        headers = {"Authorization": f"Bearer {self.api_token}"}
        if content_type:
            headers["Content-Type"] = "application/json"
        return headers

    def write_bulk(self, items: list[tuple[str, str]]) -> tuple[bool, str]:
        """
        Write multiple key-value pairs to KV.

        Args:
            items: List of (key, value) tuples to write

        Returns:
            (success, message) tuple
        """
        if not REQUESTS_AVAILABLE:
            return False, "requests library not installed. Run: pip install requests"

        if not self.is_configured():
            return False, f"Missing environment variables: {', '.join(self.get_missing_config())}"

        bulk_data = [{"key": key, "value": value} for key, value in items]

        try:
            response = requests.put(
                f"{self._base_url()}/bulk",
                headers=self._headers(),
                json=bulk_data,
            )
            response.raise_for_status()

            result = response.json()
            if result.get("success"):
                return True, f"Wrote {len(items)} keys to Cloudflare KV"
            else:
                errors = result.get("errors", [])
                return False, f"Cloudflare API error: {errors}"

        except requests.exceptions.RequestException as e:
            return False, f"Request failed: {e}"

    def read(self, key: str) -> tuple[bool, Optional[str]]:
        """
        Read a single key from KV.

        Returns:
            (success, value) tuple. value is None if key doesn't exist.
        """
        if not REQUESTS_AVAILABLE:
            return False, "requests library not installed"

        if not self.is_configured():
            return False, "Cloudflare KV not configured"

        try:
            response = requests.get(
                f"{self._base_url()}/values/{key}",
                headers=self._headers(content_type=False),
            )

            if response.status_code == 404:
                return True, None

            response.raise_for_status()
            return True, response.text

        except requests.exceptions.RequestException as e:
            return False, f"Request failed: {e}"


# ============== User-specific sync functions ==============
# These functions know the semantic meaning of what we're storing


def sync_approved_users_to_kv(emails: list[str], kv: Optional[CloudflareKV] = None) -> tuple[bool, str]:
    """
    Sync approved user emails to Cloudflare KV.

    Storage format:
    - Each email stored as key with value "1" for fast existence checks
    - Special key "__approved_users__" stores the full comma-separated list

    Args:
        emails: List of approved user emails
        kv: Optional CloudflareKV instance (creates one if not provided)

    Returns:
        (success, message) tuple
    """
    if kv is None:
        kv = CloudflareKV()

    if not kv.is_configured():
        return False, f"Missing environment variables: {', '.join(kv.get_missing_config())}"

    # Build items: each email as key, plus the full list
    items = [(email, "1") for email in emails]
    items.append(("__approved_users__", ",".join(emails)))

    success, message = kv.write_bulk(items)
    if success:
        return True, f"Synced {len(emails)} users to Cloudflare KV"
    return False, message


def get_approved_users_from_kv(
    kv: Optional[CloudflareKV] = None,
) -> tuple[bool, list[str] | str]:
    """
    Get approved user emails from Cloudflare KV.

    Returns:
        (success, emails_or_error) tuple
    """
    if kv is None:
        kv = CloudflareKV()

    if not kv.is_configured():
        return False, "Cloudflare KV not configured"

    success, value = kv.read("__approved_users__")
    if not success:
        return False, value

    if value is None:
        return True, []

    # Filter empty strings from split
    return True, [u for u in value.split(",") if u]
