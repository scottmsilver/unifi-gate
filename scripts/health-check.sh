#!/usr/bin/env bash
# Health check for unifi-gate service running in Incus container.
# Hits the /health endpoint and sends a ntfy alert on failure.
#
# Install as a cron job (every 5 minutes):
#   crontab -e
#   */5 * * * * /path/to/unifi-gate/scripts/health-check.sh
#
# Configuration via environment or defaults below:
CONTAINER="${UNIFI_CONTAINER:-unifi-gate}"
NTFY_TOPIC="${UNIFI_NTFY_TOPIC:-your-ntfy-topic}"
NTFY_URL="${UNIFI_NTFY_URL:-https://ntfy.sh}"
STATE_FILE="/tmp/.unifi-gate-health-state"

# Get container IP
IP=$(incus list "$CONTAINER" --format csv -c 4 2>/dev/null | cut -d' ' -f1)

if [ -z "$IP" ]; then
    # Container not found or not running
    STATUS="container_down"
    MSG="Container '$CONTAINER' is not running or not found"
else
    # Hit health endpoint (5s timeout)
    HTTP_CODE=$(curl -s -o /tmp/.unifi-health-body -w "%{http_code}" \
        --max-time 5 "http://${IP}:8000/health" 2>/dev/null)

    if [ "$HTTP_CODE" = "200" ]; then
        STATUS="healthy"
    elif [ "$HTTP_CODE" = "503" ]; then
        BODY=$(cat /tmp/.unifi-health-body 2>/dev/null)
        STATUS="unhealthy"
        MSG="Service unhealthy (controller disconnected): $BODY"
    elif [ "$HTTP_CODE" = "000" ]; then
        STATUS="unreachable"
        MSG="Service unreachable at http://${IP}:8000 (timeout or connection refused)"
    else
        STATUS="error"
        MSG="Unexpected HTTP $HTTP_CODE from health endpoint"
    fi
fi

# Read previous state
PREV_STATE=$(cat "$STATE_FILE" 2>/dev/null || echo "unknown")

# Write current state
echo "$STATUS" > "$STATE_FILE"

# Alert on state transitions (not on every check)
if [ "$STATUS" != "healthy" ] && [ "$PREV_STATE" = "healthy" ]; then
    # Just went unhealthy — alert
    curl -s \
        -H "Title: UniFi Gate Alert" \
        -H "Priority: high" \
        -H "Tags: warning" \
        -d "$MSG" \
        "$NTFY_URL/$NTFY_TOPIC" >/dev/null 2>&1
elif [ "$STATUS" = "healthy" ] && [ "$PREV_STATE" != "healthy" ] && [ "$PREV_STATE" != "unknown" ]; then
    # Recovered — send all-clear
    curl -s \
        -H "Title: UniFi Gate Recovered" \
        -H "Priority: default" \
        -H "Tags: white_check_mark" \
        -d "Service is healthy again" \
        "$NTFY_URL/$NTFY_TOPIC" >/dev/null 2>&1
fi
