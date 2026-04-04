import argparse
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request

# Load .env file before accessing environment variables
load_dotenv()

from event_log import EventLog
from invite_manager import InviteManager
from kv_sync import CloudflareKV, sync_approved_users_to_kv
from schedule_manager import ScheduleManager
from unifi_access_api import UnifiAccessAPI
from unifi_native_api import UniFiNativeAPI
from unifi_websocket import UniFiAccessWebSocket
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
native_api = None
dev_api = None
schedule_manager = None
event_log = None
access_websocket = None
user_store = None
invite_manager = None
kv_client = None
_native_devices_cache = []  # Cache for native devices data (for images/details)
_door_thumbnails = {}  # Cache for door_id -> thumbnail_path
_websocket_events = deque(maxlen=100)  # Recent WebSocket events buffer


# --- Helper functions to reduce duplication ---


def get_config_path(filename: str) -> str:
    """Get full path for a config file in CONFIG_DIR."""
    return os.path.join(CONFIG_DIR, filename)


def require_api(api_obj, api_name: str = "API"):
    """Check if API is initialized, return error response if not."""
    if not api_obj:
        return jsonify({"error": f"{api_name} not initialized"}), 500
    return None


def require_user_store():
    """Check if user_store is initialized, return error response if not."""
    if not user_store:
        return jsonify({"error": "User store not initialized"}), 500
    return None


def require_schedule_manager():
    """Check if schedule_manager is initialized, return error response if not."""
    if not schedule_manager:
        return jsonify({"error": "API not initialized"}), 500
    return None


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


def populate_native_devices_cache() -> None:
    """Populate the native devices cache from the API."""
    global _native_devices_cache
    try:
        native_devices_response = native_api._make_request("GET", "/proxy/access/api/v2/devices")
        if native_devices_response and "data" in native_devices_response:
            _native_devices_cache = native_devices_response["data"]
            logger.info(f"Cached {len(_native_devices_cache)} native devices.")
    except Exception as e:
        logger.error(f"Failed to cache native devices: {e}")
        _native_devices_cache = []


def init_schedule_manager() -> ScheduleManager:
    """Create and return a new ScheduleManager instance."""
    state_file = get_config_path("hold_state.json")
    journal_file = get_config_path("schedule_journal.log")
    return ScheduleManager(native_api, state_file=state_file, journal_file=journal_file)


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


def run_periodic_sync():
    """Background thread to sync state periodically and clean orphan schedules."""
    logger.info("Starting periodic sync thread (interval: 60s)")
    while True:
        try:
            if schedule_manager:
                # Regular state sync (handles expired holds, re-injection)
                sync_results = schedule_manager.sync_state()

                # Log sync if anything happened
                if event_log:
                    migrated = len(sync_results.get("migrated", []))
                    expired = len(sync_results.get("expired", []))
                    reinjected = len(sync_results.get("reinjected", []))
                    if migrated > 0 or expired > 0 or reinjected > 0:
                        details = []
                        if expired > 0:
                            details.append(f"{expired} expired")
                        if reinjected > 0:
                            details.append(f"{reinjected} reinjected")
                        if migrated > 0:
                            details.append(f"{migrated} migrated")
                        event_log.log_sync(", ".join(details))

                # Orphan cleanup: for devices with no local hold, remove any stale schedules
                # Get all known door IDs from dev_api or native_api
                device_ids = []
                if dev_api:
                    try:
                        doors = dev_api.get_doors()
                        device_ids = [d.id for d in doors]
                    except Exception as e:
                        logger.warning(f"Failed to get doors from dev_api: {e}")
                elif native_api:
                    try:
                        doors = native_api.get_doors()
                        device_ids = [d.id for d in doors]
                    except Exception as e:
                        logger.warning(f"Failed to get doors from native_api: {e}")

                # Clean orphans for devices that have no local hold state
                for device_id in device_ids:
                    if not schedule_manager.state_manager.is_held(device_id):
                        result = schedule_manager.force_sync_device(device_id)
                        if result.get("removed", 0) > 0:
                            logger.info(f"Orphan cleanup: removed {result['removed']} blocks from {device_id}")
                            if event_log:
                                event_log.log_orphan_cleanup(device_id, result["removed"])

        except Exception as e:
            logger.error(f"Periodic sync failed: {e}")
        time.sleep(60)


def refresh_thumbnail_cache():
    global _door_thumbnails
    try:
        # Force fetch to ensure we have latest
        native_api._fetch_bootstrap()

        if native_api._bootstrap and "data" in native_api._bootstrap:
            data = native_api._bootstrap["data"]
            if isinstance(data, list) and len(data) > 0:
                main_site = data[0]

                # Iterate through floors to find doors and their thumbnails
                floors = main_site.get("floors", [])
                for floor in floors:
                    doors = floor.get("doors", [])
                    for door in doors:
                        door_id = door.get("unique_id")
                        extras = door.get("extras", {})
                        # Use static door_cover (more reliable than dynamic door_thumbnail)
                        cover_path = extras.get("door_cover")

                        if door_id and cover_path:
                            _door_thumbnails[door_id] = cover_path

                logger.info(f"Refreshed cache: {len(_door_thumbnails)} door thumbnails.")
    except Exception as e:
        logger.error(f"Failed to cache thumbnails: {e}")


def init_api():
    global native_api, dev_api, schedule_manager, _native_devices_cache, _door_thumbnails

    # Initialize Developer API for listing devices (it's more reliable for lists)
    try:
        dev_creds_file = os.path.join(CONFIG_DIR, "credentials.json")
        if os.path.exists(dev_creds_file):
            with open(dev_creds_file, "r") as f:
                dev_creds = json.load(f)
            dev_api = UnifiAccessAPI(host=dev_creds.get("host"), token=dev_creds.get("token"))
            logger.info(f"Developer API initialized from {dev_creds_file}")
    except Exception as e:
        logger.warning(f"Failed to initialize Developer API: {e}")

    creds_file = os.path.join(CONFIG_DIR, "credentials_native.json")
    if not os.path.exists(creds_file):
        creds_file = os.path.join(CONFIG_DIR, "credentials.json")

    if not os.path.exists(creds_file):
        logger.warning(f"No credentials found in {CONFIG_DIR}. Waiting for setup via UI.")
        return True  # Allow server to start in 'setup mode'

    native_creds = {}
    try:
        with open(creds_file, "r") as f:
            native_creds = json.load(f)
        logger.info(f"Loaded credentials from {creds_file}")
    except Exception as e:
        logger.error(f"Error reading credentials: {e}")
        return True

    # Store session file in config directory to avoid conflicts between instances
    session_file = os.path.join(CONFIG_DIR, ".unifi_access_session")
    native_api = UniFiNativeAPI(
        host=f"https://{native_creds.get('host', '')}",
        username=native_creds.get("username", "admin"),
        password=native_creds.get("password", native_creds.get("token", "")),
        session_file=session_file,
    )

    # Try to load session
    if not native_api.login():
        logger.error("Failed to login to UniFi Access Controller. /login endpoint required.")
        return True

    # Populate native devices cache
    populate_native_devices_cache()

    # Populate door thumbnails
    refresh_thumbnail_cache()

    schedule_manager = init_schedule_manager()

    # Initialize event log
    global event_log
    event_log = init_event_log()

    # Sync state on startup (handle expired holds, re-inject missing schedules)
    try:
        sync_results = schedule_manager.sync_state()
        logger.info(f"State sync: {len(sync_results['expired'])} expired, {len(sync_results['reinjected'])} reinjected")
    except Exception as e:
        logger.warning(f"State sync failed: {e}")

    # Initialize WebSocket for real-time events
    global access_websocket
    try:
        access_websocket = UniFiAccessWebSocket(native_api)
        access_websocket.on_event(handle_websocket_event)
        access_websocket.connect()
        logger.info("WebSocket client started")
    except Exception as e:
        logger.warning(f"WebSocket init failed: {e}")

    logger.info("API Initialized successfully")
    return True


def get_custom_site_name():
    """Get custom site name from credentials file."""
    try:
        creds_path = get_config_path("credentials_native.json")
        if os.path.exists(creds_path):
            with open(creds_path, "r") as f:
                creds = json.load(f)
                return creds.get("site_name")
    except Exception:
        pass
    return None


@app.route("/config", methods=["GET"])
def get_config_status():
    is_configured = native_api is not None
    is_connected = native_api.logged_in if native_api else False
    host = native_api.host if native_api else None
    username = native_api.username if native_api else None

    # Prioritize custom name -> dynamic name -> default
    custom_name = get_custom_site_name()
    if custom_name:
        site_name = custom_name
    elif is_connected:
        site_name = native_api.get_site_name()
    else:
        site_name = "Home Access"

    site_timezone = native_api.get_site_timezone() if is_connected else None
    is_past_6pm = schedule_manager.is_past_6pm() if schedule_manager else False

    # Get user admin status
    _, is_admin = get_verified_user()

    return jsonify(
        {
            "configured": is_configured,
            "connected": is_connected,
            "host": host,
            "username": username,
            "site_name": site_name,
            "site_timezone": site_timezone,
            "is_past_6pm": is_past_6pm,
            "is_admin": is_admin,
        }
    )


@app.route("/config/update", methods=["POST"])
def update_config():
    data = request.get_json(silent=True) or {}
    new_name = data.get("site_name")

    if new_name is not None:
        try:
            creds_path = get_config_path("credentials_native.json")
            creds = {}
            # Read existing
            if os.path.exists(creds_path):
                with open(creds_path, "r") as f:
                    creds = json.load(f)

            # Update
            creds["site_name"] = new_name

            # Write back
            with open(creds_path, "w") as f:
                json.dump(creds, f, indent=4)

            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    return jsonify({"status": "error", "message": "No site_name provided"}), 400


@app.route("/setup", methods=["POST"])
def setup():
    global native_api, dev_api, schedule_manager, _native_devices_cache

    data = request.get_json(silent=True) or {}
    host = data.get("host", "").strip()
    username = data.get("username", "admin").strip()
    password = data.get("password", "").strip()
    token = data.get("token", "").strip()  # 2FA
    site_name = data.get("site_name", "").strip()

    if not host or not password:
        return jsonify({"status": "error", "message": "Host and Password are required"}), 400

    # Initialize temp API to test connection
    if not host.startswith("http"):
        host = f"https://{host}"

    temp_api = UniFiNativeAPI(host=host, username=username, password=password)

    # Attempt login
    if temp_api.login(auth_code=token if token else None, force_new=True):
        # Success! Save credentials
        creds = {
            "host": host.replace("https://", "").replace("http://", "").rstrip("/"),
            "username": username,
            "password": password,
        }
        if site_name:
            creds["site_name"] = site_name

        try:
            with open("credentials_native.json", "w") as f:
                json.dump(creds, f, indent=4)
        except Exception as e:
            return jsonify({"status": "error", "message": f"Login success but failed to save file: {e}"}), 500

        # Promote to global
        native_api = temp_api

        # Re-initialize Developer API (it reloads its own creds)
        try:
            if os.path.exists("credentials.json"):  # UnifiAccessAPI uses this
                dev_api = UnifiAccessAPI()
                logger.info("Developer API re-initialized")
        except Exception as e:
            logger.warning(f"Failed to re-initialize Developer API: {e}")

        # Re-populate native devices cache
        populate_native_devices_cache()
        refresh_thumbnail_cache()

        schedule_manager = init_schedule_manager()

        # Initialize event log
        global event_log
        event_log = init_event_log()

        # Sync state after login
        try:
            schedule_manager.sync_state()
        except Exception as e:
            logger.warning(f"State sync failed: {e}")

        # Log the login
        user = request.headers.get("Cf-Access-Authenticated-User-Email", username)
        if event_log:
            event_log.log_login(user, success=True)

        return jsonify({"status": "success", "message": "Connected and saved!"})

    return jsonify({"status": "error", "message": "Login failed. Check credentials or provide 2FA token."}), 401


@app.route("/login", methods=["POST"])
def login():
    global schedule_manager, _native_devices_cache
    if not native_api:
        return jsonify({"error": "API client not configured"}), 500

    data = request.get_json(silent=True) or {}
    token = data.get("token")

    # Attempt login (force new if token provided)
    force = True if token else False

    if native_api.login(auth_code=token, force_new=force):
        schedule_manager = init_schedule_manager()
        # Re-populate native devices cache after login
        populate_native_devices_cache()
        refresh_thumbnail_cache()

        # Sync state after login
        try:
            schedule_manager.sync_state()
        except Exception as e:
            logger.warning(f"State sync failed: {e}")

        # Log the login
        user = request.headers.get("Cf-Access-Authenticated-User-Email", "admin")
        if event_log:
            event_log.log_login(user, success=True)

        return jsonify({"status": "success", "message": "Login successful"})

    return jsonify({"status": "error", "message": "Login failed. Check 2FA or credentials."}), 401


def get_verified_user() -> tuple[str, bool]:
    """
    Get the verified user email and admin status.

    Priority order:
    1. X-Verified-User header (from Cloudflare Worker after JWT validation)
    2. Cf-Access-Authenticated-User-Email header (legacy Cloudflare Access)
    3. "Guest" if in dev mode, otherwise None (unauthenticated)

    Returns: (email, is_admin)
    """
    # Check Worker-verified user first
    verified_user = request.headers.get("X-Verified-User")
    if verified_user:
        is_admin = user_store.is_admin(verified_user) if user_store else False
        return verified_user, is_admin

    # Fall back to Cloudflare Access header
    cf_user = request.headers.get("Cf-Access-Authenticated-User-Email")
    if cf_user:
        is_admin = user_store.is_admin(cf_user) if user_store else False
        return cf_user, is_admin

    # In dev mode, allow Guest access
    if DEV_MODE:
        return "Guest", True  # Guest is admin in dev mode for testing

    return None, False


def require_auth(f):
    """Decorator to require authentication (skipped in dev mode)."""
    from functools import wraps

    @wraps(f)
    def decorated_function(*args, **kwargs):
        user, _ = get_verified_user()
        if user is None:
            return jsonify({"error": "Authentication required"}), 401
        return f(*args, **kwargs)

    return decorated_function


def require_admin(f):
    """Decorator to require admin role (skipped in dev mode)."""
    from functools import wraps

    @wraps(f)
    def decorated_function(*args, **kwargs):
        user, is_admin = get_verified_user()
        if user is None:
            return jsonify({"error": "Authentication required"}), 401
        if not is_admin:
            return jsonify({"error": "Admin access required"}), 403
        return f(*args, **kwargs)

    return decorated_function


@app.route("/favicon.ico")
def favicon():
    """Serve favicon from static folder."""
    return app.send_static_file("favicon.svg")


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint. Returns 200 if healthy, 503 if not."""
    if not native_api or not native_api.logged_in:
        return jsonify({"status": "unhealthy", "controller_connected": False}), 503

    if not native_api._validate_session():
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
    # Use Developer API for listing if available, else fall back to Native
    if dev_api:
        try:
            doors = dev_api.get_doors()
            doors_json = []
            for d in doors:
                door_data = {
                    "id": d.id,
                    "name": d.name,
                    "is_online": True,
                    "status": d.display_status,
                    "imageUrl": None,  # Default to None
                }

                # 1. Try live thumbnail first (Best)
                if d.id in _door_thumbnails:
                    door_data["imageUrl"] = f"/door-image/{d.id}"

                # 2. Fallback to static device icon (Good)
                if not door_data["imageUrl"]:
                    # Try to find image from native devices cache
                    for native_dev in _native_devices_cache:
                        # Case 1: Physical device itself is the door
                        if native_dev.get("unique_id") == d.id:
                            if "images" in native_dev and "xs" in native_dev["images"]:
                                door_data["imageUrl"] = native_dev["images"]["xs"]
                                break
                        # Case 2: Physical device has extensions mapping to doors
                        if "extensions" in native_dev and isinstance(native_dev["extensions"], list):
                            for ext in native_dev["extensions"]:
                                if ext.get("target_type") == "door" and ext.get("target_value") == d.id:
                                    if "images" in native_dev and "xs" in native_dev["images"]:
                                        door_data["imageUrl"] = native_dev["images"]["xs"]
                                        break
                            if door_data["imageUrl"]:
                                break

                doors_json.append(door_data)

            doors_json.sort(key=lambda x: x["name"])
            return jsonify(doors_json)
        except Exception as e:
            logger.error(f"Dev API list failed: {e}")

    if not native_api:
        return jsonify({"error": "API not initialized"}), 500

    doors = native_api.get_doors()
    # Convert NativeDoor objects to JSON-serializable dicts
    doors_json = [
        {
            "id": d.id,
            "name": d.name,
            "is_online": d.is_online,
            "status": d.display_status,
            "imageUrl": None,  # No image if only native list is used
        }
        for d in doors
    ]

    # Add images from thumbnail cache if available
    for d in doors_json:
        if d["id"] in _door_thumbnails:
            d["imageUrl"] = f"/door-image/{d['id']}"

    doors_json.sort(key=lambda x: x["name"])

    return jsonify(doors_json)


@app.route("/door-image/<door_id>", methods=["GET"])
def get_door_image(door_id):
    if not native_api:
        return jsonify({"error": "API not initialized"}), 500

    path = _door_thumbnails.get(door_id)

    # If missing, try ONE refresh
    if not path:
        logger.info(f"Thumbnail miss for {door_id}, refreshing cache...")
        refresh_thumbnail_cache()
        path = _door_thumbnails.get(door_id)

    if not path:
        logger.warning(f"Thumbnail not found for door {door_id}")
        return jsonify({"error": "Thumbnail not found"}), 404

    full_url = f"{native_api.host}/proxy/access{path}"
    try:
        # Stream response to client
        resp = native_api.session.get(full_url, stream=True, verify=False)
        if resp.status_code == 200:
            return Response(resp.content, mimetype=resp.headers.get("Content-Type"))
        else:
            logger.error(f"Failed to fetch image from controller: {resp.status_code} for {full_url}")
            return jsonify({"error": "Failed to fetch image from controller"}), resp.status_code
    except Exception as e:
        logger.error(f"Exception fetching image: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/status/<device_id>", methods=["GET"])
def get_status(device_id):
    if not native_api:
        return jsonify({"error": "API not initialized"}), 500

    state_data = schedule_manager.get_hold_state_data(device_id)
    return jsonify({"device_id": device_id, **state_data})


def get_device_name(device_id: str) -> str:
    """Get device name from cache or return device_id."""
    if dev_api:
        try:
            doors = dev_api.get_doors()
            for d in doors:
                if d.id == device_id:
                    return d.name
        except:
            pass
    return device_id


def get_user_email() -> str:
    """Get user email from verified headers or dev mode fallback."""
    user, _ = get_verified_user()
    return user or "unknown"


@app.route("/unlock/<device_id>", methods=["POST"])
def unlock(device_id):
    user = get_user_email()
    device_name = get_device_name(device_id)

    # Prefer Developer API for momentary unlock as we know the endpoint works
    if dev_api:
        try:
            if dev_api.unlock_door(device_id):
                if event_log:
                    event_log.log_unlock(user, device_id, device_name)
                return jsonify({"status": "success", "action": "unlock"})
            return jsonify({"status": "error", "message": "Failed to unlock via Dev API"}), 500
        except Exception as e:
            logger.error(f"Dev API unlock failed: {e}")

    if not native_api:
        return jsonify({"error": "API not initialized"}), 500

    if native_api.unlock_door(device_id):
        if event_log:
            event_log.log_unlock(user, device_id, device_name)
        return jsonify({"status": "success", "action": "unlock"})
    return jsonify({"status": "error", "message": "Failed to unlock"}), 500


@app.route("/hold/today/<device_id>", methods=["POST"])
def hold_today(device_id):
    error = require_schedule_manager()
    if error:
        return error

    user = get_user_email()
    device_name = get_device_name(device_id)

    # Get optional end_time from request body
    data = request.get_json(silent=True) or {}
    end_time = data.get("end_time")  # Format: "HH:MM" (24-hour), defaults to 18:00

    if schedule_manager.inject_hold_open(device_id, end_time=end_time):
        if event_log:
            event_log.log_hold_today(user, device_id, device_name, end_time or "18:00")
        return jsonify({"status": "success", "action": "hold_today"})
    return jsonify({"status": "error", "message": "Failed to inject schedule"}), 500


@app.route("/hold/forever/<device_id>", methods=["POST"])
def hold_forever(device_id):
    error = require_schedule_manager()
    if error:
        return error

    user = get_user_email()
    device_name = get_device_name(device_id)

    if schedule_manager.inject_hold_open_forever(device_id):
        if event_log:
            event_log.log_hold_forever(user, device_id, device_name)
        return jsonify({"status": "success", "action": "hold_forever"})
    return jsonify({"status": "error", "message": "Failed to inject schedule"}), 500


@app.route("/hold/stop/<device_id>", methods=["POST"])
def stop_hold(device_id):
    error = require_schedule_manager()
    if error:
        return error

    user = get_user_email()
    device_name = get_device_name(device_id)

    if schedule_manager.remove_hold_open(device_id):
        if event_log:
            event_log.log_stop_hold(user, device_id, device_name)
        return jsonify({"status": "success", "action": "stop_hold"})
    return jsonify({"status": "error", "message": "Failed to remove schedule"}), 500


@app.route("/force-sync/<device_id>", methods=["POST"])
def force_sync(device_id):
    """Force sync a device's schedule to match local state."""
    error = require_schedule_manager()
    if error:
        return error

    result = schedule_manager.force_sync_device(device_id)
    return jsonify(result)


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
            "connected": access_websocket.is_connected() if access_websocket else False,
            "event_count": len(_websocket_events),
            "events": events,
        }
    )


@app.route("/debug/<device_id>", methods=["GET"])
def get_debug_info(device_id):
    """Get raw debug info for a door (UniFi API + local state)."""
    if not native_api:
        return jsonify({"error": "API not initialized"}), 500

    result = {
        "unifi": {
            "physical_device": None,  # The hardware (UA-Hub, UA-Gate, etc.)
            "door": None,  # The logical door from topology
            "schedule": None,
            "hardware_status": None,
        },
        "local": {
            "hold_state": None,
            "journal_entries": [],
        },
        "websocket": {
            "connected": access_websocket.is_connected() if access_websocket else False,
            "recent_events": [],
        },
    }

    # Get real-time hardware status from dev API or native API
    try:
        if dev_api:
            doors = dev_api.get_doors()
            for d in doors:
                if d.id == device_id:
                    result["unifi"]["hardware_status"] = {
                        "door_lock_relay_status": d.door_lock_relay_status,
                        "door_position_status": d.door_position_status,
                        "is_bind_hub": d.is_bind_hub,
                    }
                    break
        elif native_api:
            doors = native_api.get_doors()
            for d in doors:
                if d.id == device_id:
                    result["unifi"]["hardware_status"] = {
                        "door_lock_relay_status": d.door_lock_relay_status,
                        "door_position_status": d.door_position_status,
                        "is_online": d.is_online,
                    }
                    break
    except Exception as e:
        logger.error(f"Failed to get hardware status: {e}")

    # Find the physical device that manages this door (from native devices cache)
    try:
        for native_dev in _native_devices_cache:
            # Check if this device has an extension mapping to our door
            extensions = native_dev.get("extensions", [])
            if isinstance(extensions, list):
                for ext in extensions:
                    if ext.get("target_type") == "door" and ext.get("target_value") == device_id:
                        result["unifi"]["physical_device"] = {
                            "unique_id": native_dev.get("unique_id"),
                            "name": native_dev.get("name"),
                            "model": native_dev.get("model"),
                            "firmware": native_dev.get("firmware"),
                            "ip": native_dev.get("ip"),
                            "mac": native_dev.get("mac"),
                            "is_online": native_dev.get("is_online"),
                            "is_connected": native_dev.get("is_connected"),
                            "device_type": native_dev.get("device_type"),
                            "hw_type": native_dev.get("hw_type"),
                            "configs": native_dev.get("configs", []),
                        }
                        break
            # Also check if the device itself is the door (some device types)
            if native_dev.get("location_id") == device_id:
                result["unifi"]["physical_device"] = {
                    "unique_id": native_dev.get("unique_id"),
                    "name": native_dev.get("name"),
                    "model": native_dev.get("model"),
                    "firmware": native_dev.get("firmware"),
                    "ip": native_dev.get("ip"),
                    "mac": native_dev.get("mac"),
                    "is_online": native_dev.get("is_online"),
                    "is_connected": native_dev.get("is_connected"),
                    "device_type": native_dev.get("device_type"),
                    "hw_type": native_dev.get("hw_type"),
                    "configs": native_dev.get("configs", []),
                }
                break
    except Exception as e:
        logger.error(f"Failed to get physical device: {e}")

    # Get door info from topology
    try:
        if native_api._bootstrap and "data" in native_api._bootstrap:
            data = native_api._bootstrap["data"]
            if isinstance(data, list) and len(data) > 0:
                main_site = data[0]
                for floor in main_site.get("floors", []):
                    for door in floor.get("doors", []):
                        if door.get("unique_id") == device_id:
                            # Extract just the door-specific info, not device_groups
                            result["unifi"]["door"] = {
                                "unique_id": door.get("unique_id"),
                                "name": door.get("name"),
                                "full_name": door.get("full_name"),
                                "floor": floor.get("name"),
                                "extras": door.get("extras", {}),
                            }
                            break
    except Exception as e:
        logger.error(f"Failed to get door from topology: {e}")

    # Get raw schedule info
    try:
        schedule_response = native_api.get_unlock_schedule(device_id)
        result["unifi"]["schedule"] = schedule_response
    except Exception as e:
        logger.error(f"Failed to get schedule: {e}")

    # Get local hold state
    if schedule_manager:
        try:
            result["local"]["hold_state"] = schedule_manager.state_manager.get_hold(device_id)
        except Exception as e:
            logger.error(f"Failed to get hold state: {e}")

        # Get journal entries
        try:
            result["local"]["journal_entries"] = schedule_manager.journal.get_entries_for_device(device_id, limit=20)
        except Exception as e:
            logger.error(f"Failed to get journal entries: {e}")

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
def admin_list_users():
    """List all users (admin only)."""
    error = require_user_store()
    if error:
        return error

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
def admin_approve_user(email):
    """Approve a pending user (admin only)."""
    error = require_user_store()
    if error:
        return error

    user = user_store.update_user(email, status=UserStatus.APPROVED)
    if not user:
        return jsonify({"error": "User not found"}), 404

    # Log the approval
    admin_email = get_user_email()
    if event_log:
        event_log.log_admin_action(admin_email, "approve_user", email)

    # Auto-sync to Cloudflare KV
    sync_status = sync_users_to_kv(f"approved {email}")

    return jsonify({"status": "success", "user": {"email": user.email, "status": user.status}, "kv_sync": sync_status})


@app.route("/admin/users/<email>/reject", methods=["POST"])
@require_admin
def admin_reject_user(email):
    """Reject a user (admin only)."""
    error = require_user_store()
    if error:
        return error

    user = user_store.update_user(email, status=UserStatus.REJECTED)
    if not user:
        return jsonify({"error": "User not found"}), 404

    # Log the rejection
    admin_email = get_user_email()
    if event_log:
        event_log.log_admin_action(admin_email, "reject_user", email)

    return jsonify({"status": "success", "user": {"email": user.email, "status": user.status}})


@app.route("/admin/users/<email>/role", methods=["POST"])
@require_admin
def admin_change_role(email):
    """Change user role (admin only)."""
    error = require_user_store()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    new_role = data.get("role")

    if new_role not in ("admin", "user"):
        return jsonify({"error": "Invalid role. Must be 'admin' or 'user'"}), 400

    role = UserRole.ADMIN if new_role == "admin" else UserRole.USER
    user = user_store.update_user(email, role=role)

    if not user:
        return jsonify({"error": "User not found"}), 404

    # Log the role change
    admin_email = get_user_email()
    if event_log:
        event_log.log_admin_action(admin_email, "change_role", f"{email} -> {new_role}")

    return jsonify({"status": "success", "user": {"email": user.email, "role": user.role}})


@app.route("/admin/users/<email>", methods=["DELETE"])
@require_admin
def admin_delete_user(email):
    """Delete a user (admin only)."""
    error = require_user_store()
    if error:
        return error

    if not user_store.delete_user(email):
        return jsonify({"error": "User not found"}), 404

    # Log the deletion
    admin_email = get_user_email()
    if event_log:
        event_log.log_admin_action(admin_email, "delete_user", email)

    return jsonify({"status": "success"})


@app.route("/admin/users/add", methods=["POST"])
@require_admin
def admin_add_user():
    """Directly add an approved user (admin only). No invite link needed."""
    error = require_user_store()
    if error:
        return error

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

    # Log the action
    if event_log:
        event_log.log_admin_action(admin_email, "add_user", email)

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
def admin_invite_user():
    """Send an invite email (admin only)."""
    error = require_user_store()
    if error:
        return error

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

    # Log the invite
    if event_log:
        event_log.log_admin_action(admin_email, "invite_user", email)

    return jsonify({"status": "success", "message": f"Invite sent to {email}"})


@app.route("/admin/invites", methods=["GET"])
@require_admin
def admin_list_invites():
    """List pending invites (admin only)."""
    error = require_user_store()
    if error:
        return error

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
def admin_approve_invite(token):
    """Pre-approve a pending invite (admin only)."""
    error = require_user_store()
    if error:
        return error

    invite = user_store.set_invite_auto_approve(token, True)
    if not invite:
        return jsonify({"error": "Invite not found"}), 404

    # Log the action
    admin_email = get_user_email()
    if event_log:
        event_log.log_admin_action(admin_email, "pre_approve_invite", invite.email)

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
def validate_invite(token):
    """Validate an invite token (public endpoint)."""
    store_error = require_user_store()
    if store_error:
        return store_error

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
def accept_invite(token):
    """
    Accept an invite and create a pending user.

    The email in the request body must match the invite email.
    This is called after the user signs in with Firebase.
    """
    error = require_user_store()
    if error:
        return error

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
        # Start periodic sync thread
        sync_thread = threading.Thread(target=run_periodic_sync, daemon=True)
        sync_thread.start()

        # Run on all interfaces so Docker/Cloudflare can reach it easily
        app.run(host="0.0.0.0", port=args.port, debug=args.debug)
    else:
        print("Failed to initialize API. Check credentials.")
