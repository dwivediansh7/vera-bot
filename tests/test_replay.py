"""Multi-turn replay scenarios through the HTTP API."""

import re

from app.validator import QUALIFYING

MID = "m_001_drmeera_dentist_delhi"


def start(client, tid="trg_001_research_digest_dentists"):
    acts = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": [tid]}).json()["actions"]
    assert len(acts) == 1
    return acts[0]


def rep(client, conv, msg, turn, mid=MID, role="merchant", cust=None):
    return client.post("/v1/reply", json={"conversation_id": conv, "merchant_id": mid, "customer_id": cust, "from_role": role,
                                          "message": msg, "received_at": "2026-04-26T10:45:00Z", "turn_number": turn}).json()


def test_auto_reply_hell_same_conversation(loaded):
    a = start(loaded)
    canned = "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."
    r = [rep(loaded, a["conversation_id"], canned, t) for t in (2, 3, 4, 5)]
    assert [x["action"] for x in r[:3]] == ["send", "wait", "end"]
    assert r[1]["wait_seconds"] >= 3600
    assert r[3]["action"] == "end"


def test_auto_reply_fresh_conversation_ids(loaded):
    msg = "Thank you for contacting us! Our team will respond shortly."
    acts = [rep(loaded, f"conv_auto_{i}", msg, i + 1)["action"] for i in range(1, 5)]
    assert acts[:3] == ["send", "wait", "end"]


def test_auto_reply_streak_resets_after_human_reply(loaded):
    msg = "Thank you for contacting us! Our team will respond shortly."
    assert rep(loaded, "conv_ar_a", msg, 2)["action"] == "send"
    assert rep(loaded, "conv_ar_b", "yes go ahead", 2)["action"] == "send"      # a human answered
    assert rep(loaded, "conv_ar_c", msg, 2)["action"] == "send"                 # streak restarted, not 'wait'


def test_auto_reply_by_repetition_without_phrase(loaded):
    a = start(loaded)
    canned = "Dr Meera clinic open 10 to 7 Mon-Sat, Sunday closed, visit anytime"
    r1 = rep(loaded, a["conversation_id"], canned, 2)
    r2 = rep(loaded, a["conversation_id"], canned, 3)
    assert r2["action"] in ("send", "wait", "end")
    r3 = rep(loaded, a["conversation_id"], canned, 4)
    assert r3["action"] in ("wait", "end")


def test_intent_transition_acts_immediately(loaded):
    a = start(loaded)
    r = rep(loaded, a["conversation_id"], "Ok lets do it. Whats next?", 3)
    assert r["action"] == "send"
    low = r["body"].lower()
    assert any(w in low for w in ["done", "sending", "draft", "here", "confirm", "proceed", "next"])
    assert not any(q in low for q in QUALIFYING)
    r2 = rep(loaded, a["conversation_id"], "CONFIRM", 4)
    assert r2["action"] == "send" and r2["cta"] == "none"


def test_intent_on_unknown_conversation(loaded):
    r = rep(loaded, "conv_intent_1", "Ok lets do it. Whats next?", 2)
    low = r["body"].lower()
    assert r["action"] == "send" and not any(q in low for q in QUALIFYING)
    assert any(w in low for w in ["done", "sending", "draft", "here", "confirm", "proceed", "next"])


def test_hostile_then_offtopic_stays_closed(loaded):
    a = start(loaded)
    r1 = rep(loaded, a["conversation_id"], "Why are you bothering me. This is useless. Stop sending these.", 2)
    assert r1["action"] == "end"
    r2 = rep(loaded, a["conversation_id"], "can you also help me file my GST?", 3)
    assert r2["action"] in ("end", "send")
    if r2["action"] == "send":
        assert "gst" not in r2["body"].lower() or "won't" in r2["body"].lower() or "nothing further" in r2["body"].lower()
    r3 = rep(loaded, a["conversation_id"], "hello?", 4)
    assert r3["action"] == "end"
    # merchant is suppressed for future proactive sends
    acts = loaded.post("/v1/tick", json={"now": "2026-04-26T11:00:00Z",
                                         "available_triggers": ["trg_002_compliance_dci_radiograph"]}).json()["actions"]
    assert acts == []


def test_off_topic_declined_and_redirected(loaded):
    a = start(loaded)
    r = rep(loaded, a["conversation_id"], "Btw can you also help me with my GST filing this month?", 2)
    assert r["action"] == "send"
    assert "CA" in r["body"] and "YES" in r["body"]


def test_no_verbatim_repeat_in_conversation(loaded):
    a = start(loaded)
    bodies = [a["body"]]
    for i, msg in enumerate(["hmm", "hmm", "ok hmm", "hmm"], start=2):
        r = rep(loaded, a["conversation_id"], msg, i)
        if r["action"] == "send":
            bodies.append(r["body"])
    assert len(bodies) == len(set(bodies))


def test_language_switch_mid_conversation(loaded):
    a = start(loaded)
    r = rep(loaded, a["conversation_id"], "haan theek hai, kar do", 2)
    assert r["action"] == "send" and re.search(r"\b(ho gaya|kijiye|raha|dungi|shuru)\b", r["body"], re.I)


def test_price_objection_is_respectful_then_accepts_no(loaded):
    acts = loaded.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": ["trg_005_renewal_due_bharat"]}).json()["actions"]
    conv = acts[0]["conversation_id"]
    r1 = rep(loaded, conv, "This is too expensive for me.", 2, mid="m_002_bharat_dentist_mumbai")
    assert r1["action"] == "send" and "no pressure" in r1["body"].lower() and "4 calls" in r1["body"]
    assert not re.search(r"hurry|last chance|only today", r1["body"], re.I)
    r2 = rep(loaded, conv, "Still too expensive.", 3, mid="m_002_bharat_dentist_mumbai")
    assert r2["action"] == "end"


def test_how_does_it_work_gets_a_real_explanation(loaded):
    a = start(loaded)
    r = rep(loaded, a["conversation_id"], "accha, ye kaise kaam karega?", 2)
    assert r["action"] == "send" and "nahi hai" not in r["body"] and re.search(r"live nahi hoga|OK ke bina", r["body"])


def test_later_waits(loaded):
    a = start(loaded)
    r = rep(loaded, a["conversation_id"], "Busy right now, call me tomorrow", 2)
    assert r["action"] == "wait" and r["wait_seconds"] >= 3600


def test_customer_slot_pick(loaded):
    acts = loaded.post("/v1/tick", json={"now": "2026-04-26T11:00:00Z", "available_triggers": ["trg_003_recall_due_priya"]}).json()["actions"]
    a = acts[0]
    assert a["send_as"] == "merchant_on_behalf" and a["cta"] == "multi_choice_slot"
    r = rep(loaded, a["conversation_id"], "2", 2, role="customer", cust=a["customer_id"])
    assert r["action"] == "send" and "Thu 6 Nov, 5pm" in r["body"]


def test_question_answered_from_context(loaded):
    a = start(loaded)
    r = rep(loaded, a["conversation_id"], "Where is this data from?", 2)
    assert r["action"] == "send" and "JIDA" in r["body"]


def test_max_turns_then_end(loaded):
    a = start(loaded)
    last = None
    for i in range(2, 12):
        last = rep(loaded, a["conversation_id"], f"interesting point number {i}, tell me more about the details", i)
        if last["action"] == "end":
            break
    assert last["action"] in ("end", "wait")
