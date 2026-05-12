"""Unit tests for unifi_protect.UniFiProtect — fully mocked.

Run: python -m pytest test_unifi_protect.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests
from unifi_access import (
    UnifiAccessAPIError,
    UnifiAccessAuthError,
    UnifiAccessError,
    UnifiAccessForbiddenError,
    UnifiAccessNotFoundError,
)
from unifi_protect import Camera, UniFiProtect, _parse_camera, cameras_by_mac


@pytest.fixture
def protect():
    return UniFiProtect(host="10.0.0.1", api_key="protect-key-abc")


def _resp_json(body, status=200):
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    r.headers = {"Content-Type": "application/json"}
    r.json.return_value = body
    r.text = str(body)
    return r


def _resp_binary(payload, status=200):
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    r.headers = {"Content-Type": "image/jpeg"}
    r.content = payload
    r.text = ""
    return r


def _resp_err(status, body=None):
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    r.headers = {"Content-Type": "application/json"}
    r.text = str(body or "")
    r.json.return_value = body or {}
    return r


# ---- auth / error mapping ------------------------------------------------


def test_api_key_header_is_set(protect):
    assert protect.session.headers["X-API-KEY"] == "protect-key-abc"
    assert "Authorization" not in protect.session.headers


def test_401_raises_auth(protect):
    with patch.object(protect.session, "request", return_value=_resp_err(401)):
        with pytest.raises(UnifiAccessAuthError):
            protect.list_cameras()


def test_403_raises_forbidden(protect):
    with patch.object(protect.session, "request", return_value=_resp_err(403)):
        with pytest.raises(UnifiAccessForbiddenError):
            protect.list_cameras()


def test_404_raises_not_found(protect):
    with patch.object(protect.session, "request", return_value=_resp_err(404)):
        with pytest.raises(UnifiAccessNotFoundError):
            protect.list_cameras()


def test_500_raises_api_error(protect):
    with patch.object(protect.session, "request", return_value=_resp_err(500, {"msg": "boom"})):
        with pytest.raises(UnifiAccessAPIError):
            protect.list_cameras()


def test_network_error_wraps(protect):
    with patch.object(protect.session, "request", side_effect=requests.ConnectionError("boom")):
        with pytest.raises(UnifiAccessError):
            protect.list_cameras()


# ---- cameras --------------------------------------------------------------


def test_list_cameras_parses(protect):
    body = [
        {
            "id": "cam1",
            "modelKey": "camera",
            "state": "CONNECTED",
            "name": "Gate - Entry",
            "mac": "28704E747634",
        },
        {
            "id": "cam2",
            "modelKey": "camera",
            "state": "DISCONNECTED",
            "name": "Driveway",
            "mac": "aaaaaaaaaaaa",
        },
    ]
    with patch.object(protect.session, "request", return_value=_resp_json(body)):
        cams = protect.list_cameras()
    assert len(cams) == 2
    g = cams[0]
    assert g.id == "cam1"
    assert g.mac == "28704E747634"
    assert g.state == "CONNECTED"
    # MAC is normalized to uppercase even if the source is lowercase.
    assert cams[1].mac == "AAAAAAAAAAAA"


def test_list_cameras_handles_unexpected_shape(protect):
    with patch.object(protect.session, "request", return_value=_resp_json({"oops": "not-a-list"})):
        cams = protect.list_cameras()
    assert cams == []


def test_fetch_camera_snapshot_returns_bytes(protect):
    img = b"\xff\xd8\xff\xe0fake-jpeg"
    with patch.object(protect.session, "request", return_value=_resp_binary(img)) as mock:
        result = protect.fetch_camera_snapshot("cam1")
    assert result == img
    args, kwargs = mock.call_args
    assert args[0] == "GET"
    assert args[1].endswith("/cameras/cam1/snapshot")
    assert kwargs["params"] is None


def test_fetch_camera_snapshot_with_dimensions(protect):
    with patch.object(protect.session, "request", return_value=_resp_binary(b"x")) as mock:
        protect.fetch_camera_snapshot("cam1", width=640, height=480)
    assert mock.call_args.kwargs["params"] == {"w": 640, "h": 480}


def test_fetch_camera_snapshot_only_height(protect):
    with patch.object(protect.session, "request", return_value=_resp_binary(b"x")) as mock:
        protect.fetch_camera_snapshot("cam1", height=720)
    assert mock.call_args.kwargs["params"] == {"h": 720}


# ---- healthcheck ----------------------------------------------------------


def test_healthcheck_true(protect):
    with patch.object(protect.session, "request", return_value=_resp_json({"applicationVersion": "7.0.0"})):
        assert protect.healthcheck() is True


def test_healthcheck_false_on_auth(protect):
    with patch.object(protect.session, "request", return_value=_resp_err(401)):
        assert protect.healthcheck() is False


# ---- mapping helper -------------------------------------------------------


def test_cameras_by_mac_lowercases_keys():
    cams = [
        Camera(id="c1", name="A", mac="AABBCCDDEEFF", state="CONNECTED", model_key="camera"),
        Camera(id="c2", name="B", mac="112233445566", state="CONNECTED", model_key="camera"),
    ]
    idx = cameras_by_mac(cams)
    assert idx["aabbccddeeff"].id == "c1"
    assert idx["112233445566"].id == "c2"
    # Ensure the original case isn't a key.
    assert "AABBCCDDEEFF" not in idx


def test_parse_camera_normalizes_mac_to_upper():
    c = _parse_camera({"id": "x", "mac": "aa:bb:cc:dd:ee:ff", "state": "X", "modelKey": "camera", "name": "Y"})
    # Library doesn't strip separators — only uppercases. That matches the
    # real Protect response which already returns separator-less uppercase.
    assert c.mac == "AA:BB:CC:DD:EE:FF"
