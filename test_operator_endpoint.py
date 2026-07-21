import server


def _client():
    server.app.config["TESTING"] = True
    return server.app.test_client()


def test_operator_endpoint_reports_unconfigured_when_absent():
    server.viking_op = None
    r = _client().get("/operator")
    assert r.status_code == 200
    assert r.get_json()["reachable"] is False


def test_operator_endpoint_returns_snapshot():
    class FakeOp:
        def snapshot(self):
            return {
                "reachable": True,
                "online": True,
                "updated_at": 1,
                "state": {"model": "I-8"},
                "error": {"code": "0", "cleared": True, "description": None},
                "history": [],
            }

    server.viking_op = FakeOp()
    r = _client().get("/operator")
    assert r.status_code == 200
    body = r.get_json()
    assert body["reachable"] is True and body["state"]["model"] == "I-8"
