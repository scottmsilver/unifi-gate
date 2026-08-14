"""/devices must never present an unreadable door as a definite state.

The bug this guards: `_parse_door` collapsed a missing controller field to
False, and /devices rendered False/False as "unlocked" beside a hardcoded
`is_online: True`. A door nobody could read was displayed as a confident,
online, UNLOCKED gate — the unsafe direction for a physical door.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from unifi_access import Door


@pytest.fixture
def app():
    with patch.dict("os.environ", {"DEV_MODE": "true"}):
        if "server" in sys.modules:
            del sys.modules["server"]
        import server

        server.access = MagicMock()
        server.schedule_manager = None
        server.cover_session = None
        server.app.config["TESTING"] = True
        yield server


@pytest.fixture
def client(app):
    return app.app.test_client()


def _door(locked, open_):
    return Door(id="d1", name="Gate", full_name="Gate", locked=locked, open=open_)


def _fetch_one(client, app, door):
    app.access.list_doors.return_value = [door]
    res = client.get("/devices")
    assert res.status_code == 200
    payload = res.get_json()
    assert len(payload) == 1
    return payload[0]


def test_unreadable_door_is_unknown_and_not_unlocked(client, app):
    body = _fetch_one(client, app, _door(None, None))
    assert body["status"] == "unknown", "an unreadable door must not read as unlocked"
    assert body["is_online"] is False, "is_online was hardcoded True; it must reflect reality"
    assert body["lock_state"] == "unknown"
    assert body["door_position"] == "unknown"


def test_unknown_position_still_reports_the_lock_relay(client, app):
    # A door with no position sensor is common. Losing the relay reading too
    # would throw away the security-relevant half of the answer.
    assert _fetch_one(client, app, _door(True, None))["status"] == "locked"
    assert _fetch_one(client, app, _door(False, None))["status"] == "unlocked"


def test_definite_readings_are_unchanged(client, app):
    # The tri-state must not alter what we report when we DO know. These three
    # are the pre-existing contract the UI and both mobile clients rely on.
    assert _fetch_one(client, app, _door(True, True))["status"] == "open"
    assert _fetch_one(client, app, _door(True, False))["status"] == "locked"
    assert _fetch_one(client, app, _door(False, False))["status"] == "unlocked"
    assert _fetch_one(client, app, _door(True, False))["is_online"] is True


def test_wire_types_stay_client_compatible(client, app):
    # Android's Device model types is_online as non-null Boolean and status as
    # String?. Emitting null here would crash the client on an unreadable door,
    # so "unknown" is a new VALUE, never a new type.
    body = _fetch_one(client, app, _door(None, None))
    assert isinstance(body["is_online"], bool)
    assert isinstance(body["status"], str)
    assert isinstance(body["lock_state"], str)
    assert isinstance(body["door_position"], str)
