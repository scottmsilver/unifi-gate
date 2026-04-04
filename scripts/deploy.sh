#!/usr/bin/env bash
set -euo pipefail

# Deploy unifi-gate to Incus container
# Usage: ./scripts/deploy.sh [container_name]

CONTAINER="${1:-unifi-gate}"
DEV_MODE="${2:-}"  # pass "dev" as second arg for dev mode
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_DIR="/opt/unifi-gate"

# Application files to deploy (code only — credentials/state are NOT overwritten)
APP_FILES=(
    server.py
    unifi_native_api.py
    unifi_access_api.py
    unifi_websocket.py
    schedule_manager.py
    schedule_journal.py
    hold_state_manager.py
    user_store.py
    invite_manager.py
    event_log.py
    kv_sync.py
    manage_users.py
    requirements.txt
)

TEMPLATE_FILES=(
    templates/index.html
    templates/invite.html
)

STATIC_FILES=(
    static/favicon.svg
)

# Credentials/state files — only deployed if missing in container
STATE_FILES=(
    credentials.json
    credentials_native.json
    users.json
    hold_state.json
    .env
)

echo "==> Deploying to container: $CONTAINER"

# Check container exists and start it if stopped
STATE=$(incus list "$CONTAINER" --format csv -c s 2>/dev/null || true)
if [ -z "$STATE" ]; then
    echo "Error: Container '$CONTAINER' not found"
    exit 1
elif [ "$STATE" != "RUNNING" ]; then
    echo "==> Container is $STATE, starting it..."
    incus start "$CONTAINER"
    echo "    Waiting for container to be ready..."
    sleep 5
fi

# Push application code
echo "==> Pushing application code..."
for f in "${APP_FILES[@]}"; do
    if [ -f "$PROJECT_DIR/$f" ]; then
        incus file push "$PROJECT_DIR/$f" "$CONTAINER$REMOTE_DIR/$f"
    else
        echo "  Warning: $f not found, skipping"
    fi
done

# Push templates
echo "==> Pushing templates..."
incus exec "$CONTAINER" -- mkdir -p "$REMOTE_DIR/templates" "$REMOTE_DIR/static"
for f in "${TEMPLATE_FILES[@]}"; do
    if [ -f "$PROJECT_DIR/$f" ]; then
        incus file push "$PROJECT_DIR/$f" "$CONTAINER$REMOTE_DIR/$f"
    fi
done

# Push static files
echo "==> Pushing static files..."
for f in "${STATIC_FILES[@]}"; do
    if [ -f "$PROJECT_DIR/$f" ]; then
        incus file push "$PROJECT_DIR/$f" "$CONTAINER$REMOTE_DIR/$f"
    fi
done

# Push state/credential files only if they don't exist in the container
echo "==> Checking credentials/state..."
SOURCE_DIR="${DEPLOY_STATE_DIR:-$PROJECT_DIR/test_wizard}"
for f in "${STATE_FILES[@]}"; do
    EXISTS=$(incus exec "$CONTAINER" -- test -f "$REMOTE_DIR/$f" && echo "yes" || echo "no")
    if [ "$EXISTS" = "no" ]; then
        if [ -f "$SOURCE_DIR/$f" ]; then
            echo "  Initializing $f from $SOURCE_DIR"
            incus file push "$SOURCE_DIR/$f" "$CONTAINER$REMOTE_DIR/$f"
        else
            echo "  Warning: $f missing in container and not found in $SOURCE_DIR"
        fi
    else
        echo "  $f already exists, skipping"
    fi
done

# Install/update dependencies
echo "==> Installing dependencies..."
incus exec "$CONTAINER" -- bash -c "cd $REMOTE_DIR && .venv/bin/pip install -q -r requirements.txt"

# Push systemd service file into the container
echo "==> Syncing service file..."
if [ -f "$PROJECT_DIR/services/unifi-gate.service" ]; then
    incus file push "$PROJECT_DIR/services/unifi-gate.service" \
        "$CONTAINER/etc/systemd/system/unifi-gate.service"
    incus exec "$CONTAINER" -- systemctl daemon-reload
    incus exec "$CONTAINER" -- systemctl enable unifi-gate 2>/dev/null || true
fi

# Restart the server
echo "==> Restarting server..."
# Check if systemd service is available in the container
HAS_SERVICE=$(incus exec "$CONTAINER" -- systemctl is-enabled unifi-gate 2>/dev/null || echo "no")
if [ "$HAS_SERVICE" = "enabled" ] && [ "$DEV_MODE" != "dev" ]; then
    incus exec "$CONTAINER" -- systemctl restart unifi-gate
    sleep 3
    PID=$(incus exec "$CONTAINER" -- bash -c "pgrep -f 'python.*server.py' || true" || true)
else
    incus exec "$CONTAINER" -- bash -c "pkill -f 'python.*server.py' || true" || true
    sleep 2
    SERVER_ARGS=""
    if [ "$DEV_MODE" = "dev" ]; then
        SERVER_ARGS="--dev"
        echo "  (dev mode — auth disabled)"
    fi
    incus exec "$CONTAINER" -- bash -c "cd $REMOTE_DIR && nohup .venv/bin/python server.py $SERVER_ARGS > /var/log/unifi-gate.log 2>&1 & disown"
    sleep 4
    PID=$(incus exec "$CONTAINER" -- bash -c "pgrep -f 'python.*server.py' || true" || true)
fi

# Verify it's running
if [ -n "$PID" ]; then
    IP=$(incus list "$CONTAINER" --format csv -c 4 | cut -d' ' -f1)
    echo ""
    echo "==> Deploy complete! Server running (PID: $PID)"
    echo "    URL: http://$IP:8000"
else
    echo ""
    echo "==> ERROR: Server failed to start. Check logs:"
    echo "    incus exec $CONTAINER -- tail -50 /var/log/unifi-gate.log"
    echo "    incus exec $CONTAINER -- journalctl -u unifi-gate --no-pager -n 50"
    exit 1
fi
