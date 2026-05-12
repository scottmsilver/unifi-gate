"""Tests for /health endpoint against the new UniFiAccess-based server.

Run with: python -m pytest test_health_endpoint.py -v
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def app():
    """Test Flask app with the access global mocked."""
    with patch.dict("os.environ", {"DEV_MODE": "true"}):
        if "server" in sys.modules:
            del sys.modules["server"]

        mock_access = MagicMock()
        mock_access.host = "10.0.0.1"
        mock_access.healthcheck.return_value = True

        import server

        server.access = mock_access
        server.schedule_manager = MagicMock()
        server.app.config["TESTING"] = True

        yield server


@pytest.fixture
def client(app):
    return app.app.test_client()


def test_healthy_when_controller_responds(client, app):
    app.access.healthcheck.return_value = True
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "healthy"
    assert data["controller_connected"] is True


def test_unhealthy_when_controller_rejects(client, app):
    app.access.healthcheck.return_value = False
    resp = client.get("/health")
    assert resp.status_code == 503
    data = resp.get_json()
    assert data["status"] == "unhealthy"
    assert data["controller_connected"] is False


def test_unhealthy_when_access_is_none(client, app):
    app.access = None
    resp = client.get("/health")
    assert resp.status_code == 503
    data = resp.get_json()
    assert data["status"] == "unhealthy"


def test_health_does_not_require_auth(client, app):
    """/health should work without authentication."""
    resp = client.get("/health")
    assert resp.status_code in (200, 503)
    assert "status" in resp.get_json()
