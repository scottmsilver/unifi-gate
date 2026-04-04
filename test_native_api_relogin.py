"""
Tests for auto-relogin on 401 in UniFiNativeAPI._make_request.

Run with: python -m pytest test_native_api_relogin.py -v
"""

import json
from unittest.mock import MagicMock

import pytest
import requests

from unifi_native_api import UniFiNativeAPI


@pytest.fixture
def api():
    """Create an API instance that thinks it's logged in."""
    a = UniFiNativeAPI(
        host="https://10.0.0.1",
        username="admin",
        password="testpass",
        session_file="/tmp/test_unifi_session",
    )
    a.logged_in = True
    a.csrf_token = "fake-csrf"
    return a


def make_response(status_code, json_data=None, headers=None):
    """Build a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    if json_data is not None:
        resp.text = json.dumps(json_data)
        resp.json.return_value = json_data
    elif status_code == 401:
        body = {"error": {"code": 401, "message": "Unauthorized"}}
        resp.text = json.dumps(body)
        resp.json.return_value = body
    else:
        resp.text = ""
    resp.raise_for_status = MagicMock()
    if status_code == 401:
        http_error = requests.exceptions.HTTPError(
            "401 Client Error: Unauthorized",
            response=resp,
        )
        resp.raise_for_status.side_effect = http_error
    return resp


class TestAutoReloginOn401:
    """Verify that _make_request auto-retries after re-login on 401."""

    def test_retries_after_401_and_succeeds(self, api):
        """When a request gets 401, re-login and retry the request."""
        success_data = {"code": "SUCCESS", "data": [{"id": "door1"}]}
        resp_401 = make_response(401)
        resp_200 = make_response(200, success_data)

        # First call returns 401, second (after relogin) returns 200
        api.session.request = MagicMock(side_effect=[resp_401, resp_200])

        # Mock login to succeed
        api.login = MagicMock(return_value=True)

        result = api._make_request("GET", "/proxy/access/api/v2/devices")

        assert result == success_data
        api.login.assert_called_once_with(force_new=True)
        assert api.session.request.call_count == 2

    def test_returns_none_when_relogin_fails(self, api):
        """When a request gets 401 and re-login also fails, return None."""
        resp_401 = make_response(401)

        api.session.request = MagicMock(return_value=resp_401)
        api.login = MagicMock(return_value=False)

        result = api._make_request("GET", "/proxy/access/api/v2/devices")

        assert result is None
        api.login.assert_called_once_with(force_new=True)

    def test_does_not_retry_on_other_errors(self, api):
        """Non-401 errors should NOT trigger re-login."""
        resp_500 = make_response(500)
        resp_500.raise_for_status.side_effect = requests.exceptions.HTTPError("500 Server Error", response=resp_500)

        api.session.request = MagicMock(return_value=resp_500)
        api.login = MagicMock()

        result = api._make_request("GET", "/proxy/access/api/v2/devices")

        assert result is None
        api.login.assert_not_called()
        assert api.session.request.call_count == 1

    def test_no_infinite_loop(self, api):
        """If retry also gets 401, give up (don't loop)."""
        resp_401 = make_response(401)

        api.session.request = MagicMock(return_value=resp_401)
        api.login = MagicMock(return_value=True)

        result = api._make_request("GET", "/proxy/access/api/v2/devices")

        assert result is None
        # Should only attempt login once, not loop
        api.login.assert_called_once()
        # Original request + one retry = 2
        assert api.session.request.call_count == 2
