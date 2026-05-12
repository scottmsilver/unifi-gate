"""Cookie + CSRF auth against the UniFi OS controller.

The Developer API token (bearer auth on port 12445) can't fetch static door
cover images. Those live behind the UniFi OS cookie session (port 443), the
same auth the web UI uses. This module owns that session: login, persist,
heartbeat, and the two-step cover fetch.

Used optionally as a sidecar to UniFiAccess (Developer API). If the session
hasn't been established, get_cover() simply returns None and the caller falls
through to other image sources.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Optional

import requests

logger = logging.getLogger("unifi_cookie")

_LOGIN_PATH = "/api/auth/login"
_SELF_PATH = "/api/users/self"
_TOPOLOGY_PATH = "/proxy/access/api/v2/devices/topology4"
_COVER_PROXY_PREFIX = "/proxy/access"
_REQUEST_TIMEOUT = 8  # seconds; UniFi can be slow under load
_COVER_MAP_TTL = 60.0  # refresh door→cover-path map at most this often
_COVER_BYTES_TTL = 300.0  # cache JPEG bytes for 5 min; cover replacement also busts via path change
_COVER_BYTES_MAX_ENTRIES = 32  # cap the bytes cache; LRU eviction


class CookieAuthError(Exception):
    """Raised on 401/403/499 from the controller."""


class UniFiCookieClient:
    def __init__(
        self,
        host: str,
        *,
        verify_ssl: bool = False,
        state_file: str = "cover_session.json",
    ):
        self.host = host
        self.verify_ssl = verify_ssl
        self.state_file = state_file
        # requests.Session manages its own cookie jar; we let it handle Set-Cookie
        # parsing rather than splitting headers ourselves.
        self._session = requests.Session()
        self._csrf: Optional[str] = None
        self._saved_at: Optional[float] = None
        self._last_heartbeat_at: Optional[float] = None
        self._last_heartbeat_ok: Optional[bool] = None
        self._csrf_rotations: int = 0
        self._cover_map_cache: Optional[dict[str, str]] = None
        self._cover_map_fetched_at: float = 0.0
        # LRU cache; insertion order = recency
        self._cover_bytes_cache: "OrderedDict[str, tuple[str, float, bytes]]" = OrderedDict()
        # Reentrant so login → _persist → other helpers don't deadlock.
        self._lock = threading.RLock()

    # ---- state ----

    def is_connected(self) -> bool:
        return any(c.name == "TOKEN" for c in self._session.cookies)

    def cookie_age_seconds(self) -> int:
        if self._saved_at is None:
            return 0
        return int(time.time() - self._saved_at)

    def _cookie_jar_dump(self) -> list[dict]:
        """Serialize the session's cookie jar to a JSON-friendly list."""
        return [
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
            }
            for c in self._session.cookies
        ]

    def _cookie_jar_load(self, entries: list[dict]) -> None:
        self._session.cookies.clear()
        for e in entries:
            self._session.cookies.set(
                e["name"], e["value"], domain=e.get("domain") or self.host, path=e.get("path") or "/"
            )

    def _persist(self) -> None:
        payload = {"cookies": self._cookie_jar_dump(), "csrf": self._csrf, "saved_at": self._saved_at}
        # Create with restrictive perms from the start; no umask race.
        fd = os.open(self.state_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)

    def _clear(self) -> None:
        self._session.cookies.clear()
        self._csrf = None
        self._saved_at = None
        try:
            os.remove(self.state_file)
        except FileNotFoundError:
            pass

    def restore_session(self) -> bool:
        try:
            with open(self.state_file) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return False
        with self._lock:
            entries = data.get("cookies")
            if entries:
                self._cookie_jar_load(entries)
            else:
                # Backward-compat with the v1 schema that stored a single "TOKEN=value" string.
                legacy = data.get("cookie")
                if not legacy or "=" not in legacy:
                    return False
                name, _, value = legacy.partition("=")
                self._session.cookies.set(name.strip(), value.strip(), domain=self.host, path="/")
            self._csrf = data.get("csrf")
            self._saved_at = data.get("saved_at") or time.time()
        return self.is_connected()

    # ---- HTTP plumbing ----

    def _headers(self, *, with_csrf: bool = True) -> dict:
        # requests.Session attaches cookies automatically; we only need to
        # supply the CSRF header explicitly.
        return {"X-CSRF-Token": self._csrf} if with_csrf and self._csrf else {}

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"https://{self.host}{path}"
        kwargs.setdefault("timeout", _REQUEST_TIMEOUT)
        kwargs.setdefault("verify", self.verify_ssl)
        return self._session.request(method, url=url, **kwargs)

    @staticmethod
    def _extract_csrf(resp: requests.Response) -> Optional[str]:
        return resp.headers.get("X-Updated-CSRF-Token") or resp.headers.get("X-CSRF-Token")

    def _check_auth(self, resp: requests.Response) -> None:
        if resp.status_code in (401, 403, 499):
            self._clear()
            raise CookieAuthError(f"unauthorized: HTTP {resp.status_code}")

    # ---- login ----

    def login(self, username: str, password: str, otp: Optional[str] = None) -> None:
        body = {"username": username, "password": password, "token": otp or "", "rememberMe": True}
        with self._lock:
            # Start from a fresh cookie jar so an old TOKEN doesn't shadow the new one.
            self._session.cookies.clear()
            resp = self._request(
                "POST",
                _LOGIN_PATH,
                data=json.dumps(body),
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code >= 400:
                raise CookieAuthError(f"login failed: HTTP {resp.status_code}")
            if not self.is_connected():
                raise CookieAuthError("login succeeded but no session cookie returned")

            self._csrf = self._extract_csrf(resp)
            self._saved_at = time.time()

            # Some firmwares omit CSRF on the login response — fetch one now via /users/self.
            if not self._csrf:
                r2 = self._request("GET", _SELF_PATH, headers=self._headers(with_csrf=False))
                self._check_auth(r2)
                self._csrf = self._extract_csrf(r2)

            self._persist()
        logger.info("UniFi cookie session established for %s", self.host)

    # ---- heartbeat ----

    def heartbeat(self) -> dict:
        """Hit /api/users/self to slide the cookie's TTL and rotate CSRF.
        Swallows auth errors after clearing state. Returns a diagnostic dict
        so callers (and admins) can validate the session is being kept alive.
        """
        with self._lock:
            self._last_heartbeat_at = time.time()
            result = {"ok": False, "status": None, "csrf_rotated": False, "error": None}

            if not self.is_connected():
                result["error"] = "not_connected"
                self._last_heartbeat_ok = False
                return result
            try:
                resp = self._request("GET", _SELF_PATH, headers=self._headers())
                result["status"] = resp.status_code
                self._check_auth(resp)
                updated = self._extract_csrf(resp)
                if updated and updated != self._csrf:
                    self._csrf = updated
                    self._csrf_rotations += 1
                    result["csrf_rotated"] = True
                    self._persist()
                result["ok"] = True
                self._last_heartbeat_ok = True
                logger.info(
                    "cover_session heartbeat ok (status=%s, csrf_rotated=%s)",
                    resp.status_code,
                    result["csrf_rotated"],
                )
            except CookieAuthError as e:
                result["error"] = str(e)
                self._last_heartbeat_ok = False
                logger.warning("cover_session heartbeat failed (session cleared): %s", e)
            except requests.RequestException as e:
                result["error"] = f"network: {e}"
                self._last_heartbeat_ok = False
                logger.warning("cover_session heartbeat network error: %s", e)
            return result

    def status(self) -> dict:
        """Snapshot of session health for the admin UI."""
        return {
            "connected": self.is_connected(),
            "cookieAgeSeconds": self.cookie_age_seconds(),
            "lastHeartbeatAt": self._last_heartbeat_at,
            "lastHeartbeatOk": self._last_heartbeat_ok,
            "csrfRotations": self._csrf_rotations,
        }

    # ---- cover ----

    def _fetch_topology(self) -> dict[str, str]:
        """door_id -> cover relative path. Caller must be logged in."""
        resp = self._request("GET", _TOPOLOGY_PATH, headers=self._headers())
        self._check_auth(resp)
        if resp.status_code >= 400:
            raise CookieAuthError(f"topology fetch failed: HTTP {resp.status_code}")
        # data: <dict with floors[].doors[]> for the firmware we target.
        data = resp.json().get("data") or {}
        out: dict[str, str] = {}
        for floor in data.get("floors", []) or []:
            for door in floor.get("doors", []) or []:
                cover = (door.get("extras") or {}).get("door_cover")
                if cover:
                    out[door.get("unique_id")] = cover
        return out

    def has_cover(self, door_id: str) -> bool:
        """Cheap check: does the controller report a cover for this door?
        Uses the cached cover-map (60s TTL); does not fetch image bytes."""
        if not self.is_connected():
            return False
        try:
            return door_id in self._get_cover_map()
        except (CookieAuthError, requests.RequestException):
            return False

    def _get_cover_map(self) -> dict[str, str]:
        """door_id -> cover relative path, cached for _COVER_MAP_TTL seconds."""
        with self._lock:
            now = time.time()
            if self._cover_map_cache is not None and now - self._cover_map_fetched_at < _COVER_MAP_TTL:
                return self._cover_map_cache
            fresh = self._fetch_topology()
            self._cover_map_cache = fresh
            self._cover_map_fetched_at = now
            return fresh

    def _bytes_cache_get(self, door_id: str, rel: str) -> Optional[bytes]:
        cached = self._cover_bytes_cache.get(door_id)
        if cached is None:
            return None
        cached_path, cached_at, cached_bytes = cached
        if cached_path == rel and time.time() - cached_at < _COVER_BYTES_TTL:
            self._cover_bytes_cache.move_to_end(door_id)  # LRU touch
            return cached_bytes
        return None

    def _bytes_cache_put(self, door_id: str, rel: str, content: bytes) -> None:
        self._cover_bytes_cache[door_id] = (rel, time.time(), content)
        self._cover_bytes_cache.move_to_end(door_id)
        while len(self._cover_bytes_cache) > _COVER_BYTES_MAX_ENTRIES:
            self._cover_bytes_cache.popitem(last=False)  # evict oldest

    def get_cover(self, door_id: str) -> Optional[bytes]:
        """Two-step: topology → cover relative path → image bytes. Both layers cached."""
        if not self.is_connected():
            return None
        cover_map = self._get_cover_map()
        rel = cover_map.get(door_id)
        if not rel:
            return None

        with self._lock:
            hit = self._bytes_cache_get(door_id, rel)
            if hit is not None:
                return hit

        resp = self._request("GET", _COVER_PROXY_PREFIX + rel, headers=self._headers())
        self._check_auth(resp)
        if resp.status_code == 200 and resp.content:
            with self._lock:
                self._bytes_cache_put(door_id, rel, resp.content)
            return resp.content
        return None
