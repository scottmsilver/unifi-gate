"""Minimal UniFi Protect Integration API client.

Used by the gate server to fetch live camera snapshots (e.g., a higher-quality
cover image for doors that have a Protect camera bound to them, like the gate
intercom). The Protect Integration API uses a separate API key (X-API-KEY
header, not Authorization: Bearer) and lives on port 443 — completely separate
auth surface from UniFi Access.

Door → Camera mapping is the integration layer's job, not this client's. The
typical trick: an Access UA-Intercom device's `id` field is its MAC address,
and Protect cameras expose a `mac` field, so doors can be linked to cameras
case-insensitively via MAC.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import requests
import urllib3
from unifi_access import (
    UnifiAccessAPIError,
    UnifiAccessAuthError,
    UnifiAccessError,
    UnifiAccessForbiddenError,
    UnifiAccessNotFoundError,
)

DEFAULT_PORT = 443
DEFAULT_TIMEOUT = 10.0


@dataclass
class Camera:
    id: str
    name: str
    mac: str  # uppercase, no separators (e.g. "28704E747634")
    state: str  # "CONNECTED", "DISCONNECTED", etc.
    model_key: str


class UniFiProtect:
    """HTTP client for the UniFi Protect Integration API."""

    BASE_PATH = "/proxy/protect/integration/v1"

    def __init__(
        self,
        host: str,
        api_key: str,
        *,
        port: int = DEFAULT_PORT,
        verify_ssl: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.host = host
        self.api_key = api_key
        self.port = port
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["X-API-KEY"] = api_key
        self.session.headers["Accept"] = "application/json"
        if not verify_ssl:
            self.session.verify = False
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    @property
    def base_url(self) -> str:
        return f"https://{self.host}:{self.port}{self.BASE_PATH}"

    def _request(self, method: str, path: str, params: Optional[dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(method, url, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise UnifiAccessError(f"network error talking to {url}: {e}") from e

        if resp.status_code == 401:
            raise UnifiAccessAuthError(f"401 from {url} — Protect API key rejected")
        if resp.status_code == 403:
            raise UnifiAccessForbiddenError(f"403 from {url}")
        if resp.status_code == 404:
            raise UnifiAccessNotFoundError(f"404 from {url}")
        if resp.status_code >= 400:
            raise UnifiAccessAPIError(
                f"{resp.status_code} from {url}: {resp.text[:200]}",
                status_code=resp.status_code,
                body=resp.text,
            )

        ctype = resp.headers.get("Content-Type", "")
        if "application/json" in ctype:
            try:
                return resp.json()
            except ValueError as e:
                raise UnifiAccessAPIError(f"invalid JSON from {url}: {e}") from e
        return resp.content

    def healthcheck(self) -> bool:
        try:
            self._request("GET", "/meta/info")
            return True
        except UnifiAccessError:
            return False

    # ---- cameras ----

    def list_cameras(self) -> list[Camera]:
        data = self._request("GET", "/cameras")
        if not isinstance(data, list):
            return []
        return [_parse_camera(c) for c in data]

    def fetch_camera_snapshot(
        self,
        camera_id: str,
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> bytes:
        """Fetch a current still snapshot from the camera. Returns raw JPEG bytes.
        width/height are passed as `w`/`h` query params — the controller may
        choose to honor or ignore them depending on camera and firmware."""
        params: dict[str, int] = {}
        if width is not None:
            params["w"] = int(width)
        if height is not None:
            params["h"] = int(height)
        return self._request("GET", f"/cameras/{camera_id}/snapshot", params=params or None)


# ---- parsers ----


def _parse_camera(c: dict) -> Camera:
    return Camera(
        id=c["id"],
        name=c.get("name", ""),
        mac=(c.get("mac") or "").upper(),
        state=c.get("state", ""),
        model_key=c.get("modelKey", ""),
    )


# ---- helpers ----


def cameras_by_mac(cameras: list[Camera]) -> dict[str, Camera]:
    """Index a camera list by lowercased MAC for fast lookup from an Access
    device's `id` (which is its MAC). Convenience for door→camera mapping."""
    return {c.mac.lower(): c for c in cameras}
