"""Spec for unifi_cookie.UniFiCookieClient — cookie+CSRF auth against UniFi OS.

Contract:
    - login(username, password, otp): POSTs /api/auth/login; persists cookie + csrf.
    - get_cover(door_id) -> bytes | None: 2-step (topology4 → /proxy/access/<cover_path>).
    - heartbeat(): /api/users/self; rotates csrf if response carries X-Updated-CSRF-Token.
    - 401/403 on any call → state cleared, file removed.
    - restore_session() loads persisted state.

Network calls are mocked via requests.Session.request — no live HTTP.
Set-Cookie effects are simulated by side_effects that inject into the session jar.
Run: python -m pytest test_unifi_cookie.py -v
"""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from unifi_cookie import CookieAuthError, UniFiCookieClient


def _resp(status: int, *, content: bytes = b"", headers: dict | None = None, json_body=None):
    r = MagicMock()
    r.status_code = status
    r.content = content
    r.headers = headers or {}
    if json_body is not None:
        r.json.return_value = json_body
    return r


def _login_side_effect(client, cookie_value: str = "abc", csrf: str | None = "csrf-1"):
    """Simulate the server returning Set-Cookie by injecting into the session jar."""

    def _impl(method, url=None, **kwargs):
        client._session.cookies.set("TOKEN", cookie_value, domain=client.host, path="/")
        return _resp(200, headers={"X-CSRF-Token": csrf} if csrf else {})

    return _impl


@pytest.fixture
def tmp_state():
    with tempfile.TemporaryDirectory() as d:
        yield os.path.join(d, "cover_session.json")


@pytest.fixture
def client(tmp_state):
    return UniFiCookieClient(host="unvr.local", verify_ssl=False, state_file=tmp_state)


# ---- login ----------------------------------------------------------------


def test_login_persists_cookies_and_csrf(client, tmp_state):
    with patch.object(client._session, "request", side_effect=_login_side_effect(client)) as req:
        client.login("user", "pass", otp="123456")

    assert client.is_connected()
    assert req.called
    body = json.loads(req.call_args.kwargs["data"])
    assert body == {"username": "user", "password": "pass", "token": "123456", "rememberMe": True}

    saved = json.load(open(tmp_state))
    assert any(c["name"] == "TOKEN" and c["value"] == "abc" for c in saved["cookies"])
    assert saved["csrf"] == "csrf-1"
    assert "saved_at" in saved


def test_login_uses_x_updated_csrf_when_present(client):
    def _impl(method, url=None, **kwargs):
        client._session.cookies.set("TOKEN", "z", domain=client.host, path="/")
        return _resp(200, headers={"X-Updated-CSRF-Token": "fresh"})

    with patch.object(client._session, "request", side_effect=_impl):
        client.login("u", "p")
    assert client._csrf == "fresh"


def test_login_raises_on_401(client):
    with patch.object(client._session, "request", return_value=_resp(401)):
        with pytest.raises(CookieAuthError):
            client.login("u", "wrong")
    assert not client.is_connected()


def test_login_raises_on_499_mfa_required(client):
    """499 = MFA required (no TOTP). Treat as auth error."""
    with patch.object(client._session, "request", return_value=_resp(499)):
        with pytest.raises(CookieAuthError):
            client.login("u", "p")


def test_login_fetches_csrf_separately_when_login_omits_it(client):
    """Some firmwares don't include CSRF on the login response — must follow up with /api/users/self."""

    def _impl(method, url=None, **kwargs):
        if "/auth/login" in (url or ""):
            client._session.cookies.set("TOKEN", "abc", domain=client.host, path="/")
            return _resp(200)
        # /api/users/self
        return _resp(200, headers={"X-CSRF-Token": "from-self"})

    with patch.object(client._session, "request", side_effect=_impl):
        client.login("u", "p")
    assert client._csrf == "from-self"


def test_login_clears_stale_cookies_before_authenticating(client):
    """A previous TOKEN cookie must not leak into a fresh login attempt."""
    client._session.cookies.set("TOKEN", "stale", domain=client.host, path="/")
    with patch.object(client._session, "request", side_effect=_login_side_effect(client, cookie_value="new")):
        client.login("u", "p")
    # Only the new TOKEN should be in the jar
    tokens = [c.value for c in client._session.cookies if c.name == "TOKEN"]
    assert tokens == ["new"]


# ---- get_cover ------------------------------------------------------------


_TOPOLOGY_FIXTURE = {
    "data": {
        "floors": [
            {
                "doors": [
                    {
                        "unique_id": "door-1",
                        "extras": {"door_cover": "/location_cover/door-1_111.jpg"},
                    },
                    {
                        "unique_id": "door-2",
                        "extras": {"door_cover": None},  # no cover uploaded
                    },
                ]
            }
        ]
    }
}


def _logged_in(client, csrf: str = "csrf-1"):
    client._session.cookies.set("TOKEN", "abc", domain=client.host, path="/")
    client._csrf = csrf
    client._saved_at = 100


def test_has_cover_returns_true_for_known_door(client):
    _logged_in(client)
    topo_resp = _resp(200, content=json.dumps(_TOPOLOGY_FIXTURE).encode(), json_body=_TOPOLOGY_FIXTURE)
    with patch.object(client._session, "request", return_value=topo_resp):
        assert client.has_cover("door-1") is True


def test_has_cover_returns_false_when_door_unknown(client):
    _logged_in(client)
    topo_resp = _resp(200, content=json.dumps(_TOPOLOGY_FIXTURE).encode(), json_body=_TOPOLOGY_FIXTURE)
    with patch.object(client._session, "request", return_value=topo_resp):
        assert client.has_cover("unknown-door") is False


def test_has_cover_returns_false_when_not_connected(client):
    assert client.has_cover("door-1") is False


def test_has_cover_returns_false_on_auth_error(client):
    _logged_in(client)
    with patch.object(client._session, "request", return_value=_resp(401)):
        assert client.has_cover("door-1") is False


def test_get_cover_reuses_topology_within_ttl(client):
    """Two cover fetches in quick succession should hit topology only once."""
    _logged_in(client)
    topo_resp = _resp(200, content=json.dumps(_TOPOLOGY_FIXTURE).encode(), json_body=_TOPOLOGY_FIXTURE)
    img_resp = _resp(200, content=b"jpeg1")
    img_resp2 = _resp(200, content=b"jpeg2")
    with patch.object(client._session, "request", side_effect=[topo_resp, img_resp, img_resp2]) as req:
        client.get_cover("door-1")
        client._cover_bytes_cache.clear()  # bust bytes cache only
        client.get_cover("door-1")
    paths = [c.kwargs["url"] for c in req.call_args_list]
    assert sum(1 for p in paths if "topology4" in p) == 1
    assert sum(1 for p in paths if "location_cover" in p) == 2


def test_get_cover_serves_cached_bytes_within_ttl(client):
    _logged_in(client)
    topo_resp = _resp(200, content=json.dumps(_TOPOLOGY_FIXTURE).encode(), json_body=_TOPOLOGY_FIXTURE)
    img_resp = _resp(200, content=b"jpeg1")
    with patch.object(client._session, "request", side_effect=[topo_resp, img_resp]) as req:
        a = client.get_cover("door-1")
        b = client.get_cover("door-1")
    assert a == b == b"jpeg1"
    assert req.call_count == 2


def test_get_cover_busts_bytes_cache_when_path_changes(client):
    """A replaced cover changes the timestamp in the filename — must bust bytes cache."""
    _logged_in(client)
    fixture_v1 = json.loads(json.dumps(_TOPOLOGY_FIXTURE))
    fixture_v2 = json.loads(json.dumps(_TOPOLOGY_FIXTURE))
    fixture_v2["data"]["floors"][0]["doors"][0]["extras"]["door_cover"] = "/location_cover/door-1_222.jpg"

    topo_v1 = _resp(200, content=json.dumps(fixture_v1).encode(), json_body=fixture_v1)
    topo_v2 = _resp(200, content=json.dumps(fixture_v2).encode(), json_body=fixture_v2)
    img1 = _resp(200, content=b"old")
    img2 = _resp(200, content=b"new")

    with patch.object(client._session, "request", side_effect=[topo_v1, img1, topo_v2, img2]):
        first = client.get_cover("door-1")
        client._cover_map_fetched_at = 0  # force topology refresh
        second = client.get_cover("door-1")
    assert first == b"old"
    assert second == b"new"


def test_get_cover_two_step_fetch(client):
    _logged_in(client)
    topo_resp = _resp(200, content=json.dumps(_TOPOLOGY_FIXTURE).encode(), json_body=_TOPOLOGY_FIXTURE)
    img_resp = _resp(200, content=b"\xff\xd8\xff jpeg-bytes")
    with patch.object(client._session, "request", side_effect=[topo_resp, img_resp]) as req:
        out = client.get_cover("door-1")
    assert out == b"\xff\xd8\xff jpeg-bytes"
    second_url = req.call_args_list[1].kwargs["url"]
    assert second_url.endswith("/proxy/access/location_cover/door-1_111.jpg")


def test_get_cover_returns_none_when_door_unknown(client):
    _logged_in(client)
    topo_resp = _resp(200, content=json.dumps(_TOPOLOGY_FIXTURE).encode(), json_body=_TOPOLOGY_FIXTURE)
    with patch.object(client._session, "request", return_value=topo_resp):
        assert client.get_cover("missing-door") is None


def test_get_cover_returns_none_when_cover_field_null(client):
    """door-2's extras.door_cover is null — no uploaded cover."""
    _logged_in(client)
    topo_resp = _resp(200, content=json.dumps(_TOPOLOGY_FIXTURE).encode(), json_body=_TOPOLOGY_FIXTURE)
    with patch.object(client._session, "request", return_value=topo_resp):
        assert client.get_cover("door-2") is None


def test_get_cover_returns_none_when_not_logged_in(client):
    assert client.get_cover("door-1") is None


def test_get_cover_clears_session_on_401(client, tmp_state):
    _logged_in(client)
    client._persist()
    assert os.path.exists(tmp_state)
    with patch.object(client._session, "request", return_value=_resp(401)):
        with pytest.raises(CookieAuthError):
            client.get_cover("door-1")
    assert not client.is_connected()
    assert not os.path.exists(tmp_state)


def test_bytes_cache_evicts_oldest_past_max(client):
    """LRU: once we exceed the cap, the oldest entry is dropped."""
    _logged_in(client)
    from unifi_cookie import _COVER_BYTES_MAX_ENTRIES

    for i in range(_COVER_BYTES_MAX_ENTRIES + 5):
        client._bytes_cache_put(f"door-{i}", f"/p/{i}.jpg", b"x")
    assert len(client._cover_bytes_cache) == _COVER_BYTES_MAX_ENTRIES
    # The first few entries are gone
    assert "door-0" not in client._cover_bytes_cache
    assert f"door-{_COVER_BYTES_MAX_ENTRIES + 4}" in client._cover_bytes_cache


# ---- heartbeat ------------------------------------------------------------


def test_heartbeat_rotates_csrf_on_x_updated(client):
    _logged_in(client)
    resp = _resp(200, headers={"X-Updated-CSRF-Token": "rotated"})
    with patch.object(client._session, "request", return_value=resp):
        result = client.heartbeat()
    assert client._csrf == "rotated"
    assert result["ok"] is True
    assert result["status"] == 200
    assert result["csrf_rotated"] is True


def test_heartbeat_does_not_count_unchanged_csrf_as_rotated(client):
    _logged_in(client)  # csrf is "csrf-1"
    resp = _resp(200, headers={"X-CSRF-Token": "csrf-1"})
    with patch.object(client._session, "request", return_value=resp):
        result = client.heartbeat()
    assert result["csrf_rotated"] is False
    assert result["ok"] is True


def test_heartbeat_clears_session_on_401(client, tmp_state):
    _logged_in(client)
    client._persist()
    with patch.object(client._session, "request", return_value=_resp(401)):
        result = client.heartbeat()
    assert result["ok"] is False
    assert "unauthorized" in result["error"]
    assert not client.is_connected()
    assert not os.path.exists(tmp_state)


def test_heartbeat_noop_when_not_logged_in(client):
    with patch.object(client._session, "request") as req:
        result = client.heartbeat()
    req.assert_not_called()
    assert result["ok"] is False
    assert result["error"] == "not_connected"


def test_heartbeat_increments_csrf_rotations_counter(client):
    _logged_in(client)
    r1 = _resp(200, headers={"X-Updated-CSRF-Token": "rot-1"})
    r2 = _resp(200, headers={"X-Updated-CSRF-Token": "rot-2"})
    with patch.object(client._session, "request", side_effect=[r1, r2]):
        client.heartbeat()
        client.heartbeat()
    assert client._csrf_rotations == 2


def test_status_reports_heartbeat_metadata(client):
    _logged_in(client)
    resp = _resp(200, headers={"X-Updated-CSRF-Token": "rot"})
    with patch.object(client._session, "request", return_value=resp):
        client.heartbeat()
    s = client.status()
    assert s["connected"] is True
    assert s["lastHeartbeatOk"] is True
    assert s["lastHeartbeatAt"] is not None
    assert s["csrfRotations"] == 1


# ---- restore_session ------------------------------------------------------


def test_restore_session_loads_from_disk(client, tmp_state):
    with open(tmp_state, "w") as f:
        json.dump(
            {
                "cookies": [{"name": "TOKEN", "value": "stored", "domain": client.host, "path": "/"}],
                "csrf": "csrf-stored",
                "saved_at": 500.0,
            },
            f,
        )
    fresh = UniFiCookieClient(host="unvr.local", state_file=tmp_state)
    assert fresh.restore_session() is True
    assert fresh.is_connected()
    assert fresh._csrf == "csrf-stored"


def test_restore_session_accepts_legacy_string_cookie(tmp_state):
    """Back-compat with the v1 schema that stored {cookie: 'TOKEN=abc', ...}."""
    with open(tmp_state, "w") as f:
        json.dump({"cookie": "TOKEN=legacy", "csrf": "csrf-legacy", "saved_at": 500.0}, f)
    fresh = UniFiCookieClient(host="unvr.local", state_file=tmp_state)
    assert fresh.restore_session() is True
    assert fresh.is_connected()


def test_restore_session_returns_false_when_missing(tmp_state):
    fresh = UniFiCookieClient(host="unvr.local", state_file=tmp_state + ".nope")
    assert fresh.restore_session() is False
    assert not fresh.is_connected()


def test_restore_session_returns_false_on_corrupt_file(tmp_state):
    with open(tmp_state, "w") as f:
        f.write("{not json")
    fresh = UniFiCookieClient(host="unvr.local", state_file=tmp_state)
    assert fresh.restore_session() is False


# ---- status reporting ----------------------------------------------------


def test_cookie_age_seconds_zero_when_not_connected(client):
    assert client.cookie_age_seconds() == 0


def test_cookie_age_seconds_reflects_time_since_login(client):
    """Login at t=1000, read age at t=1075 → 75s."""
    with patch("unifi_cookie.time.time", side_effect=[1000.0, 1075.0]):
        with patch.object(client._session, "request", side_effect=_login_side_effect(client)):
            client.login("u", "p")
        assert client.cookie_age_seconds() == 75


# ---- file perms ----------------------------------------------------------


def test_persist_writes_file_with_mode_0600(client, tmp_state):
    _logged_in(client)
    client._persist()
    mode = os.stat(tmp_state).st_mode & 0o777
    assert mode == 0o600
