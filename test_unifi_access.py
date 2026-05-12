"""Unit tests for unifi_access.UniFiAccess — fully mocked, no live UNVR.

Run: python -m pytest test_unifi_access.py -v
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest
import requests
from unifi_access import (
    AccessEventStream,
    Door,
    HoldType,
    UniFiAccess,
    UnifiAccessAPIError,
    UnifiAccessAuthError,
    UnifiAccessError,
    UnifiAccessForbiddenError,
    UnifiAccessNotFoundError,
    _parse_device,
    _parse_door,
    fetch_console_name,
)

# ---- helpers --------------------------------------------------------------


@pytest.fixture
def client():
    return UniFiAccess(host="10.0.0.1", token="test-token-123")


def _resp_ok(body):
    r = MagicMock(spec=requests.Response)
    r.status_code = 200
    r.headers = {"Content-Type": "application/json"}
    r.json.return_value = body
    r.text = json.dumps(body)
    return r


def _resp_binary(payload):
    r = MagicMock(spec=requests.Response)
    r.status_code = 200
    r.headers = {"Content-Type": "image/jpeg"}
    r.content = payload
    return r


def _resp_err(status, body=None):
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    r.headers = {"Content-Type": "application/json"}
    if body is not None:
        r.json.return_value = body
        r.text = json.dumps(body)
    else:
        r.json.side_effect = ValueError("no body")
        r.text = ""
    return r


# ---- low-level / error mapping --------------------------------------------


def test_bearer_token_is_set_on_session(client):
    assert client.session.headers["Authorization"] == "Bearer test-token-123"


def test_401_raises_auth_error(client):
    with patch.object(client.session, "request", return_value=_resp_err(401)):
        with pytest.raises(UnifiAccessAuthError):
            client.list_doors()


def test_403_raises_forbidden(client):
    with patch.object(client.session, "request", return_value=_resp_err(403)):
        with pytest.raises(UnifiAccessForbiddenError):
            client.list_doors()


def test_404_raises_not_found(client):
    with patch.object(client.session, "request", return_value=_resp_err(404)):
        with pytest.raises(UnifiAccessNotFoundError):
            client.list_doors()


def test_500_raises_generic_api_error(client):
    with patch.object(client.session, "request", return_value=_resp_err(500, {"msg": "boom"})):
        with pytest.raises(UnifiAccessAPIError) as exc:
            client.list_doors()
    assert exc.value.status_code == 500


def test_network_error_wraps_as_unifi_access_error(client):
    with patch.object(client.session, "request", side_effect=requests.ConnectionError("nope")):
        with pytest.raises(UnifiAccessError):
            client.list_doors()


def test_non_success_code_in_200_body_raises(client):
    body = {"code": "CODE_PARAMS_INVALID", "msg": "bad params", "data": None}
    with patch.object(client.session, "request", return_value=_resp_ok(body)):
        with pytest.raises(UnifiAccessAPIError) as exc:
            client.list_doors()
    assert "CODE_PARAMS_INVALID" in str(exc.value)


# ---- doors ---------------------------------------------------------------


def test_list_doors_parses_fields(client):
    body = {
        "code": "SUCCESS",
        "data": [
            {
                "id": "d1",
                "name": "Gate",
                "full_name": "Site - 1F - Gate",
                "door_lock_relay_status": "lock",
                "door_position_status": "open",
                "floor_id": "f1",
                "is_bind_hub": True,
                "extras": {
                    "door_cover": "/location_cover/d1.jpg",
                    "door_thumbnail": "/preview/d1.jpg",
                    "door_thumbnail_last_update": 1700000000,
                },
            },
            {
                "id": "d2",
                "name": "Side",
                "door_lock_relay_status": "unlock",
                "door_position_status": "close",
            },
        ],
    }
    with patch.object(client.session, "request", return_value=_resp_ok(body)):
        doors = client.list_doors()

    assert [d.id for d in doors] == ["d1", "d2"]
    g = doors[0]
    assert g.locked is True and g.open is True
    assert g.full_name == "Site - 1F - Gate"
    assert g.cover_path == "/location_cover/d1.jpg"
    assert g.thumbnail_path == "/preview/d1.jpg"
    assert g.thumbnail_updated_at == 1700000000
    assert g.bound_to_hub is True

    s = doors[1]
    assert s.locked is False and s.open is False
    assert s.cover_path is None and s.thumbnail_path is None
    assert s.full_name == "Side"  # defaults to name when full_name absent


def test_get_door(client):
    body = {
        "code": "SUCCESS",
        "data": {
            "id": "d1",
            "name": "Gate",
            "door_lock_relay_status": "lock",
            "door_position_status": "close",
        },
    }
    with patch.object(client.session, "request", return_value=_resp_ok(body)) as mock:
        d = client.get_door("d1")
    assert d.id == "d1"
    args, _ = mock.call_args
    assert args[0] == "GET"
    assert args[1].endswith("/api/v1/developer/doors/d1")


# ---- devices -------------------------------------------------------------


def test_list_devices_flattens_nested_array(client):
    body = {
        "code": "SUCCESS",
        "data": [
            [
                {
                    "id": "hub1",
                    "name": "UA Hub",
                    "type": "UGT",
                    "location_id": "doorgroup",
                    "is_online": True,
                    "is_connected": False,
                    "is_managed": True,
                    "is_adopted": True,
                    "capabilities": ["unlock_schedule", "multi_unlock_schedule"],
                }
            ]
        ],
    }
    with patch.object(client.session, "request", return_value=_resp_ok(body)):
        devices = client.list_devices()
    assert len(devices) == 1
    d = devices[0]
    assert d.type == "UGT"
    assert d.online is True and d.adopted is True
    assert "unlock_schedule" in d.capabilities


def test_list_devices_handles_flat_array(client):
    body = {"code": "SUCCESS", "data": [{"id": "x", "type": "UA", "location_id": "y", "is_online": True}]}
    with patch.object(client.session, "request", return_value=_resp_ok(body)):
        devices = client.list_devices()
    assert len(devices) == 1


# ---- unlock / hold -------------------------------------------------------


def _capture_call(client):
    """Returns the mock and asserts there was exactly one call."""
    return patch.object(client.session, "request", return_value=_resp_ok({"code": "SUCCESS"}))


def test_unlock_no_actor_sends_empty_body(client):
    with _capture_call(client) as mock:
        client.unlock("d1")
    assert mock.call_args.kwargs["json"] == {}
    assert mock.call_args.args[0] == "PUT"
    assert mock.call_args.args[1].endswith("/doors/d1/unlock")


def test_unlock_with_actor_pair(client):
    with _capture_call(client) as mock:
        client.unlock("d1", actor_id="a1", actor_name="Scott")
    assert mock.call_args.kwargs["json"] == {"actor_id": "a1", "actor_name": "Scott"}


def test_unlock_with_partial_actor_raises(client):
    with pytest.raises(ValueError):
        client.unlock("d1", actor_id="only-id")
    with pytest.raises(ValueError):
        client.unlock("d1", actor_name="only-name")


def test_unlock_with_extra_passthrough(client):
    with _capture_call(client) as mock:
        client.unlock("d1", extra={"reason": "test"})
    assert mock.call_args.kwargs["json"] == {"extra": {"reason": "test"}}


def test_hold_for_minutes_payload(client):
    with _capture_call(client) as mock:
        client.hold_for_minutes("d1", 15)
    assert mock.call_args.args[1].endswith("/doors/d1/lock_rule")
    assert mock.call_args.kwargs["json"] == {"type": "custom", "interval": 15}


def test_hold_for_minutes_rejects_zero(client):
    with pytest.raises(ValueError):
        client.hold_for_minutes("d1", 0)


def test_hold_for_minutes_rejects_negative(client):
    with pytest.raises(ValueError):
        client.hold_for_minutes("d1", -3)


def test_hold_until_rounds_up_and_caps_minimum(client):
    fixed_now = 1_000_000.0
    with patch("unifi_access.time.time", return_value=fixed_now), _capture_call(client) as mock:
        sent = client.hold_until("d1", fixed_now + 90)  # 1.5 minutes
    assert sent == 2
    assert mock.call_args.kwargs["json"] == {"type": "custom", "interval": 2}


def test_hold_until_in_the_past_still_sends_at_least_one_minute(client):
    fixed_now = 1_000_000.0
    with patch("unifi_access.time.time", return_value=fixed_now), _capture_call(client) as mock:
        sent = client.hold_until("d1", fixed_now - 60)
    assert sent == 1
    assert mock.call_args.kwargs["json"]["interval"] == 1


def test_hold_indefinitely(client):
    with _capture_call(client) as mock:
        client.hold_indefinitely("d1")
    assert mock.call_args.kwargs["json"] == {"type": "keep_unlock"}


def test_release_hold(client):
    with _capture_call(client) as mock:
        client.release_hold("d1")
    assert mock.call_args.kwargs["json"] == {"type": "lock_now"}


def test_keep_locked(client):
    with _capture_call(client) as mock:
        client.keep_locked("d1")
    assert mock.call_args.kwargs["json"] == {"type": "keep_lock"}


def test_get_hold_state_active(client):
    body = {"code": "SUCCESS", "data": {"type": "custom", "ended_time": 1_700_000_000}}
    with patch.object(client.session, "request", return_value=_resp_ok(body)):
        state = client.get_hold_state("d1")
    assert state.type == HoldType.CUSTOM
    assert state.ended_time == 1_700_000_000
    assert state.active is True


def test_get_hold_state_idle(client):
    body = {"code": "SUCCESS", "data": {"type": "", "ended_time": 0}}
    with patch.object(client.session, "request", return_value=_resp_ok(body)):
        state = client.get_hold_state("d1")
    assert state.type == HoldType.NONE
    assert state.ended_time is None
    assert state.active is False


# ---- thumbnails ----------------------------------------------------------


def test_fetch_thumbnail_returns_bytes(client):
    img = b"\xff\xd8\xff\xe0fake-jpeg"
    with patch.object(client.session, "request", return_value=_resp_binary(img)) as mock:
        result = client.fetch_thumbnail("/preview/d1.jpg")
    assert result == img
    assert mock.call_args.args[1].endswith("/system/static/preview/d1.jpg")


def test_fetch_thumbnail_normalizes_missing_leading_slash(client):
    with patch.object(client.session, "request", return_value=_resp_binary(b"x")) as mock:
        client.fetch_thumbnail("preview/d1.jpg")
    assert mock.call_args.args[1].endswith("/system/static/preview/d1.jpg")


def test_fetch_door_thumbnail_prefers_live_preview(client):
    door = Door(
        id="d",
        name="Gate",
        full_name="Gate",
        locked=True,
        open=False,
        cover_path="/location_cover/c.jpg",
        thumbnail_path="/preview/t.jpg",
    )
    with patch.object(client.session, "request", return_value=_resp_binary(b"x")) as mock:
        client.fetch_door_thumbnail(door)
    assert mock.call_args.args[1].endswith("/system/static/preview/t.jpg")


def test_fetch_door_thumbnail_returns_none_when_only_cover_available(client):
    """cover_path is NOT a usable fallback — it lives behind UniFi OS session
    auth on port 443 and isn't reachable via the Developer bearer token."""
    door = Door(
        id="d",
        name="Side",
        full_name="Side",
        locked=True,
        open=False,
        cover_path="/location_cover/c.jpg",
        thumbnail_path=None,
    )
    with patch.object(client.session, "request", side_effect=AssertionError("should not request")):
        assert client.fetch_door_thumbnail(door) is None


def test_fetch_door_thumbnail_returns_none_when_no_path(client):
    door = Door(id="d", name="Gate", full_name="Gate", locked=True, open=False)
    assert client.fetch_door_thumbnail(door) is None


# ---- healthcheck ---------------------------------------------------------


def test_healthcheck_true_when_doors_returns_200(client):
    with patch.object(client.session, "request", return_value=_resp_ok({"code": "SUCCESS", "data": []})):
        assert client.healthcheck() is True


def test_healthcheck_false_on_auth_error(client):
    with patch.object(client.session, "request", return_value=_resp_err(401)):
        assert client.healthcheck() is False


def test_healthcheck_false_on_network_error(client):
    with patch.object(client.session, "request", side_effect=requests.ConnectionError):
        assert client.healthcheck() is False


# ---- parsers -------------------------------------------------------------


def test_parse_door_minimal():
    d = _parse_door({"id": "x", "name": "Y", "door_lock_relay_status": "lock", "door_position_status": "close"})
    assert d.id == "x" and d.locked is True and d.open is False
    assert d.full_name == "Y"
    assert d.cover_path is None


def test_parse_device_minimal():
    d = _parse_device({"id": "h", "type": "UGT", "location_id": "lg", "is_online": True})
    assert d.id == "h"
    assert d.online is True
    assert d.capabilities == []


# ---- console name --------------------------------------------------------


def test_fetch_console_name_ok():
    with patch("unifi_access.requests.get") as mock:
        mock.return_value.ok = True
        mock.return_value.json.return_value = {"name": "316 Costello Security"}
        assert fetch_console_name("host") == "316 Costello Security"


def test_fetch_console_name_returns_none_on_network_error():
    with patch("unifi_access.requests.get", side_effect=requests.ConnectionError):
        assert fetch_console_name("host") is None


def test_fetch_console_name_returns_none_on_non_ok():
    with patch("unifi_access.requests.get") as mock:
        mock.return_value.ok = False
        assert fetch_console_name("host") is None


# ---- event stream --------------------------------------------------------


def test_event_stream_dispatches_events(client):
    """Mock websocket.create_connection and verify on_event is called with parsed dicts."""
    events_received = []

    def handler(event):
        events_received.append(event)

    ws_mock = MagicMock()
    messages = [json.dumps({"event": "test1"}), json.dumps({"event": "test2"})]
    ws_mock.recv.side_effect = messages + [Exception("close")]

    with patch("websocket.create_connection", return_value=ws_mock):
        stream = AccessEventStream(client, handler)
        stream.start()
        # Wait for the thread to process all messages
        for _ in range(50):
            if len(events_received) >= 2:
                break
            time.sleep(0.05)
        stream.stop()

    assert events_received == [{"event": "test1"}, {"event": "test2"}]


def test_event_stream_skips_non_json(client):
    received = []
    ws_mock = MagicMock()
    ws_mock.recv.side_effect = ["not-json", json.dumps({"event": "ok"}), Exception("done")]

    with patch("websocket.create_connection", return_value=ws_mock):
        stream = AccessEventStream(client, received.append)
        stream.start()
        for _ in range(50):
            if received:
                break
            time.sleep(0.05)
        stream.stop()

    assert received == [{"event": "ok"}]


def test_event_stream_swallows_handler_exception(client):
    received = []

    def handler(event):
        received.append(event)
        raise RuntimeError("handler boom")  # should be logged, not crash the stream

    ws_mock = MagicMock()
    ws_mock.recv.side_effect = [
        json.dumps({"event": "a"}),
        json.dumps({"event": "b"}),
        Exception("done"),
    ]

    with patch("websocket.create_connection", return_value=ws_mock):
        stream = AccessEventStream(client, handler)
        stream.start()
        for _ in range(50):
            if len(received) >= 2:
                break
            time.sleep(0.05)
        stream.stop()

    assert received == [{"event": "a"}, {"event": "b"}]
