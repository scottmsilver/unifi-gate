"""
Live integration tests for the UniFi Access Developer API (port 12445).

These tests hit the real UNVR and prove the Developer API covers every native-cookie-API
capability the app currently depends on. Read-only tests are gated behind
RUN_LIVE_API_TESTS=1; tests that physically unlock the gate require
RUN_LIVE_API_TESTS_UNLOCK=1 as well.

Environment:
    RUN_LIVE_API_TESTS=1            enables read-only live tests
    RUN_LIVE_API_TESTS_UNLOCK=1     additionally enables tests that briefly
                                    unlock the physical gate (cleanup always
                                    sends lock_now in a finally block)
    LIVE_TEST_HOST                  hostname/IP of the UNVR (default: read from
                                    credentials.json next to this file, then
                                    fallback to 316-costello-security.316costello)
    LIVE_TEST_TOKEN                 bearer token (default: from credentials.json)
    LIVE_TEST_DOOR_ID               door UUID to exercise hold-open / unlock
                                    against (default: Gate)

Run examples:
    RUN_LIVE_API_TESTS=1 python -m pytest test_developer_api_live.py -v
    RUN_LIVE_API_TESTS=1 RUN_LIVE_API_TESTS_UNLOCK=1 \\
        python -m pytest test_developer_api_live.py -v
"""

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent
CREDS_PATH = REPO_ROOT / "credentials.json"

DEFAULT_HOST_FALLBACK = "316-costello-security.316costello"
DEFAULT_DOOR_ID = "824949ac-d2d1-4e07-88c1-e12ebec6f516"  # Gate

# ---- gating ---------------------------------------------------------------

live_enabled = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_API_TESTS") != "1",
    reason="set RUN_LIVE_API_TESTS=1 to enable live UNVR tests",
)

unlock_enabled = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_API_TESTS_UNLOCK") != "1",
    reason="set RUN_LIVE_API_TESTS_UNLOCK=1 to allow physically unlocking the gate",
)


# ---- helpers --------------------------------------------------------------


def _load_creds():
    if CREDS_PATH.exists():
        with open(CREDS_PATH) as f:
            return json.load(f)
    return {}


def _config():
    creds = _load_creds()
    host = os.environ.get("LIVE_TEST_HOST") or creds.get("host") or DEFAULT_HOST_FALLBACK
    # credentials.json may have a stale IP; allow override via env. If the file
    # value looks like the old IP, fall back to the FQDN.
    if host == "192.168.1.79":
        host = DEFAULT_HOST_FALLBACK
    token = os.environ.get("LIVE_TEST_TOKEN") or creds.get("token")
    door = os.environ.get("LIVE_TEST_DOOR_ID") or DEFAULT_DOOR_ID
    if not token:
        pytest.skip("no API token available (set LIVE_TEST_TOKEN or populate credentials.json)")
    return host, token, door


def _request(method, host, path, token, body=None, timeout=10):
    """Returns (status, parsed_body). parsed_body is the decoded JSON for
    application/json responses, otherwise raw bytes."""
    url = f"https://{host}:12445{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            payload = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype and payload:
                return resp.status, json.loads(payload.decode())
            return resp.status, payload if payload else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode())
        except Exception:
            return e.code, raw


def _force_lock_now(host, token, door_id):
    """Best-effort cleanup. Never raises."""
    try:
        _request("PUT", host, f"/api/v1/developer/doors/{door_id}/lock_rule", token, body={"type": "lock_now"})
    except Exception:
        pass


# ---- fixtures -------------------------------------------------------------


@pytest.fixture
def api():
    host, token, door = _config()
    return {"host": host, "token": token, "door": door}


@pytest.fixture
def cleanup_lock(api):
    """Yields, then sends lock_now no matter what happened in the test."""
    yield
    _force_lock_now(api["host"], api["token"], api["door"])


# ---- read-only tests ------------------------------------------------------


@live_enabled
def test_auth_and_list_doors(api):
    """Bearer token authenticates and /doors returns at least one door with
    the fields the app currently consumes from native get_doors()."""
    code, data = _request("GET", api["host"], "/api/v1/developer/doors", api["token"])
    assert code == 200, f"expected 200, got {code}: {data}"
    assert isinstance(data, dict) and data.get("code") == "SUCCESS"
    doors = data["data"]
    assert isinstance(doors, list) and doors, "expected at least one door"
    for d in doors:
        assert "id" in d
        assert "name" in d
        assert "door_lock_relay_status" in d  # lock/unlock — used by /status
        assert "door_position_status" in d  # open/close — used by /status


@live_enabled
def test_list_devices_returns_hardware_with_location(api):
    """GET /devices returns access hardware (hubs, readers) — the replacement for
    server.py:171 _make_request('GET', '/proxy/access/api/v2/devices').

    The runtime uses this to map a door back to its physical device for icon
    fallback. The Developer API surfaces this mapping via `location_id` on each
    device (rather than the old `extensions[].target_value`).

    Caveat encoded for the migrator: the response shape is `data: [[...]]`
    (extra nesting), and the per-device `images.xs` field is NOT included
    here — the cosmetic device-icon fallback at server.py:725-746 will need
    to come from the door thumbnail or be dropped.
    """
    code, data = _request("GET", api["host"], "/api/v1/developer/devices", api["token"])
    assert code == 200, f"expected 200, got {code}: {data}"
    assert isinstance(data, dict) and data.get("code") == "SUCCESS"

    raw = data["data"]
    # Documented shape is a nested array — flatten for assertions.
    devices = raw[0] if isinstance(raw, list) and raw and isinstance(raw[0], list) else raw
    assert isinstance(devices, list) and devices, f"expected at least one device, got {raw!r}"

    door_ids = set()
    code2, doors = _request("GET", api["host"], "/api/v1/developer/doors", api["token"])
    if code2 == 200:
        door_ids = {d["id"] for d in doors["data"]}

    for d in devices:
        assert "id" in d
        assert "name" in d
        assert "type" in d  # e.g. UGT, UA-Intercom
        assert "is_online" in d
        assert "location_id" in d, f"device {d.get('name')} missing location_id"

    # location_id may point to a door OR to a door-group id (hubs). At least
    # one device should map to a known door so the icon-fallback lookup works.
    if door_ids:
        assert any(
            d.get("location_id") in door_ids for d in devices
        ), "expected at least one device to map to a known door via location_id"


@live_enabled
def test_target_door_exists(api):
    """The door under test is in the list and reachable."""
    code, data = _request("GET", api["host"], "/api/v1/developer/doors", api["token"])
    assert code == 200
    ids = [d["id"] for d in data["data"]]
    assert api["door"] in ids, f"door {api['door']} not in {ids}"


@live_enabled
def test_get_lock_rule_idle(api):
    """Reading the lock_rule on an idle door returns the canonical empty shape."""
    code, data = _request("GET", api["host"], f"/api/v1/developer/doors/{api['door']}/lock_rule", api["token"])
    assert code == 200
    assert data["code"] == "SUCCESS"
    rule = data["data"]
    assert set(rule.keys()) >= {"type", "ended_time"}
    # Don't assert empty — a hold may legitimately be active.


@live_enabled
def test_door_thumbnail_url_present(api):
    """The /doors response exposes a thumbnail or cover path that we can use
    for the /door-image proxy replacement."""
    code, data = _request("GET", api["host"], "/api/v1/developer/doors", api["token"])
    assert code == 200
    door = next((d for d in data["data"] if d["id"] == api["door"]), None)
    assert door is not None
    extras = door.get("extras") or {}
    cover = extras.get("door_cover") or extras.get("door_thumbnail")
    assert cover, f"expected door_cover or door_thumbnail in extras, got {extras}"
    assert cover.startswith("/"), f"unexpected cover path: {cover}"


@live_enabled
def test_thumbnail_fetch_via_static_endpoint(api):
    """The system/static endpoint serves the door thumbnail with the bearer token.
    This is the Developer-API replacement for the proxy/access image fetch.

    NOTE: This test is xfail-tolerant — if the path format isn't what we expect,
    record the failure and move on so we can adjust before migrating /door-image.
    """
    code, data = _request("GET", api["host"], "/api/v1/developer/doors", api["token"])
    assert code == 200
    door = next((d for d in data["data"] if d["id"] == api["door"]), None)
    extras = (door or {}).get("extras") or {}
    cover = extras.get("door_thumbnail") or extras.get("door_cover")
    if not cover:
        pytest.skip("no thumbnail path on door — cover lives in events only?")

    # cover is e.g. "/preview/reader_...jpg" or "/location_cover/...jpg"
    static_path = f"/api/v1/developer/system/static{cover}"
    code2, payload2 = _request("GET", api["host"], static_path, api["token"])
    assert code2 == 200, f"thumbnail fetch returned {code2}: {payload2!r}"
    assert isinstance(payload2, (bytes, bytearray)) and len(payload2) > 100, "expected image bytes"
    # JPEG starts with FF D8 FF, PNG with 89 50 4E 47.
    head = bytes(payload2[:4])
    assert head[:3] == b"\xff\xd8\xff" or head == b"\x89PNG", f"unexpected image magic: {head!r}"


# ---- destructive (physical unlock) ----------------------------------------


@live_enabled
@unlock_enabled
def test_hold_open_custom_auto_expires(api, cleanup_lock):
    """PUT lock_rule {type:custom, interval:1} holds for ~60s then auto-expires.
    Replaces schedule_manager.inject_hold_open() for the
    /hold/today/<id> endpoint (where the duration is computed from end_time)."""
    host, token, door = api["host"], api["token"], api["door"]

    code, resp = _request(
        "PUT",
        host,
        f"/api/v1/developer/doors/{door}/lock_rule",
        token,
        body={"type": "custom", "interval": 1},
    )
    assert code == 200 and resp["code"] == "SUCCESS"

    # Immediate readback: should show custom + ended_time in the near future.
    code, rule = _request("GET", host, f"/api/v1/developer/doors/{door}/lock_rule", token)
    assert code == 200
    assert rule["data"]["type"] == "custom"
    ended = rule["data"]["ended_time"]
    now = int(time.time())
    assert 30 < ended - now < 120, f"ended_time {ended} not in expected window from now {now}"

    # Poll until auto-expiry — generous timeout.
    deadline = time.time() + 90
    final_type = None
    while time.time() < deadline:
        time.sleep(10)
        code, rule = _request("GET", host, f"/api/v1/developer/doors/{door}/lock_rule", token)
        assert code == 200
        final_type = rule["data"]["type"]
        if final_type in ("", None):
            return  # auto-expired ✓
    pytest.fail(f"hold did not auto-expire within 90s; final type={final_type!r}")


@live_enabled
@unlock_enabled
def test_hold_open_keep_unlock_then_lock_now(api, cleanup_lock):
    """keep_unlock holds indefinitely; lock_now clears it. Replaces
    /hold/forever/<id> + /hold/stop/<id>."""
    host, token, door = api["host"], api["token"], api["door"]

    code, resp = _request(
        "PUT",
        host,
        f"/api/v1/developer/doors/{door}/lock_rule",
        token,
        body={"type": "keep_unlock"},
    )
    assert code == 200 and resp["code"] == "SUCCESS"

    code, rule = _request("GET", host, f"/api/v1/developer/doors/{door}/lock_rule", token)
    assert code == 200
    assert rule["data"]["type"] == "keep_unlock", f"got {rule['data']}"

    # Hold open briefly to confirm sticky behavior.
    time.sleep(5)
    code, rule = _request("GET", host, f"/api/v1/developer/doors/{door}/lock_rule", token)
    assert rule["data"]["type"] == "keep_unlock"

    # Cancel.
    code, resp = _request(
        "PUT",
        host,
        f"/api/v1/developer/doors/{door}/lock_rule",
        token,
        body={"type": "lock_now"},
    )
    assert code == 200 and resp["code"] == "SUCCESS"

    code, rule = _request("GET", host, f"/api/v1/developer/doors/{door}/lock_rule", token)
    assert rule["data"]["type"] in ("", None)
    assert rule["data"]["ended_time"] in (0, None)


@live_enabled
@unlock_enabled
def test_one_shot_unlock(api):
    """PUT /doors/<id>/unlock triggers a single unlock event. Replaces
    native_api.unlock_door() / the /unlock/<id> endpoint."""
    host, token, door = api["host"], api["token"], api["door"]
    code, resp = _request(
        "PUT",
        host,
        f"/api/v1/developer/doors/{door}/unlock",
        token,
        body={},
    )
    assert code == 200, f"expected 200, got {code}: {resp}"
    assert resp["code"] == "SUCCESS"


# ---- realtime events ------------------------------------------------------


@live_enabled
def test_notifications_websocket_connects(api):
    """The notifications WebSocket accepts the bearer token. Replaces
    unifi_websocket.py which currently uses scraped cookies."""
    try:
        import websocket  # websocket-client, already in requirements.txt
    except ImportError:
        pytest.skip("websocket-client not installed")

    url = f"wss://{api['host']}:12445/api/v1/developer/devices/notifications"
    ws = websocket.create_connection(
        url,
        header=[f"Authorization: Bearer {api['token']}"],
        sslopt={"cert_reqs": ssl.CERT_NONE},
        timeout=10,
    )
    try:
        # Connection established is the assertion. Don't depend on an event firing.
        assert ws.connected
    finally:
        ws.close()
