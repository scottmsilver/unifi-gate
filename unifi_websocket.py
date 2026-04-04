"""
UniFi Access WebSocket Client

Real-time event stream from UniFi Access controller.
Based on reverse engineering of hjdhjd/unifi-access library.

Events received:
- access.data.device.update - Terminal input changes (REX, DPS, etc.)
- access.data.v2.device.update - Device state changes
- access.data.device.remote_unlock - Unlock events
"""

import json
import logging
import ssl
import threading
import time
from typing import Callable, Dict, List, Optional

import websocket

logger = logging.getLogger(__name__)


class UniFiAccessWebSocket:
    """
    WebSocket client for real-time UniFi Access events.

    Usage:
        ws = UniFiAccessWebSocket(native_api)
        ws.on_event(callback)  # Register event handler
        ws.connect()  # Start receiving events
    """

    def __init__(self, native_api):
        """
        Initialize with an authenticated UniFiNativeAPI instance.

        Args:
            native_api: Authenticated UniFiNativeAPI with active session
        """
        self.native_api = native_api
        self.ws: Optional[websocket.WebSocketApp] = None
        self.thread: Optional[threading.Thread] = None
        self.running = False
        self.callbacks: List[Callable[[dict], None]] = []
        self.last_heartbeat = 0
        self.reconnect_delay = 5

    def on_event(self, callback: Callable[[dict], None]) -> None:
        """Register a callback for events."""
        self.callbacks.append(callback)

    def _get_cookie_header(self) -> str:
        """Extract cookies from native_api session."""
        cookies = self.native_api.session.cookies
        return "; ".join([f"{c.name}={c.value}" for c in cookies])

    def _on_message(self, ws, message: str) -> None:
        """Handle incoming WebSocket message."""
        # Heartbeat check
        if message.strip() == '"Hello"':
            self.last_heartbeat = time.time()
            logger.debug("WebSocket heartbeat received")
            return

        try:
            data = json.loads(message)
            logger.debug(f"WebSocket event: {data.get('event', 'unknown')}")

            # Dispatch to callbacks
            for callback in self.callbacks:
                try:
                    callback(data)
                except Exception as e:
                    logger.error(f"Error in event callback: {e}")

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse WebSocket message: {e}")

    def _on_error(self, ws, error) -> None:
        """Handle WebSocket error."""
        logger.error(f"WebSocket error: {error}")

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        """Handle WebSocket close."""
        logger.info(f"WebSocket closed: {close_status_code} - {close_msg}")

        # Attempt reconnect if we should still be running
        if self.running:
            logger.info(f"Reconnecting in {self.reconnect_delay}s...")
            time.sleep(self.reconnect_delay)
            self._connect_ws()

    def _on_open(self, ws) -> None:
        """Handle WebSocket open."""
        logger.info("WebSocket connected to UniFi Access")
        self.last_heartbeat = time.time()

    def _connect_ws(self) -> None:
        """Establish WebSocket connection."""
        if not self.native_api or not self.native_api.logged_in:
            logger.error("Cannot connect WebSocket: API not authenticated")
            return

        # Build WebSocket URL
        host = self.native_api.host.replace("https://", "").replace("http://", "")
        ws_url = f"wss://{host}/proxy/access/api/v2/ws/notification"

        logger.info(f"Connecting to WebSocket: {ws_url}")

        # Get cookies for auth
        cookie = self._get_cookie_header()

        self.ws = websocket.WebSocketApp(
            ws_url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open,
            cookie=cookie,
        )

        # Run with SSL verification disabled (self-signed certs)
        self.ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})

    def connect(self) -> None:
        """Start WebSocket connection in background thread."""
        if self.running:
            logger.warning("WebSocket already running")
            return

        self.running = True
        self.thread = threading.Thread(target=self._connect_ws, daemon=True)
        self.thread.start()
        logger.info("WebSocket thread started")

    def disconnect(self) -> None:
        """Stop WebSocket connection."""
        self.running = False
        if self.ws:
            self.ws.close()
        logger.info("WebSocket disconnected")

    def is_connected(self) -> bool:
        """Check if WebSocket is connected and receiving heartbeats."""
        if not self.ws:
            return False
        # Consider connected if heartbeat received in last 15 seconds
        return (time.time() - self.last_heartbeat) < 15


# Event type constants
EVENT_DEVICE_UPDATE = "access.data.device.update"
EVENT_V2_DEVICE_UPDATE = "access.data.v2.device.update"
EVENT_V2_LOCATION_UPDATE = "access.data.v2.location.update"
EVENT_REMOTE_UNLOCK = "access.data.device.remote_unlock"


def parse_terminal_inputs(device_configs: List[dict]) -> Dict[str, str]:
    """
    Parse terminal input states from device configs.

    Returns dict with keys: dps, rex, rel, ren
    Values are "on", "off", or "unknown"
    """
    result = {"dps": "unknown", "rex": "unknown", "rel": "unknown", "ren": "unknown"}

    for config in device_configs:
        key = config.get("key", "")
        value = config.get("value", "")

        # Check wiring state configs
        if "wiring_state_dps" in key:
            if value == "on":
                result["dps"] = "triggered" if "-neg" in key else result["dps"]
        elif "wiring_state_rex" in key:
            if value == "on":
                result["rex"] = "triggered" if "-neg" in key else result["rex"]
        elif "wiring_state_rel" in key:
            if value == "on":
                result["rel"] = "triggered" if "-neg" in key else result["rel"]
        elif "wiring_state_ren" in key:
            if value == "on":
                result["ren"] = "triggered" if "-neg" in key else result["ren"]

        # Also check for relay state (lock)
        if key == "relay" or key == "relay_state":
            result["relay"] = value

    return result
