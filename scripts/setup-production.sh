#!/usr/bin/env bash
set -euo pipefail

# Set up the full production stack from scratch.
# Run this on a fresh machine to get your UniFi Gate instance running.
# NOTE: This script assumes Ubuntu 24.04 + Incus. Adapt for your setup.
#
# Prerequisites:
#   - Ubuntu 24.04
#   - cloudflared installed
#   - Tunnel credentials in ~/.cloudflared/
#   - User in incus-admin group
#
# Usage: sudo ./scripts/setup-production.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONTAINER="unifi-gate"

echo "==> Setting up production stack"
echo "    Project: $PROJECT_DIR"
echo ""

# --- 1. Incus ---
echo "==> Step 1: Incus container"

if ! command -v incus &>/dev/null; then
    echo "  Installing Incus..."
    apt install -y incus
    incus admin init --minimal
else
    echo "  Incus already installed"
fi

if incus list --format csv -c n | grep -q "^${CONTAINER}$"; then
    echo "  Container '$CONTAINER' already exists"
else
    echo "  Creating container..."
    incus launch images:ubuntu/24.04 "$CONTAINER"
    echo "  Installing Python..."
    incus exec "$CONTAINER" -- apt update -qq
    incus exec "$CONTAINER" -- apt install -y -qq python3 python3-pip python3-venv
    incus exec "$CONTAINER" -- mkdir -p /opt/unifi-gate
    incus exec "$CONTAINER" -- bash -c "cd /opt/unifi-gate && python3 -m venv .venv"
fi

# Auto-start container on boot
incus config set "$CONTAINER" boot.autostart true
echo "  Container auto-start: enabled"

# --- 2. Deploy app into container ---
echo ""
echo "==> Step 2: Deploying application"
# Use the deploy script (run as the regular user, not root)
DEPLOY_USER="${SUDO_USER:-$USER}"
su - "$DEPLOY_USER" -c "bash $PROJECT_DIR/scripts/deploy.sh"

# --- 3. Server systemd service (inside container) ---
echo ""
echo "==> Step 3: Server service (inside container)"
incus file push "$PROJECT_DIR/services/unifi-gate.service" \
    "${CONTAINER}/etc/systemd/system/unifi-gate.service"
incus exec "$CONTAINER" -- systemctl daemon-reload
incus exec "$CONTAINER" -- systemctl enable unifi-gate
incus exec "$CONTAINER" -- bash -c "pkill -f 'python.*server.py' || true" || true
sleep 1
incus exec "$CONTAINER" -- systemctl start unifi-gate
echo "  Server service: enabled and started"

# --- 4. Cloudflare tunnel systemd service (on host) ---
echo ""
echo "==> Step 4: Cloudflare tunnel service"
cp "$PROJECT_DIR/services/cloudflared-gate.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable cloudflared-gate

# Kill any manually-started tunnel
pkill -f "cloudflared tunnel.*config-gate" 2>/dev/null || true
sleep 1
systemctl start cloudflared-gate
echo "  Tunnel service: enabled and started"

# --- 5. Verify ---
echo ""
echo "==> Step 5: Verification"
sleep 3

CONTAINER_STATE=$(incus list "$CONTAINER" --format csv -c s)
SERVER_PID=$(incus exec "$CONTAINER" -- bash -c "pgrep -f 'python.*server.py' || true" || true)
TUNNEL_ACTIVE=$(systemctl is-active cloudflared-gate 2>/dev/null || echo "inactive")

echo "  Container:  $CONTAINER_STATE"
echo "  Server PID: ${SERVER_PID:-NOT RUNNING}"
echo "  Tunnel:     $TUNNEL_ACTIVE"

if [ "$CONTAINER_STATE" = "RUNNING" ] && [ -n "$SERVER_PID" ] && [ "$TUNNEL_ACTIVE" = "active" ]; then
    echo ""
    echo "==> Production stack is running!"
    echo "    https://gate.yourdomain.com"
else
    echo ""
    echo "==> WARNING: Something isn't running. Check the output above."
fi
