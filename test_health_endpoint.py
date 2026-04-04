"""
Tests for /health endpoint.

Run with: python -m pytest test_health_endpoint.py -v
"""

# We need to mock out imports that server.py pulls in at module level
# before importing the app
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def app():
    """Create a test Flask app with mocked globals."""
    # Patch environment before importing server
    with patch.dict("os.environ", {"DEV_MODE": "true"}):
        # Need to reload server module to pick up patched env
        if "server" in sys.modules:
            del sys.modules["server"]

        # Mock the heavy dependencies that need real credentials
        mock_native_api = MagicMock()
        mock_native_api.logged_in = True
        mock_native_api.host = "https://10.0.0.1"
        mock_native_api.get_site_name.return_value = "Test Site"
        mock_native_api.get_site_timezone.return_value = "America/Los_Angeles"

        import server

        server.native_api = mock_native_api
        server.dev_api = None
        server.schedule_manager = MagicMock()
        server.app.config["TESTING"] = True

        yield server


@pytest.fixture
def client(app):
    """Create a test client."""
    return app.app.test_client()


class TestHealthEndpoint:
    """Tests for the /health endpoint."""

    def test_healthy_when_connected(self, client, app):
        """Returns healthy status when native API is connected."""
        app.native_api.logged_in = True
        app.native_api._validate_session.return_value = True

        resp = client.get("/health")
        data = resp.get_json()

        assert resp.status_code == 200
        assert data["status"] == "healthy"
        assert data["controller_connected"] is True

    def test_unhealthy_when_session_expired(self, client, app):
        """Returns unhealthy when controller session is invalid."""
        app.native_api.logged_in = True
        app.native_api._validate_session.return_value = False

        resp = client.get("/health")
        data = resp.get_json()

        assert resp.status_code == 503
        assert data["status"] == "unhealthy"
        assert data["controller_connected"] is False

    def test_unhealthy_when_api_not_initialized(self, client, app):
        """Returns unhealthy when native_api is None."""
        app.native_api = None

        resp = client.get("/health")
        data = resp.get_json()

        assert resp.status_code == 503
        assert data["status"] == "unhealthy"
        assert data["controller_connected"] is False

    def test_unhealthy_when_not_logged_in(self, client, app):
        """Returns unhealthy when not logged in."""
        app.native_api.logged_in = False

        resp = client.get("/health")
        data = resp.get_json()

        assert resp.status_code == 503
        assert data["status"] == "unhealthy"

    def test_health_does_not_require_auth(self, client, app):
        """Health endpoint should work without authentication."""
        # Even with no auth headers, should return a response (not 401)
        resp = client.get("/health")
        assert resp.status_code in (200, 503)
        assert "status" in resp.get_json()
