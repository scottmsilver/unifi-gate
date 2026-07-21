"""Cached view of the Viking gate operator, fed by the Viking AWS-IoT backend.

Runs inside the gate backend. Holds a persistent shadow subscription (via the
`viking_monitor` package) and exposes a thread-safe snapshot for GET /operator.
Never raises out of the cache path — a bad message must not disturb the server.
"""

import logging
import threading
import time

log = logging.getLogger("viking_operator")

_CLEARED_CODES = {None, "", "0", "0.0"}


def _is_cleared(code):
    return code in _CLEARED_CODES


def _has_content(row):
    """True if a history row carries any displayable telemetry: a status change
    (Open/Close/Stop), an online/offline transition, a real error, or a voltage
    sample. Most OperatorHistory rows are metadata-only (no telemetry)."""
    for k in ("status", "online", "acVoltage", "batteryVoltage"):
        if row.get(k) is not None:
            return True
    err = row.get("error")
    return err is not None and str(err) != "0"


def meaningful_history(rows, limit=500):
    """Keep content rows (events + voltage), newest first. The cap is a safety
    bound well above current volume (~150 rows / a few days) so nothing old gets
    cut off; the UI filters by category (chips) and paginates. (If history ever
    outgrows this, switch to an on-demand 'load older' fetch.)"""
    return [r for r in rows if _has_content(r)][:limit]


class VikingOperator:
    def __init__(self, cfg, transport, history_interval=60, shadow_interval=5, creds_interval=2400, stale_after=90):
        self._cfg = cfg
        self._t = transport
        self._history_interval = history_interval
        self._shadow_interval = shadow_interval
        self._creds_interval = creds_interval  # refresh AWS creds before their ~1h expiry
        self._stale_after = stale_after  # cache updates every ~shadow_interval; older => polling is failing
        self._lock = threading.Lock()
        self._state = None
        self._online = None
        self._updated_at = None
        self._reported_at = None
        self._device = None
        self._error = {"code": None, "cleared": True, "description": None}
        self._history = []
        self._reachable = False

    # ── cache updates (pure, test-friendly) ──────────────────────
    def apply_shadow(self, payload):
        """Update the cache from a shadow document (bytes/str/dict). Never raises."""
        from viking_monitor import shadow

        try:
            diag = shadow.diagnostics(payload)  # {} on malformed
        except Exception:  # defensive: parser is already tolerant
            return
        if not diag:
            return
        code = diag.get("error")
        code = None if code is None else str(code)
        reported = shadow.reported_at(payload)  # device's real report time (None if absent)
        ota = shadow.ota(payload)  # firmware/device identity (may be absent on a partial update)
        with self._lock:
            self._state = {
                "model": diag.get("model"),
                "gate_state": diag.get("gate_state"),
                "motor": diag.get("motor"),
                "limit": diag.get("limit"),
                "ac_voltage": diag.get("ac_voltage"),
                "battery_voltage": diag.get("battery_voltage"),
            }
            self._error = {"code": code, "cleared": _is_cleared(code), "description": self._describe(code)}
            self._updated_at = int(time.time())  # when WE cached it (not device freshness)
            self._reported_at = reported  # when the DEVICE last reported (freshness)
            if ota:  # merge: keep last-known per key so a partial ota doesn't wipe fields
                dev = dict(self._device) if self._device else {}
                for k in ("fw_version", "mac", "device_id", "arch"):
                    if ota.get(k) is not None:
                        dev[k] = ota.get(k)
                self._device = dev
            self._reachable = True

    def set_presence(self, online):
        with self._lock:
            self._online = bool(online)

    def _describe(self, code):
        if _is_cleared(code):
            return None
        for row in self._history:
            if str(row.get("error")) == str(code) and row.get("errorDescription"):
                return row.get("errorDescription")
        return None

    # ── snapshot (the /operator payload) ─────────────────────────
    def snapshot(self):
        with self._lock:
            snap = {
                "reachable": self._reachable,
                "online": self._online,
                "updated_at": self._updated_at,
                "reported_at": self._reported_at,
                "device": dict(self._device) if self._device else None,
                "state": dict(self._state) if self._state else None,
                "error": dict(self._error),
                "history": list(self._history),
            }
        # Backend health: the shadow poll refreshes updated_at every
        # ~shadow_interval. If it hasn't in a while, our fetches are failing
        # (expired creds, network) — surface it so the UI warns instead of
        # silently showing frozen data.
        snap["stale"] = bool(
            snap["updated_at"] is not None and (int(time.time()) - snap["updated_at"]) > self._stale_after
        )
        return snap

    # ── live wiring ──────────────────────────────────────────────
    def start(self, history=True, poll=True):
        """Connect, prime the cache, and subscribe to shadow + presence.

        A get_shadow poll keeps the cache fresh even where the MQTT
        shadow/update/documents subscription doesn't deliver (the subscription
        is kept too, as a faster path when it does work).
        """
        from viking_monitor import topics

        self._t.connect()
        serial = self._cfg.serial
        try:
            self.apply_shadow(self._t.get_shadow(serial))
        except Exception as e:
            log.warning("initial shadow read failed: %s", e)
        self._t.subscribe(topics.shadow_update_documents_topic(serial), lambda p: self.apply_shadow(p))
        self._t.subscribe(topics.presence_connected_topic(serial), lambda p: self.set_presence(True))
        self._t.subscribe(topics.presence_disconnected_topic(serial), lambda p: self.set_presence(False))
        if poll:
            threading.Thread(target=self._creds_loop, daemon=True).start()
            threading.Thread(target=self._shadow_loop, daemon=True).start()
        if history:
            threading.Thread(target=self._history_loop, daemon=True).start()

    def _refresh_creds(self):
        """Re-auth for fresh AWS credentials; never raises."""
        try:
            self._t.refresh_credentials()
            log.info("refreshed AWS credentials")
        except Exception as e:
            log.warning("credential refresh failed: %s", e)

    def _creds_loop(self):
        """Proactively refresh credentials before they expire (~1h) so the shadow
        and history polls never hit an ExpiredToken and freeze the cache."""
        while True:
            time.sleep(self._creds_interval)
            self._refresh_creds()

    def _shadow_loop(self):
        """Periodically re-read the shadow so the cache tracks the device even
        when the update subscription is silent. Never raises out of the loop.
        On failure (e.g. expired creds), re-auth so the next poll recovers."""
        serial = self._cfg.serial
        while True:
            time.sleep(self._shadow_interval)
            try:
                self.apply_shadow(self._t.get_shadow(serial))
            except Exception as e:
                log.warning("shadow refresh failed: %s", e)
                self._refresh_creds()

    def _history_loop(self):
        from viking_monitor import history

        while True:
            try:
                creds = self._t.credentials()
                if creds:
                    # Most rows are metadata-only, so pull a deep window and keep
                    # only the ones with real content.
                    rows = history.fetch(self._cfg, creds, self._cfg.serial, limit=1500)
                    with self._lock:
                        self._history = meaningful_history(rows)
            except Exception as e:
                log.warning("history refresh failed: %s", e)
                self._refresh_creds()
            time.sleep(self._history_interval)
