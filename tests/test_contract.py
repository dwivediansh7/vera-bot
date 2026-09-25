"""HTTP contract: schemas, versioning, counts, malformed input, never-500."""

from tests.conftest import push

ACTION_FIELDS = {"conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name",
                 "template_params", "body", "cta", "suppression_key", "rationale"}


def test_healthz_and_metadata(client):
    h = client.get("/v1/healthz").json()
    assert h["status"] == "ok" and isinstance(h["uptime_seconds"], int)
    assert h["contexts_loaded"] == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    m = client.get("/v1/metadata").json()
    for k in ("team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"):
        assert k in m
    assert m["team_name"] in ("Ansh Dwivedi", "ANSH DWIVEDI")


def test_warmup_counts_exact(client, data):
    for scope in ("category", "merchant", "customer"):
        for cid, p in data[scope].items():
            assert push(client, scope, cid, p).json()["accepted"] is True
    assert client.get("/v1/healthz").json()["contexts_loaded"] == {"category": 5, "merchant": 50, "customer": 200, "trigger": 0}


def test_versioning(client, data):
    cat = data["category"]["dentists"]
    r1 = push(client, "category", "dentists", cat, 1)
    assert r1.status_code == 200 and r1.json()["accepted"] and r1.json()["ack_id"] and r1.json()["stored_at"]
    r2 = push(client, "category", "dentists", cat, 1)
    assert r2.status_code == 409 and r2.json() == {"accepted": False, "reason": "stale_version", "current_version": 1}
    r3 = push(client, "category", "dentists", {**cat, "digest": []}, 2)
    assert r3.status_code == 200
    r4 = push(client, "category", "dentists", cat, 1)
    assert r4.status_code == 409 and r4.json()["current_version"] == 2
    assert client.get("/v1/healthz").json()["contexts_loaded"]["category"] == 1


def test_malformed_context(client):
    assert client.post("/v1/context", json={"scope": "planet", "context_id": "x", "version": 1, "payload": {}}).status_code == 400
    assert client.post("/v1/context", json={"scope": "merchant", "context_id": "", "version": 1, "payload": {}}).status_code == 400
    assert client.post("/v1/context", json={"scope": "merchant", "context_id": "m", "version": "one", "payload": {}}).status_code == 400
    assert client.post("/v1/context", json={"scope": "merchant", "context_id": "m", "version": 1, "payload": []}).status_code == 400
    r = client.post("/v1/context", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["accepted"] is False
    big = {"blob": "x" * (600 * 1024)}
    assert client.post("/v1/context", json={"scope": "merchant", "context_id": "m", "version": 1, "payload": big}).status_code == 400


def test_tick_empty_and_malformed(client):
    assert client.post("/v1/tick", json={"now": "2026-04-26T10:30:00Z", "available_triggers": []}).json() == {"actions": []}
    assert client.post("/v1/tick", content=b"garbage").json() == {"actions": []}
    assert client.post("/v1/tick", json={"available_triggers": ["does_not_exist"]}).json() == {"actions": []}
    assert client.post("/v1/tick", json={"now": "not-a-date", "available_triggers": None}).json() == {"actions": []}


def test_tick_actions_schema_and_limits(loaded, data):
    ids = list(data["trigger"].keys())
    r = loaded.post("/v1/tick", json={"now": "2026-09-26T10:00:00Z", "available_triggers": ids})
    acts = r.json()["actions"]
    assert 0 < len(acts) <= 20
    convs, vera_merchants, customers = set(), set(), set()
    for a in acts:
        assert set(a) >= ACTION_FIELDS, set(a) ^ ACTION_FIELDS
        assert a["body"].strip() and isinstance(a["template_params"], list) and all(isinstance(p, str) for p in a["template_params"])
        assert a["send_as"] in ("vera", "merchant_on_behalf")
        assert (a["customer_id"] is None) == (a["send_as"] == "vera")
        assert a["conversation_id"] not in convs
        convs.add(a["conversation_id"])
        if a["send_as"] == "vera":
            assert a["merchant_id"] not in vera_merchants, "at most one merchant-facing action per merchant per tick"
            vera_merchants.add(a["merchant_id"])
        else:
            assert a["customer_id"] not in customers
            customers.add(a["customer_id"])


def test_suppression_no_resend(loaded):
    tid = "trg_001_research_digest_dentists"
    a1 = loaded.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": [tid]}).json()["actions"]
    a2 = loaded.post("/v1/tick", json={"now": "2026-04-26T10:40:00Z", "available_triggers": [tid]}).json()["actions"]
    assert len(a1) == 1 and a2 == []


def test_reply_unknown_conversation_never_crashes(client):
    for msg in ["hello?", "", "Yes", "💥" * 50, "a" * 5000]:
        r = client.post("/v1/reply", json={"conversation_id": "conv_never_seen", "merchant_id": "m_unknown",
                                           "from_role": "merchant", "message": msg, "turn_number": 2})
        assert r.status_code == 200 and r.json()["action"] in ("send", "wait", "end")
    assert client.post("/v1/reply", content=b"nope").json()["action"] == "wait"


def test_teardown(loaded):
    assert loaded.post("/v1/teardown").status_code == 200
    assert loaded.get("/v1/healthz").json()["contexts_loaded"] == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}


def test_landing_page_is_rendered_html(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "<table>" in r.text and "<h2>" in r.text and "## " not in r.text and "|---|" not in r.text


def test_demo_works_and_is_isolated_from_the_judged_bot(client):
    before = client.get("/v1/healthz").json()["contexts_loaded"]
    assert client.get("/demo").status_code == 200
    trig = client.get("/demo/api/triggers").json()["triggers"]
    assert len(trig) >= 20
    d = client.post("/demo/api/compose", json={"trigger_id": "trg_023_competitor_opened_dentist"}).json()
    assert d["sent"] and "Dr. Meera" in d["body"] and d["rationale"]
    r = client.post("/demo/api/reply", json={"conversation_id": d["conversation_id"], "message": "Yes, let's do it"}).json()
    assert r["action"] == "send" and r["body"]
    again = client.post("/demo/api/compose", json={"trigger_id": "trg_023_competitor_opened_dentist"}).json()
    assert again["sent"]                                          # scenarios can be replayed
    skip = client.post("/demo/api/compose", json={"trigger_id": "trg_004_perf_dip_bharat"}).json()
    assert "sent" in skip
    assert client.get("/v1/healthz").json()["contexts_loaded"] == before   # judge state untouched
