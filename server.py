import argparse
import atexit
import json
import logging
import os
import re
import socket
import threading
import time
from collections import deque
from datetime import datetime
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request
from zeroconf import ServiceInfo, Zeroconf

# Load .env file before accessing environment variables
load_dotenv()

from event_log import EventLog
from invite_manager import InviteManager
from kv_sync import CloudflareKV, sync_approved_users_to_kv
from schedule_manager import EMPTY_HOLD_STATE, ScheduleManager
from unifi_access import AccessEventStream, UniFiAccess, UnifiAccessError, fetch_console_name
from unifi_protect import UniFiProtect, cameras_by_mac
from user_store import UserRole, UserStatus, UserStore

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")

app = Flask(__name__)

# Global config directory (can be set via CLI)
CONFIG_DIR = "."

# Development mode - set via DEV_MODE env var or --dev flag
DEV_MODE = os.environ.get("DEV_MODE", "").lower() in ("true", "1", "yes")

# Firebase config - loaded from environment variables
FIREBASE_CONFIG = {
    "apiKey": os.environ.get("FIREBASE_API_KEY", ""),
    "authDomain": os.environ.get("FIREBASE_AUTH_DOMAIN", ""),
    "projectId": os.environ.get("FIREBASE_PROJECT_ID", ""),
}

# Global API instance
access: UniFiAccess | None = None
protect: UniFiProtect | None = None
schedule_manager = None
event_log = None
event_stream: AccessEventStream | None = None
user_store = None
invite_manager = None
kv_client = None
_devices_cache: list = []  # List[Device] from access.list_devices()
_door_thumbnails: dict = {}  # door_id -> thumbnail_path (e.g. "/preview/...jpg")
_door_to_camera: dict = {}  # door_id -> protect.Camera (for snapshot fallback)
_websocket_events = deque(maxlen=100)
_zeroconf = None


# --- Helper functions to reduce duplication ---


def get_config_path(filename: str) -> str:
    """Get full path for a config file in CONFIG_DIR."""
    return os.path.join(CONFIG_DIR, filename)


def read_credentials() -> dict:
    """Load credentials.json. Returns empty dict if missing or unreadable."""
    path = get_config_path("credentials.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"credentials.json unreadable: {e}")
        return {}


def write_credentials(creds: dict) -> None:
    """Persist credentials.json. Raises on I/O error."""
    with open(get_config_path("credentials.json"), "w") as f:
        json.dump(creds, f, indent=4)


def update_credentials(**fields) -> dict:
    """Merge `fields` into credentials.json (read-modify-write). Empty/None
    values in `fields` are ignored so callers don't accidentally clobber
    existing keys when a field wasn't supplied. Returns the merged dict."""
    creds = read_credentials()
    for k, v in fields.items():
        if v is None or v == "":
            continue
        creds[k] = v
    write_credentials(creds)
    return creds


def requires_user_store(f):
    """Decorator: 500 if user_store isn't initialized."""

    @wraps(f)
    def wrap(*args, **kwargs):
        if not user_store:
            return jsonify({"error": "User store not initialized"}), 500
        return f(*args, **kwargs)

    return wrap


def require_schedule_manager():
    """Guard for non-route callers (e.g. _hold_endpoint). Returns Response|None."""
    if not schedule_manager:
        return jsonify({"error": "API not initialized"}), 500
    return None


# ---- Auth helpers (must be defined before any route that uses them) ----


def get_verified_user() -> tuple[str, bool]:
    """Return (email, is_admin) for the current request.

    Priority:
    1. X-Verified-User header (set by the Cloudflare Worker after Firebase JWT validation)
    2. Cf-Access-Authenticated-User-Email header (legacy Cloudflare Access)
    3. "Guest" in dev mode, else (None, False)
    """
    verified_user = request.headers.get("X-Verified-User")
    if verified_user:
        is_admin = user_store.is_admin(verified_user) if user_store else False
        return verified_user, is_admin

    cf_user = request.headers.get("Cf-Access-Authenticated-User-Email")
    if cf_user:
        is_admin = user_store.is_admin(cf_user) if user_store else False
        return cf_user, is_admin

    if DEV_MODE:
        return "Guest", True  # Guest is admin in dev mode for testing

    return None, False


def require_auth(f):
    """Decorator: require authentication (passes through in dev mode)."""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        user, _ = get_verified_user()
        if user is None:
            return jsonify({"error": "Authentication required"}), 401
        return f(*args, **kwargs)

    return decorated_function


def require_admin(f):
    """Decorator: require admin role (passes through in dev mode — Guest is admin)."""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        user, is_admin = get_verified_user()
        if user is None:
            return jsonify({"error": "Authentication required"}), 401
        if not is_admin:
            return jsonify({"error": "Admin access required"}), 403
        return f(*args, **kwargs)

    return decorated_function


def validate_email(email: str) -> tuple[bool, str]:
    """
    Validate an email address.
    Returns: (is_valid, normalized_email)
    """
    email = email.strip().lower() if email else ""
    if not email or "@" not in email:
        return False, email
    return True, email


def check_user_exists(email: str) -> tuple[bool, any]:
    """
    Check if a user already exists.
    Returns: (exists, user_or_none)
    """
    existing = user_store.get_user(email)
    return existing is not None, existing


def get_local_ip():
    """Get the local IP address for mDNS advertisement."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def start_mdns(port, site_name="UniFi Gate"):
    """Register mDNS service for auto-discovery."""
    global _zeroconf
    try:
        local_ip = get_local_ip()
        info = ServiceInfo(
            "_unifi-gate._tcp.local.",
            "UniFi Gate._unifi-gate._tcp.local.",
            addresses=[socket.inet_aton(local_ip)],
            port=port,
            properties={"site": site_name},
        )
        _zeroconf = Zeroconf()
        _zeroconf.register_service(info)
        logger.info(f"mDNS: advertising _unifi-gate._tcp on {local_ip}:{port}")
    except Exception as e:
        logger.warning(f"mDNS advertisement failed (non-fatal): {e}")


def stop_mdns():
    """Unregister mDNS service."""
    global _zeroconf
    if _zeroconf:
        _zeroconf.unregister_all_services()
        _zeroconf.close()
        _zeroconf = None


def sync_users_to_kv(context: str = ""):
    """Sync approved users to Cloudflare KV. Returns sync status dict or None."""
    if not kv_client or not kv_client.is_configured():
        return None

    approved_emails = user_store.get_approved_emails()
    success, message = sync_approved_users_to_kv(approved_emails, kv_client)

    if success:
        logger.info(f"Synced {len(approved_emails)} users to KV{' (' + context + ')' if context else ''}")
    else:
        logger.warning(f"KV sync failed{' (' + context + ')' if context else ''}: {message}")

    return {"synced": success, "message": message}


def populate_devices_cache() -> None:
    """Populate the access devices cache (hardware: hubs, intercoms, readers)."""
    global _devices_cache
    if not access:
        return
    try:
        _devices_cache = access.list_devices()
        logger.info(f"Cached {len(_devices_cache)} access devices.")
    except UnifiAccessError as e:
        logger.error(f"Failed to cache devices: {e}")
        _devices_cache = []


def populate_camera_index() -> None:
    """Build the door_id -> Protect Camera map via MAC matching.
    An Access intercom/hub has `id == its MAC`; a Protect camera carries `mac`.
    No-op (silent) if Protect isn't configured."""
    global _door_to_camera
    _door_to_camera = {}
    if not protect:
        return
    try:
        cams_by_mac = cameras_by_mac(protect.list_cameras())
    except UnifiAccessError as e:
        logger.warning(f"Protect camera fetch failed: {e}")
        return
    for dev in _devices_cache:
        cam = cams_by_mac.get(dev.id.lower())
        if cam and dev.location_id:
            _door_to_camera[dev.location_id] = cam
    if _door_to_camera:
        logger.info(f"Mapped {len(_door_to_camera)} door(s) to Protect cameras.")


def get_site_timezone() -> str:
    """Read site_timezone from credentials.json, falling back to UTC."""
    return read_credentials().get("site_timezone") or "UTC"


def init_schedule_manager() -> ScheduleManager:
    """Create and return a new ScheduleManager instance."""
    return ScheduleManager(access, timezone=get_site_timezone())


def init_event_log() -> EventLog:
    """Create and return a new EventLog instance."""
    event_log_file = get_config_path("event_log.jsonl")
    return EventLog(event_log_file)


def handle_websocket_event(event: dict) -> None:
    """Handle incoming WebSocket event and store in buffer."""
    # Add timestamp
    event["_received_at"] = datetime.now().isoformat()
    _websocket_events.appendleft(event)

    # Log interesting events
    event_type = event.get("event", "unknown")
    device_id = event.get("event_object_id", "")
    logger.debug(f"WebSocket event: {event_type} for {device_id[:8] if device_id else 'unknown'}")

    # Log meaningful events to activity log
    if not event_log:
        return

    # Get device name if possible
    device_name = get_device_name(device_id) if device_id else None

    # Map WebSocket events to user-friendly log entries
    if event_type == "access.data.device.remote_unlock":
        # Someone unlocked the door remotely
        event_log.log_ws_event("ws_unlock", device_id, device_name, "remote unlock")

    elif event_type == "access.door.unlock":
        # Door was unlocked (any method)
        method = event.get("data", {}).get("method", "unknown")
        event_log.log_ws_event("ws_unlock", device_id, device_name, method)

    elif event_type == "access.door.lock":
        # Door was locked
        event_log.log_ws_event("ws_lock", device_id, device_name)

    elif event_type == "access.data.device.update":
        # Device state changed - check for interesting changes
        data = event.get("data", {})
        configs = data.get("configs", [])
        for config in configs:
            key = config.get("key", "")
            value = config.get("value", "")
            # Log REX button presses
            if "rex" in key.lower() and value == "on":
                event_log.log_ws_event("ws_rex", device_id, device_name, "REX button pressed")
                break
            # Log door position changes
            if "door_position" in key.lower():
                status = "opened" if value == "open" else "closed"
                event_log.log_ws_event("ws_door_position", device_id, device_name, status)
                break

    elif event_type == "access.entry":
        # Access granted
        actor = event.get("data", {}).get("actor_name", "unknown")
        method = event.get("data", {}).get("method", "")
        event_log.log_ws_event("ws_entry", device_id, device_name, f"{actor} via {method}")

    elif event_type == "access.exit":
        # Exit event
        actor = event.get("data", {}).get("actor_name", "unknown")
        event_log.log_ws_event("ws_exit", device_id, device_name, actor)


def refresh_thumbnail_cache():
    """Build door_id -> thumbnail_path map from access.list_doors().
    Prefers the live `door_thumbnail` (Access preview) — the static `door_cover`
    is not fetchable via the Developer API (cookie-only on port 443)."""
    global _door_thumbnails
    if not access:
        return
    try:
        doors = access.list_doors()
    except UnifiAccessError as e:
        logger.error(f"Failed to refresh thumbnails: {e}")
        return
    _door_thumbnails = {}
    for d in doors:
        # Only `thumbnail_path` (/preview/...) is reachable with the bearer token.
        if d.thumbnail_path:
            _door_thumbnails[d.id] = d.thumbnail_path
    logger.info(f"Refreshed thumbnail cache: {len(_door_thumbnails)} door(s) with preview.")


def init_api():
    """Construct the Access (and optional Protect) clients from credentials.json,
    populate caches, attach the WebSocket event stream. Returns True if the
    server should boot (even in unconfigured 'setup mode')."""
    global access, protect, schedule_manager, event_log, event_stream

    creds = read_credentials()
    host = (creds.get("host") or "").strip()
    token = (creds.get("token") or "").strip()
    if not host or not token:
        logger.warning("credentials.json missing host or token. Waiting for setup via UI.")
        return True

    access = UniFiAccess(host=host, token=token)
    if not access.healthcheck():
        logger.error(f"UniFi Access controller at {host}:12445 rejected the token.")
        # Don't abort startup — the UI's /setup flow can replace the token.
        access = None
        return True
    logger.info(f"Access API connected to {host}.")

    # Optional Protect Integration for live camera snapshots
    protect_key = (creds.get("protect_api_key") or "").strip()
    if protect_key:
        try:
            protect = UniFiProtect(host=host, api_key=protect_key)
            if protect.healthcheck():
                logger.info("Protect API connected.")
            else:
                logger.warning("Protect API key rejected; snapshots disabled.")
                protect = None
        except Exception as e:
            logger.warning(f"Protect init failed: {e}")
            protect = None

    populate_devices_cache()
    populate_camera_index()
    refresh_thumbnail_cache()

    schedule_manager = init_schedule_manager()
    event_log = init_event_log()

    # Real-time events
    try:
        event_stream = AccessEventStream(access, handle_websocket_event)
        event_stream.start()
        logger.info("WebSocket event stream started.")
    except Exception as e:
        logger.warning(f"WebSocket event stream failed to start: {e}")

    logger.info("API initialized successfully")
    return True


def get_custom_site_name() -> str | None:
    """Custom site name from credentials.json (set via /config/update)."""
    return read_credentials().get("site_name")


# Site-name cache for the unauthenticated /api/system probe on port 443.
# The site name doesn't change in normal operation, so fetching it on every
# /config GET adds a 2s-timeout network round-trip for no benefit.
_console_name_cache: dict = {"value": None, "fetched_at": 0.0, "ttl": 3600.0}


def _cached_console_name(host: str) -> str | None:
    now = time.time()
    if (
        _console_name_cache["value"] is not None
        and now - _console_name_cache["fetched_at"] < _console_name_cache["ttl"]
    ):
        return _console_name_cache["value"]
    name = fetch_console_name(host)
    if name is not None:
        _console_name_cache["value"] = name
        _console_name_cache["fetched_at"] = now
    return name


@app.route("/config", methods=["GET"])
def get_config_status():
    is_configured = access is not None
    is_connected = access.healthcheck() if access else False
    host = access.host if access else None

    custom_name = get_custom_site_name()
    if custom_name:
        site_name = custom_name
    elif host:
        site_name = _cached_console_name(host) or "UniFi Gate"
    else:
        site_name = "UniFi Gate"

    is_past_6pm = schedule_manager.is_past_6pm() if schedule_manager else False

    _, is_admin = get_verified_user()

    return jsonify(
        {
            "configured": is_configured,
            "connected": is_connected,
            "host": host,
            "username": None,  # bearer-token auth: no user concept here
            "site_name": site_name,
            "is_past_6pm": is_past_6pm,
            "is_admin": is_admin,
        }
    )


@app.route("/config/update", methods=["POST"])
@require_admin
def update_config():
    """Update editable fields in credentials.json (currently: site_name)."""
    new_name = (request.get_json(silent=True) or {}).get("site_name")
    if new_name is None:
        return jsonify({"status": "error", "message": "No site_name provided"}), 400
    try:
        update_credentials(site_name=new_name)
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# Host is restricted to hostname/IP characters to prevent /setup from being
# pointed at arbitrary URLs (even with admin auth, this is a defense-in-depth
# check before writing to credentials.json).
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?(?::\d+)?$")

# Serialises /setup so two concurrent admins can't half-swap globals
# (access, protect, schedule_manager, event_stream) and leave the server
# in a torn state with mismatched objects.
_setup_lock = threading.Lock()


@app.route("/setup", methods=["POST"])
@require_admin
def setup():
    """Configure or re-configure the controller connection with a Developer
    API bearer token. No username/password, no 2FA."""
    global access, protect, schedule_manager, event_log, event_stream

    data = request.get_json(silent=True) or {}
    host = (data.get("host") or "").strip().replace("https://", "").replace("http://", "").rstrip("/")
    token = (data.get("token") or "").strip()
    site_name = (data.get("site_name") or "").strip()
    site_timezone = (data.get("site_timezone") or "").strip()
    protect_api_key = (data.get("protect_api_key") or "").strip()

    if not host or not token:
        return jsonify({"status": "error", "message": "host and token are required"}), 400
    if not _HOST_RE.match(host):
        return jsonify({"status": "error", "message": "host must be a hostname or IP"}), 400

    # Verify the token works before persisting.
    candidate = UniFiAccess(host=host, token=token)
    if not candidate.healthcheck():
        return (
            jsonify(
                {"status": "error", "message": "Token rejected by controller. Check host and Developer API token."}
            ),
            401,
        )

    if not _setup_lock.acquire(blocking=False):
        return jsonify({"status": "error", "message": "Another setup is in progress."}), 409
    try:
        # Merge with existing credentials so unrelated fields (e.g. protect_api_key
        # not in this POST body) aren't wiped on re-setup.
        try:
            update_credentials(
                host=host,
                token=token,
                site_name=site_name,
                site_timezone=site_timezone,
                protect_api_key=protect_api_key,
            )
        except Exception as e:
            return jsonify({"status": "error", "message": f"Token works but failed to save credentials: {e}"}), 500

        # Promote to globals and rebuild dependent state.
        access = candidate
        _console_name_cache["value"] = None  # host may have changed; force re-fetch
        if protect_api_key:
            try:
                protect = UniFiProtect(host=host, api_key=protect_api_key)
                if not protect.healthcheck():
                    protect = None
                    logger.warning("Protect API key rejected; snapshots disabled.")
            except Exception as e:
                logger.warning(f"Protect setup failed: {e}")
                protect = None
        else:
            protect = None

        populate_devices_cache()
        populate_camera_index()
        refresh_thumbnail_cache()

        schedule_manager = init_schedule_manager()
        event_log = init_event_log()

        # Restart the event stream so it picks up the new bearer token.
        if event_stream is not None:
            try:
                event_stream.stop()
            except Exception:
                pass
        try:
            event_stream = AccessEventStream(access, handle_websocket_event)
            event_stream.start()
        except Exception as e:
            logger.warning(f"WebSocket restart failed: {e}")

        user = request.headers.get("Cf-Access-Authenticated-User-Email", "setup")
        if event_log:
            event_log.log_login(user, success=True)

        return jsonify({"status": "success", "message": "Connected and saved."})
    finally:
        _setup_lock.release()


# /login removed: bearer-token auth has no per-session login. Kept as a
# 410 Gone so old clients get a clear signal.
@app.route("/login", methods=["POST"])
def login_gone():
    return (
        jsonify(
            {
                "status": "error",
                "message": "Login is no longer required — UniFi Gate now uses a Developer API token. Use /setup to configure.",
            }
        ),
        410,
    )


@app.route("/favicon.ico")
def favicon():
    """Serve favicon from static folder."""
    return app.send_static_file("favicon.svg")


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint. Returns 200 if healthy, 503 if not."""
    if not access or not access.healthcheck():
        return jsonify({"status": "unhealthy", "controller_connected": False}), 503
    return jsonify({"status": "healthy", "controller_connected": True})


@app.route("/")
def index():
    user_email, is_admin = get_verified_user()
    # In production without auth, show login page (handled by frontend)
    if user_email is None:
        user_email = ""
    return render_template(
        "index.html",
        user_email=user_email,
        admin_mode=False,
        is_admin=is_admin,
        dev_mode=DEV_MODE,
        firebase_config=FIREBASE_CONFIG,
    )


@app.route("/admin")
def admin():
    """Admin page - same as index but auto-opens settings view."""
    user_email, is_admin = get_verified_user()
    if user_email is None:
        user_email = ""
    return render_template(
        "index.html",
        user_email=user_email,
        admin_mode=True,
        is_admin=is_admin,
        dev_mode=DEV_MODE,
        firebase_config=FIREBASE_CONFIG,
    )


@app.route("/devices", methods=["GET"])
def list_devices():
    if not access:
        return jsonify({"error": "API not initialized"}), 500
    try:
        doors = access.list_doors()
    except UnifiAccessError as e:
        logger.error(f"list_doors failed: {e}")
        return jsonify({"error": str(e)}), 502

    doors_json = []
    for d in doors:
        # Status reads like the legacy display_status (open/closed + lock state).
        if d.open:
            status = "open"
        elif d.locked:
            status = "locked"
        else:
            status = "unlocked"

        # imageUrl is set only when a real image is available (Protect camera
        # or Access /preview thumbnail). Cameraless doors get null so the
        # frontend renders its round-circle lock-icon fallback instead of
        # squeezing the placeholder SVG into the wide-rect slot.
        has_real_image = d.id in _door_to_camera or d.id in _door_thumbnails

        hold = schedule_manager.get_hold_state_data(d.id) if schedule_manager else dict(EMPTY_HOLD_STATE)
        doors_json.append(
            {
                "id": d.id,
                "name": d.name,
                "is_online": True,
                "status": status,
                "imageUrl": f"/door-image/{d.id}" if has_real_image else None,
                **hold,
            }
        )
    doors_json.sort(key=lambda x: x["name"])
    return jsonify(doors_json)


@app.route("/door-image/<door_id>", methods=["GET"])
def get_door_image(door_id):
    """Serve a door image. Priority:
      1. Access /preview thumbnail — last access event capture. Most events
         happen in daylight (people coming and going) so this is usually a
         bright, cover-like image. Functions as our 'cover' replacement.
      2. Protect live snapshot — current frame. Used only as a fallback
         because at night it's just a dark picture of nothing.
      3. Placeholder SVG — when no camera is bound at all.

    Pass `?fresh=1` to force the live Protect snapshot (skipping the cover).
    """
    if access:
        prefer_fresh = request.args.get("fresh") in ("1", "true", "yes")

        # 1) Access /preview thumbnail — our cover-equivalent
        if not prefer_fresh:
            path = _door_thumbnails.get(door_id)
            if not path:
                refresh_thumbnail_cache()
                path = _door_thumbnails.get(door_id)
            if path:
                try:
                    img = access.fetch_thumbnail(path)
                    return Response(img, mimetype="image/jpeg")
                except UnifiAccessError as e:
                    logger.warning(f"Access thumbnail for {door_id} failed: {e}")

        # 2) Protect live snapshot — current frame, can be dark at night
        cam = _door_to_camera.get(door_id)
        if protect and cam:
            try:
                img = protect.fetch_camera_snapshot(cam.id)
                return Response(img, mimetype="image/jpeg")
            except UnifiAccessError as e:
                logger.warning(f"Protect snapshot for {door_id} failed: {e}")

    # 3) Placeholder for cameraless doors
    return app.send_static_file("door-placeholder.svg")


@app.route("/status/<device_id>", methods=["GET"])
def get_status(device_id):
    if not schedule_manager:
        return jsonify({"error": "API not initialized"}), 500
    state_data = schedule_manager.get_hold_state_data(device_id)
    return jsonify({"device_id": device_id, **state_data})


def get_device_name(device_id: str) -> str:
    """Look up a door name from the access API; falls back to the id."""
    if not access:
        return device_id
    try:
        for d in access.list_doors():
            if d.id == device_id:
                return d.name
    except UnifiAccessError:
        pass
    return device_id


def get_user_email() -> str:
    """Get user email from verified headers or dev mode fallback."""
    user, _ = get_verified_user()
    return user or "unknown"


def log_admin(verb: str, target: str = "") -> None:
    """Log an admin action against the current user; no-op if event_log is unset."""
    if event_log:
        event_log.log_admin_action(get_user_email(), verb, target)


@app.route("/unlock/<device_id>", methods=["POST"])
def unlock(device_id):
    if not access:
        return jsonify({"error": "API not initialized"}), 500
    user = get_user_email()
    device_name = get_device_name(device_id)
    try:
        access.unlock(device_id)
    except UnifiAccessError as e:
        logger.error(f"unlock {device_id} failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 502

    if event_log:
        event_log.log_unlock(user, device_id, device_name)
    return jsonify({"status": "success", "action": "unlock"})


def _hold_endpoint(device_id: str, action: str, run_op, log_event):
    """Shared /hold/* handler: gate on schedule_manager, run op, log, return JSON."""
    error = require_schedule_manager()
    if error:
        return error
    user = get_user_email()
    device_name = get_device_name(device_id)
    if run_op():
        if event_log:
            log_event(user, device_id, device_name)
        return jsonify({"status": "success", "action": action})
    return jsonify({"status": "error", "message": f"Failed: {action}"}), 500


@app.route("/hold/today/<device_id>", methods=["POST"])
def hold_today(device_id):
    end_time = (request.get_json(silent=True) or {}).get("end_time")  # "HH:MM", default 18:00
    return _hold_endpoint(
        device_id,
        "hold_today",
        lambda: schedule_manager.inject_hold_open(device_id, end_time=end_time),
        lambda u, i, n: event_log.log_hold_today(u, i, n, end_time or "18:00"),
    )


@app.route("/hold/forever/<device_id>", methods=["POST"])
def hold_forever(device_id):
    return _hold_endpoint(
        device_id,
        "hold_forever",
        lambda: schedule_manager.inject_hold_open_forever(device_id),
        event_log.log_hold_forever,
    )


@app.route("/hold/stop/<device_id>", methods=["POST"])
def stop_hold(device_id):
    return _hold_endpoint(
        device_id,
        "stop_hold",
        lambda: schedule_manager.remove_hold_open(device_id),
        event_log.log_stop_hold,
    )


@app.route("/events", methods=["GET"])
def get_events():
    """Get recent event log entries."""
    if not event_log:
        return jsonify([])

    limit = request.args.get("limit", 50, type=int)
    events = event_log.get_recent(limit=min(limit, 200))
    return jsonify(events)


@app.route("/websocket/events", methods=["GET"])
def get_websocket_events():
    """Get recent WebSocket events."""
    limit = request.args.get("limit", 50, type=int)
    device_id = request.args.get("device_id")

    events = list(_websocket_events)[:limit]

    # Filter by device if specified
    if device_id:
        events = [e for e in events if e.get("event_object_id") == device_id]

    return jsonify(
        {
            "connected": event_stream.is_connected() if event_stream else False,
            "event_count": len(_websocket_events),
            "events": events,
        }
    )


@app.route("/debug/<device_id>", methods=["GET"])
def get_debug_info(device_id):
    """Get raw debug info for a door (Developer API + local state)."""
    if not access:
        return jsonify({"error": "API not initialized"}), 500

    result = {
        "unifi": {
            "physical_device": None,
            "door": None,
            "schedule": None,
            "hardware_status": None,
        },
        "websocket": {
            "connected": event_stream.is_connected() if event_stream else False,
            "recent_events": [],
        },
    }

    # Hardware status (lock relay + door position) from /doors
    try:
        for d in access.list_doors():
            if d.id == device_id:
                result["unifi"]["hardware_status"] = {
                    "door_lock_relay_status": "lock" if d.locked else "unlock",
                    "door_position_status": "open" if d.open else "close",
                    "is_bind_hub": d.bound_to_hub,
                }
                result["unifi"]["door"] = {
                    "unique_id": d.id,
                    "name": d.name,
                    "full_name": d.full_name,
                    "floor_id": d.floor_id,
                    "extras": {
                        "door_cover": d.cover_path,
                        "door_thumbnail": d.thumbnail_path,
                        "door_thumbnail_last_update": d.thumbnail_updated_at,
                    },
                }
                break
    except UnifiAccessError as e:
        logger.error(f"Failed to get hardware status: {e}")

    # Physical device that hosts this door (intercom / hub) from cached /devices
    for dev in _devices_cache:
        if dev.location_id == device_id:
            result["unifi"]["physical_device"] = {
                "unique_id": dev.id,  # device MAC
                "name": dev.name,
                "model": dev.type,
                "is_online": dev.online,
                "is_connected": dev.connected,
                "is_managed": dev.managed,
                "is_adopted": dev.adopted,
                "device_type": dev.type,
                "capabilities": dev.capabilities,
            }
            break

    # Current lock_rule (the new replacement for "schedule")
    try:
        state = access.get_hold_state(device_id)
        result["unifi"]["schedule"] = {
            "type": state.type.value,
            "ended_time": state.ended_time,
        }
    except UnifiAccessError as e:
        logger.error(f"Failed to get lock_rule: {e}")

    # Get recent WebSocket events for this device
    try:
        device_events = [e for e in list(_websocket_events)[:50] if e.get("event_object_id") == device_id]
        result["websocket"]["recent_events"] = device_events[:10]
    except Exception as e:
        logger.error(f"Failed to get websocket events: {e}")

    return jsonify(result)


# =========== Auth Endpoints ===========


@app.route("/auth/me", methods=["GET"])
def auth_me():
    """Get current user info."""
    user_email, is_admin = get_verified_user()

    if user_email is None:
        return jsonify({"authenticated": False}), 401

    user_data = None
    if user_store and user_email != "Guest":
        user = user_store.get_user(user_email)
        if user:
            user_data = {
                "email": user.email,
                "role": user.role,
                "status": user.status,
            }

    return jsonify(
        {
            "authenticated": True,
            "email": user_email,
            "is_admin": is_admin,
            "user": user_data,
            "dev_mode": DEV_MODE,
        }
    )


# =========== Admin Endpoints ===========


@app.route("/admin/users", methods=["GET"])
@require_admin
@requires_user_store
def admin_list_users():
    """List all users (admin only)."""

    users = user_store.list_users()
    return jsonify(
        {
            "users": [
                {
                    "email": u.email,
                    "role": u.role,
                    "status": u.status,
                    "invited_by": u.invited_by,
                    "invited_at": u.invited_at,
                    "approved_at": u.approved_at,
                }
                for u in users
            ]
        }
    )


@app.route("/admin/users/<email>/approve", methods=["POST"])
@require_admin
@requires_user_store
def admin_approve_user(email):
    """Approve a pending user (admin only)."""

    user = user_store.update_user(email, status=UserStatus.APPROVED)
    if not user:
        return jsonify({"error": "User not found"}), 404

    log_admin("approve_user", email)

    # Auto-sync to Cloudflare KV
    sync_status = sync_users_to_kv(f"approved {email}")

    return jsonify({"status": "success", "user": {"email": user.email, "status": user.status}, "kv_sync": sync_status})


@app.route("/admin/users/<email>/reject", methods=["POST"])
@require_admin
@requires_user_store
def admin_reject_user(email):
    """Reject a user (admin only)."""

    user = user_store.update_user(email, status=UserStatus.REJECTED)
    if not user:
        return jsonify({"error": "User not found"}), 404

    log_admin("reject_user", email)

    return jsonify({"status": "success", "user": {"email": user.email, "status": user.status}})


@app.route("/admin/users/<email>/role", methods=["POST"])
@require_admin
@requires_user_store
def admin_change_role(email):
    """Change user role (admin only)."""

    data = request.get_json(silent=True) or {}
    new_role = data.get("role")

    if new_role not in ("admin", "user"):
        return jsonify({"error": "Invalid role. Must be 'admin' or 'user'"}), 400

    role = UserRole.ADMIN if new_role == "admin" else UserRole.USER
    user = user_store.update_user(email, role=role)

    if not user:
        return jsonify({"error": "User not found"}), 404

    log_admin("change_role", f"{email} -> {new_role}")

    return jsonify({"status": "success", "user": {"email": user.email, "role": user.role}})


@app.route("/admin/users/<email>", methods=["DELETE"])
@require_admin
@requires_user_store
def admin_delete_user(email):
    """Delete a user (admin only)."""

    if not user_store.delete_user(email):
        return jsonify({"error": "User not found"}), 404

    log_admin("delete_user", email)

    return jsonify({"status": "success"})


@app.route("/admin/users/add", methods=["POST"])
@require_admin
@requires_user_store
def admin_add_user():
    """Directly add an approved user (admin only). No invite link needed."""

    data = request.get_json(silent=True) or {}
    role = data.get("role", "user")

    is_valid, email = validate_email(data.get("email", ""))
    if not is_valid:
        return jsonify({"error": "Valid email required"}), 400

    # Check if user already exists
    exists, existing = check_user_exists(email)
    if exists:
        return jsonify({"error": f"User {email} already exists (status: {existing.status})"}), 400

    admin_email = get_user_email()

    # Create user as approved directly
    user_role = UserRole.ADMIN if role == "admin" else UserRole.USER
    user = user_store.create_user(
        email=email,
        role=user_role,
        status=UserStatus.APPROVED,
        invited_by=admin_email,
    )

    log_admin("add_user", email)

    # Auto-sync to KV
    sync_status = sync_users_to_kv(f"added {email}")

    return jsonify(
        {
            "status": "success",
            "message": f"User {email} added and approved. They can sign in with Google now.",
            "user": {"email": user.email, "status": user.status, "role": user.role},
            "kv_sync": sync_status,
        }
    )


@app.route("/admin/invite", methods=["POST"])
@require_admin
@requires_user_store
def admin_invite_user():
    """Send an invite email (admin only)."""

    data = request.get_json(silent=True) or {}

    is_valid, email = validate_email(data.get("email", ""))
    if not is_valid:
        return jsonify({"error": "Valid email required"}), 400

    # Check if user already exists
    exists, existing = check_user_exists(email)
    if exists:
        return jsonify({"error": f"User {email} already exists (status: {existing.status})"}), 400

    admin_email = get_user_email()
    auto_approve = data.get("auto_approve", False)

    # Create invite token
    invite = user_store.create_invite(email, admin_email, auto_approve=auto_approve)

    # Get base URL for invite link
    base_url = data.get("base_url") or request.url_root.rstrip("/")

    # Send email if configured
    if invite_manager and invite_manager.is_configured():
        result = invite_manager.send_invite(
            to_email=email,
            invite_token=invite.token,
            invited_by=admin_email,
            base_url=base_url,
        )
        if not result.success:
            # Still create invite, just note email failed
            return jsonify(
                {
                    "status": "partial",
                    "message": f"Invite created but email failed: {result.error}",
                    "invite_url": f"{base_url}/invite/{invite.token}",
                }
            )
    else:
        # No email configured, return invite URL directly
        return jsonify(
            {
                "status": "success",
                "message": "Invite created (email not configured)",
                "invite_url": f"{base_url}/invite/{invite.token}",
            }
        )

    log_admin("invite_user", email)

    return jsonify({"status": "success", "message": f"Invite sent to {email}"})


@app.route("/admin/invites", methods=["GET"])
@require_admin
@requires_user_store
def admin_list_invites():
    """List pending invites (admin only)."""

    invites = user_store.list_invites()
    return jsonify(
        {
            "invites": [
                {
                    "token": i.token,
                    "email": i.email,
                    "invited_by": i.invited_by,
                    "created_at": i.created_at,
                    "expires_at": i.expires_at,
                    "auto_approve": i.auto_approve,
                }
                for i in invites
            ]
        }
    )


@app.route("/admin/invites/<token>/approve", methods=["POST"])
@require_admin
@requires_user_store
def admin_approve_invite(token):
    """Pre-approve a pending invite (admin only)."""

    invite = user_store.set_invite_auto_approve(token, True)
    if not invite:
        return jsonify({"error": "Invite not found"}), 404

    log_admin("pre_approve_invite", invite.email)

    return jsonify(
        {
            "status": "success",
            "message": f"Invite for {invite.email} will be auto-approved when accepted",
            "invite": {
                "email": invite.email,
                "auto_approve": invite.auto_approve,
            },
        }
    )


# =========== Invite Endpoints (Public) ===========


@app.route("/invite/<token>", methods=["GET"])
@requires_user_store
def validate_invite(token):
    """Validate an invite token (public endpoint)."""

    is_valid, email, error = user_store.validate_invite(token)

    # If browser request, serve invite page
    if "text/html" in request.headers.get("Accept", ""):
        return render_template(
            "invite.html",
            token=token,
            email=email if is_valid else None,
            error=error if not is_valid else None,
            valid=is_valid,
            firebase_config=FIREBASE_CONFIG,
        )

    # API request - return JSON
    if not is_valid:
        return jsonify({"valid": False, "error": error}), 400

    return jsonify({"valid": True, "email": email})


@app.route("/invite/<token>/accept", methods=["POST"])
@requires_user_store
def accept_invite(token):
    """
    Accept an invite and create a pending user.

    The email in the request body must match the invite email.
    This is called after the user signs in with Firebase.
    """

    data = request.get_json(silent=True) or {}
    is_valid, email = validate_email(data.get("email", ""))

    if not is_valid:
        return jsonify({"error": "Email required"}), 400

    user = user_store.accept_invite(token, email)

    if not user:
        return jsonify({"error": "Invalid or expired invite, or email mismatch"}), 400

    # If user was auto-approved, sync to KV
    if user.status == UserStatus.APPROVED.value:
        sync_users_to_kv(f"invite accepted by {email}")

    message = (
        "Account created and approved!"
        if user.status == UserStatus.APPROVED.value
        else "Account created. Awaiting admin approval."
    )

    return jsonify(
        {
            "status": "success",
            "message": message,
            "user": {
                "email": user.email,
                "status": user.status,
            },
        }
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="UniFi Gate Server")
    parser.add_argument("-p", "--port", type=int, default=8000, help="Port to run on (default: 8000)")
    parser.add_argument(
        "-c", "--config-dir", type=str, default=".", help="Directory containing credential files (default: current dir)"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    parser.add_argument("--dev", action="store_true", help="Enable development mode (no auth required)")
    args = parser.parse_args()

    # Set config directory
    CONFIG_DIR = os.path.abspath(args.config_dir)
    logger.info(f"Using config directory: {CONFIG_DIR}")

    # Set dev mode from flag (env var already checked at module load)
    if args.dev:
        DEV_MODE = True

    if DEV_MODE:
        logger.info("Running in DEVELOPMENT MODE - authentication disabled")

    # Initialize user store, invite manager, and KV client
    user_store = UserStore(config_dir=CONFIG_DIR)
    invite_manager = InviteManager()
    kv_client = CloudflareKV()
    logger.info("User store initialized")
    if kv_client.is_configured():
        logger.info("Cloudflare KV sync configured")
    else:
        logger.warning("Cloudflare KV sync not configured - approvals won't auto-sync")

    if init_api():
        # Start mDNS advertisement
        start_mdns(args.port)
        atexit.register(stop_mdns)

        # Run on all interfaces so Docker/Cloudflare can reach it easily
        app.run(host="0.0.0.0", port=args.port, debug=args.debug)
    else:
        print("Failed to initialize API. Check credentials.")
