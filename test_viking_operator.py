import json

import viking_operator


class FakeTransport:
    def __init__(self, shadow=None):
        self._shadow = shadow or {}
        self.subscribed = {}
        self.connected = False

    def connect(self):
        self.connected = True

    def subscribe(self, topic, callback):
        self.subscribed[topic] = callback

    def get_shadow(self, serial):
        return self._shadow

    def credentials(self):
        return None

    def refresh_credentials(self):
        self.refreshed = getattr(self, "refreshed", 0) + 1


REPORTED = {
    "state": {
        "reported": {
            "Operator": {"Model": "I-8", "Limit": "Open", "Motor": "Stop", "Error": "0", "AC": 27.3, "Bat": 27.8},
            "ota": {"device_id": "0000-VM-00000"},
        }
    }
}


def op(**kw):
    return viking_operator.VikingOperator(cfg=None, transport=FakeTransport(**kw))


def test_snapshot_before_any_data_is_reachable_false_state_none():
    s = op().snapshot()
    assert s["reachable"] is False
    assert s["state"] is None
    assert s["error"] == {"code": None, "cleared": True, "description": None}


def test_apply_shadow_populates_state_and_marks_reachable():
    o = op()
    o.apply_shadow(json.dumps(REPORTED).encode())
    s = o.snapshot()
    assert s["reachable"] is True
    assert s["state"] == {
        "model": "I-8",
        "gate_state": "Open",
        "motor": "Stop",
        "limit": "Open",
        "ac_voltage": 27.3,
        "battery_voltage": 27.8,
    }
    assert s["error"] == {"code": "0", "cleared": True, "description": None}
    assert isinstance(s["updated_at"], int)


def test_reported_at_comes_from_shadow_not_wall_clock():
    o = op()
    doc = json.loads(json.dumps(REPORTED))
    doc["metadata"] = {"reported": {"Operator": {"AC": {"timestamp": 1699999999}}}}
    o.apply_shadow(json.dumps(doc).encode())
    s = o.snapshot()
    # freshness is the DEVICE report time, not our cache time
    assert s["reported_at"] == 1699999999
    assert isinstance(s["updated_at"], int) and s["updated_at"] != s["reported_at"]


def test_device_captured_from_ota_and_kept_on_partial_update():
    o = op()
    doc = json.loads(json.dumps(REPORTED))
    doc["state"]["reported"]["ota"] = {
        "fw_version": "1.5",
        "mac": "AABBCCDDEEFF",
        "device_id": "0000-VM-00000",
        "arch": "esp32",
    }
    o.apply_shadow(json.dumps(doc).encode())
    assert o.snapshot()["device"] == {
        "fw_version": "1.5",
        "mac": "AABBCCDDEEFF",
        "device_id": "0000-VM-00000",
        "arch": "esp32",
    }
    # a later Operator-only update (no ota) must not wipe the known device info
    o.apply_shadow(json.dumps(REPORTED).encode())
    assert o.snapshot()["device"]["fw_version"] == "1.5"


def test_reported_at_none_when_shadow_has_no_timestamp():
    o = op()
    o.apply_shadow(json.dumps(REPORTED).encode())
    assert o.snapshot()["reported_at"] is None


def test_apply_shadow_with_error_is_not_cleared():
    o = op()
    bad = json.loads(json.dumps(REPORTED))
    bad["state"]["reported"]["Operator"]["Error"] = "15"
    o.apply_shadow(json.dumps(bad).encode())
    s = o.snapshot()
    assert s["error"]["code"] == "15"
    assert s["error"]["cleared"] is False


def test_malformed_shadow_never_raises_and_keeps_last_good():
    o = op()
    o.apply_shadow(json.dumps(REPORTED).encode())
    o.apply_shadow(b"not json")
    assert o.snapshot()["state"]["model"] == "I-8"


def test_meaningful_history_keeps_content_drops_metadata_and_cleared():
    rows = [
        {"id": "a", "timestamp": 3, "serialNumber": "x"},  # metadata-only → drop
        {"id": "b", "timestamp": 2, "status": "Open"},  # event → keep
        {"id": "c", "timestamp": 2, "acVoltage": 27.3},  # voltage → keep (UI chip)
        {"id": "d", "timestamp": 1, "error": "0"},  # cleared error → drop
        {"id": "e", "timestamp": 1, "error": "15"},  # real error → keep
        {"id": "f", "timestamp": 1, "online": False},  # connectivity → keep
    ]
    kept = viking_operator.meaningful_history(rows)
    assert [r["id"] for r in kept] == ["b", "c", "e", "f"]


def test_meaningful_history_caps():
    rows = [{"id": i, "status": "Stop"} for i in range(50)]
    assert len(viking_operator.meaningful_history(rows, limit=20)) == 20


def test_snapshot_stale_flag_reflects_backend_freshness():
    import time as _t

    o = op()
    o.apply_shadow(json.dumps(REPORTED).encode())  # sets updated_at = now
    assert o.snapshot()["stale"] is False
    # simulate the poll having stopped updating the cache
    o._updated_at = int(_t.time()) - 10_000
    assert o.snapshot()["stale"] is True


def test_refresh_creds_delegates_to_transport():
    o = op()
    o._refresh_creds()
    assert o._t.refreshed == 1


def test_set_presence_tracks_online():
    o = op()
    o.set_presence(False)
    assert o.snapshot()["online"] is False
    o.set_presence(True)
    assert o.snapshot()["online"] is True


def test_start_connects_and_subscribes_shadow_and_presence():
    t = FakeTransport(shadow=REPORTED)
    o = viking_operator.VikingOperator(cfg=_Cfg(), transport=t)
    o.start(history=False, poll=False)
    assert t.connected is True
    topics = list(t.subscribed)
    assert any("shadow/update/documents" in x for x in topics)
    assert any("presence/connected" in x for x in topics)
    assert any("presence/disconnected" in x for x in topics)
    # initial shadow read primed the cache
    assert o.snapshot()["state"]["model"] == "I-8"


class _Cfg:
    serial = "0000-VM-00000"
