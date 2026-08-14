"""Clean client for the UniFi Access Developer API (port 12445).

Replaces the legacy reverse-engineered cookie client (`unifi_native_api.py`),
which required 2FA on every session refresh and broke under systemd.

Surface kept small: only what the gate server actually needs — door listing,
unlock, hold (for-minutes / until-epoch / indefinitely / release), lock-state
inspection, device listing, thumbnails, console name, and a WebSocket event
stream.
"""

from __future__ import annotations

import json
import logging
import math
import ssl
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

import requests
import urllib3

logger = logging.getLogger(__name__)

DEFAULT_PORT = 12445
DEFAULT_TIMEOUT = 10.0

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UnifiAccessError(Exception):
    """Base for all client errors."""


class UnifiAccessAuthError(UnifiAccessError):
    """401 from the controller — token is missing, expired, or rejected."""


class UnifiAccessForbiddenError(UnifiAccessError):
    """403 from the controller — token lacks the required permission."""


class UnifiAccessNotFoundError(UnifiAccessError):
    """404 from the controller — endpoint or resource not found."""


class UnifiAccessAPIError(UnifiAccessError):
    """Other 4xx/5xx response, or 200 with a non-SUCCESS body code."""

    def __init__(self, message: str, status_code: int = 0, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class HoldType(str, Enum):
    NONE = ""
    CUSTOM = "custom"
    KEEP_UNLOCK = "keep_unlock"
    KEEP_LOCK = "keep_lock"


@dataclass
class Door:
    id: str
    name: str
    full_name: str
    # Tri-state on purpose. None means "the controller did not tell us", which
    # is different from False and must never be rendered as a definite state.
    locked: Optional[bool]
    open: Optional[bool]
    floor_id: Optional[str] = None
    bound_to_hub: bool = False
    cover_path: Optional[str] = None
    thumbnail_path: Optional[str] = None
    thumbnail_updated_at: Optional[int] = None


@dataclass
class Device:
    id: str
    name: str
    type: str
    location_id: str
    online: bool
    connected: bool = False
    managed: bool = False
    adopted: bool = False
    capabilities: list[str] = field(default_factory=list)


@dataclass
class HoldState:
    type: HoldType
    ended_time: Optional[int]

    @property
    def active(self) -> bool:
        return self.type != HoldType.NONE


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class UniFiAccess:
    """HTTP client for the UniFi Access Developer API."""

    def __init__(
        self,
        host: str,
        token: str,
        *,
        port: int = DEFAULT_PORT,
        verify_ssl: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.host = host
        self.port = port
        self.token = token
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.session.headers["Accept"] = "application/json"
        if not verify_ssl:
            self.session.verify = False
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    @property
    def base_url(self) -> str:
        return f"https://{self.host}:{self.port}"

    # ---- low-level ----

    def _request(self, method: str, path: str, json_body: Any = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(method, url, json=json_body, timeout=self.timeout)
        except requests.RequestException as e:
            raise UnifiAccessError(f"network error talking to {url}: {e}") from e

        if resp.status_code == 401:
            raise UnifiAccessAuthError(f"401 from {url} — token rejected")
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
                data = resp.json()
            except ValueError as e:
                raise UnifiAccessAPIError(f"invalid JSON from {url}: {e}") from e
            if isinstance(data, dict) and "code" in data and data["code"] != "SUCCESS":
                raise UnifiAccessAPIError(
                    f"{data.get('code')}: {data.get('msg', 'unknown error')}",
                    status_code=resp.status_code,
                    body=data,
                )
            return data
        return resp.content

    def healthcheck(self) -> bool:
        """Cheap liveness probe — confirms token works and controller is reachable."""
        try:
            self._request("GET", "/api/v1/developer/doors")
            return True
        except UnifiAccessError:
            return False

    # ---- doors ----

    def list_doors(self) -> list[Door]:
        data = self._request("GET", "/api/v1/developer/doors")
        return [_parse_door(d) for d in (data.get("data") or [])]

    def get_door(self, door_id: str) -> Door:
        data = self._request("GET", f"/api/v1/developer/doors/{door_id}")
        return _parse_door(data["data"])

    # ---- devices ----

    def list_devices(self) -> list[Device]:
        data = self._request("GET", "/api/v1/developer/devices")
        raw = data.get("data") or []
        # Doc shape is data: [[...]] — flatten one level if needed.
        if raw and isinstance(raw[0], list):
            raw = raw[0]
        return [_parse_device(d) for d in raw]

    # ---- unlock / hold ----

    def unlock(
        self,
        door_id: str,
        *,
        actor_id: Optional[str] = None,
        actor_name: Optional[str] = None,
        extra: Optional[dict] = None,
    ) -> None:
        """Momentary unlock — fires the relay once. Custom actor info shows up
        in system logs and webhook events."""
        body: dict = {}
        if actor_id is not None and actor_name is not None:
            body["actor_id"] = actor_id
            body["actor_name"] = actor_name
        elif actor_id is not None or actor_name is not None:
            raise ValueError("actor_id and actor_name must be provided together")
        if extra is not None:
            body["extra"] = extra
        self._request("PUT", f"/api/v1/developer/doors/{door_id}/unlock", json_body=body)

    def hold_for_minutes(self, door_id: str, minutes: int) -> None:
        if minutes < 1:
            raise ValueError("hold_for_minutes requires minutes >= 1")
        self._request(
            "PUT",
            f"/api/v1/developer/doors/{door_id}/lock_rule",
            json_body={"type": "custom", "interval": int(minutes)},
        )

    def hold_until(self, door_id: str, end_epoch: float) -> int:
        """Hold the door open until the given epoch time. Rounds up to the
        nearest minute; minimum 1. Returns the actual minute count sent."""
        minutes = max(1, math.ceil((end_epoch - time.time()) / 60))
        self.hold_for_minutes(door_id, minutes)
        return minutes

    def hold_indefinitely(self, door_id: str) -> None:
        self._request(
            "PUT",
            f"/api/v1/developer/doors/{door_id}/lock_rule",
            json_body={"type": "keep_unlock"},
        )

    def release_hold(self, door_id: str) -> None:
        """Cancel any active hold (custom, keep_unlock, or scheduled) and lock now."""
        self._request(
            "PUT",
            f"/api/v1/developer/doors/{door_id}/lock_rule",
            json_body={"type": "lock_now"},
        )

    def keep_locked(self, door_id: str) -> None:
        """Force the door into a locked state, ignoring schedules until released."""
        self._request(
            "PUT",
            f"/api/v1/developer/doors/{door_id}/lock_rule",
            json_body={"type": "keep_lock"},
        )

    def get_hold_state(self, door_id: str) -> HoldState:
        data = self._request("GET", f"/api/v1/developer/doors/{door_id}/lock_rule")
        rule = data.get("data") or {}
        rtype = HoldType(rule.get("type") or "")
        ended = rule.get("ended_time")
        if ended in (0, None):
            ended = None
        return HoldState(type=rtype, ended_time=ended)

    # ---- thumbnails ----

    def fetch_thumbnail(self, path: str) -> bytes:
        """Fetch a door thumbnail or cover by its relative path."""
        if not path.startswith("/"):
            path = "/" + path
        return self._request("GET", f"/api/v1/developer/system/static{path}")

    def fetch_door_thumbnail(self, door: Door) -> Optional[bytes]:
        """Fetch the current live preview for a door. Returns None when the
        door has no live thumbnail (e.g. doors with no bound camera/intercom).

        NOTE: door.cover_path is intentionally NOT used as a fallback — the
        static `/location_cover/<id>.jpg` resource lives behind UniFi OS
        session-cookie auth on port 443, not the Developer API. It cannot be
        fetched with a bearer token.
        """
        if not door.thumbnail_path:
            return None
        return self.fetch_thumbnail(door.thumbnail_path)


# ---------------------------------------------------------------------------
# Console name (unauthenticated, port 443)
# ---------------------------------------------------------------------------


def fetch_console_name(
    host: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    verify_ssl: bool = False,
) -> Optional[str]:
    """Read the controller's console name from /api/system. Unauthenticated.
    Returns None on any failure — the name is cosmetic."""
    try:
        resp = requests.get(f"https://{host}/api/system", timeout=timeout, verify=verify_ssl)
        if resp.ok:
            return resp.json().get("name")
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _tri_state(value, when_true: str, when_false: str) -> Optional[bool]:
    """Map a controller enum to True/False, or None when it says neither.

    Written as an explicit three-way rather than `value == when_true`, because
    that idiom silently turns "field absent", "unknown" and "" into False. A
    door with no position sensor reports exactly those, and the old code
    rendered it as closed-and-unlocked with full confidence.
    """
    if value == when_true:
        return True
    if value == when_false:
        return False
    return None


def _parse_door(d: dict) -> Door:
    extras = d.get("extras") or {}
    return Door(
        id=d["id"],
        name=d["name"],
        full_name=d.get("full_name", d["name"]),
        locked=_tri_state(d.get("door_lock_relay_status"), "lock", "unlock"),
        open=_tri_state(d.get("door_position_status"), "open", "close"),
        floor_id=d.get("floor_id"),
        bound_to_hub=bool(d.get("is_bind_hub")),
        cover_path=extras.get("door_cover"),
        thumbnail_path=extras.get("door_thumbnail"),
        thumbnail_updated_at=extras.get("door_thumbnail_last_update"),
    )


def _parse_device(d: dict) -> Device:
    return Device(
        id=d["id"],
        name=d.get("name", ""),
        type=d.get("type", ""),
        location_id=d.get("location_id", ""),
        online=bool(d.get("is_online")),
        connected=bool(d.get("is_connected")),
        managed=bool(d.get("is_managed")),
        adopted=bool(d.get("is_adopted")),
        capabilities=list(d.get("capabilities") or []),
    )


# ---------------------------------------------------------------------------
# Event stream
# ---------------------------------------------------------------------------


class AccessEventStream:
    """Background WebSocket listener for the controller's notification feed.

    Use as a context manager or call start()/stop() directly. The on_event
    callback receives a parsed dict for each message; it runs on the stream's
    own thread, so make it cheap or hand off to a queue.

    The stream auto-reconnects with exponential backoff capped at 30s.
    """

    URL_PATH = "/api/v1/developer/devices/notifications"

    def __init__(self, client: UniFiAccess, on_event: Callable[[dict], None]):
        self.client = client
        self._on_event = on_event
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="unifi-access-ws")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=2)
        self._connected = False

    def __enter__(self) -> "AccessEventStream":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def _run(self) -> None:
        import websocket  # websocket-client; declared in requirements.txt

        url = f"wss://{self.client.host}:{self.client.port}{self.URL_PATH}"
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._ws = websocket.create_connection(
                    url,
                    header=[f"Authorization: Bearer {self.client.token}"],
                    sslopt={"cert_reqs": ssl.CERT_NONE} if not self.client.verify_ssl else None,
                    # The `timeout` here also acts as the per-recv socket timeout.
                    # Setting it to 30s gives us a heartbeat opportunity below if
                    # the controller sits silent — without this, a NAT/firewall
                    # idle-killing the TCP connection would only be detected after
                    # the OS-level keepalive (~2h on Linux default).
                    timeout=30,
                )
                self._connected = True
                backoff = 1.0
                logger.info("UniFi Access WebSocket connected")
                while not self._stop.is_set():
                    try:
                        msg = self._ws.recv()
                    except websocket.WebSocketTimeoutException:
                        # Idle period — send a ping to verify the peer is alive.
                        # If the ping itself fails, we break out and reconnect.
                        try:
                            self._ws.ping()
                            continue
                        except Exception as e:
                            logger.warning("UniFi Access WS heartbeat ping failed: %s", e)
                            break
                    except Exception as e:
                        logger.warning("UniFi Access WS recv error: %s", e)
                        break
                    if not msg:
                        continue
                    # The controller sends a "Hello..." text frame on connect
                    # and may emit other plain-text heartbeats; skip non-JSON
                    # and non-dict payloads silently.
                    if isinstance(msg, (bytes, bytearray)):
                        try:
                            msg = msg.decode("utf-8", errors="replace")
                        except Exception:
                            continue
                    if msg.startswith("Hello"):
                        continue
                    try:
                        event = json.loads(msg)
                    except ValueError:
                        logger.debug("non-JSON WS message: %r", msg[:80])
                        continue
                    if not isinstance(event, dict):
                        logger.debug("non-dict WS payload: %s", type(event).__name__)
                        continue
                    try:
                        self._on_event(event)
                    except Exception:
                        logger.exception("event handler raised")
                self._connected = False
            except Exception as e:
                logger.warning("UniFi Access WS connect failed: %s", e)
                self._connected = False
            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
