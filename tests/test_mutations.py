"""Context-mutation fortress: change the evidence, the message must follow it and never keep stale facts."""

import copy
import re
from datetime import datetime, timezone

from app.compose import build_candidate
from app.store import Store
from tests.dataset import load_into

NOW = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)


def fresh(data):
    s = Store()
    load_into(s, data)
    return s


def put(s, scope, cid, payload, v=2):
    ok, _ = s.put_context(scope, cid, v, payload)
    assert ok


def body_for(s, data_or_trig, tid=None):
    trig = data_or_trig if tid is None else data_or_trig["trigger"][tid]
    return build_candidate(s, trig, NOW)


def test_changed_performance_numbers_flow_through(data):
    s = fresh(data)
    m = copy.deepcopy(data["merchant"]["m_002_bharat_dentist_mumbai"])
    m["performance"].update({"calls": 7, "views": 1234})
    m["performance"]["delta_7d"]["calls_pct"] = -0.42
    put(s, "merchant", m["merchant_id"], m)
    t = copy.deepcopy(data["trigger"]["trg_004_perf_dip_bharat"])
    t["payload"]["delta_pct"] = -0.42
    c = body_for(s, t)
    assert c.ok and "42%" in c.body and "7 calls" in c.body
    assert "50%" not in c.body and "4 calls" not in c.body


def test_dip_trigger_but_metrics_improved_becomes_honest(data):
    s = fresh(data)
    m = copy.deepcopy(data["merchant"]["m_002_bharat_dentist_mumbai"])
    m["performance"]["delta_7d"] = {"views_pct": 0.12, "calls_pct": 0.09}
    put(s, "merchant", m["merchant_id"], m)
    c = body_for(s, data, "trg_004_perf_dip_bharat")
    assert c.ok and c.assessment.contradiction and c.assessment.effective_kind == "perf_check"
    assert "50%" not in c.body and "up 9%" in c.body


def test_spike_trigger_but_metrics_fell_abstains(data):
    s = fresh(data)
    m = copy.deepcopy(data["merchant"]["m_008_zenyoga_gym_chennai"])
    m["performance"]["delta_7d"] = {"views_pct": -0.2, "calls_pct": -0.1}
    put(s, "merchant", m["merchant_id"], m)
    c = body_for(s, data, "trg_024_perf_spike_zen")
    assert not c.ok and "contradiction" in c.abstain_reason


def test_new_digest_item_is_cited(data):
    s = fresh(data)
    cat = copy.deepcopy(data["category"]["dentists"])
    cat["digest"].append({"id": "d_new_x", "kind": "research", "title": "Silver diamine fluoride halts 81% of early lesions in 18-month trial",
                          "source": "IJDR Aug 2026", "trial_n": 640, "summary": "Arrest rate 81% vs 34% for placebo varnish in children aged 3-6."})
    put(s, "category", "dentists", cat)
    t = {"id": "trg_new", "scope": "merchant", "kind": "research_digest", "merchant_id": "m_001_drmeera_dentist_delhi",
         "payload": {"category": "dentists", "top_item_id": "d_new_x"}, "urgency": 2, "suppression_key": "new"}
    c = body_for(s, t)
    assert c.ok and "IJDR Aug 2026" in c.body and "81%" in c.body and "640" in c.body
    assert "JIDA" not in c.body


def test_changed_offer_price_used_verbatim(data):
    s = fresh(data)
    m = copy.deepcopy(data["merchant"]["m_001_drmeera_dentist_delhi"])
    m["offers"][0]["title"] = "Dental Cleaning @ ₹349"
    put(s, "merchant", m["merchant_id"], m)
    c = body_for(s, data, "trg_023_competitor_opened_dentist")
    assert c.ok and "₹349" in c.body and "₹150" in c.body and "₹299" not in c.body


def test_changed_dates_in_payload(data):
    s = fresh(data)
    t = copy.deepcopy(data["trigger"]["trg_003_recall_due_priya"])
    t["payload"]["due_date"] = "2026-12-01"
    t["payload"]["available_slots"] = [{"iso": "2026-11-20T18:00:00+05:30", "label": "Fri 20 Nov, 6pm"}]
    t["suppression_key"] = "recall:mut"
    c = body_for(s, t)
    assert c.ok and "1 Dec" in c.body and "20 Nov" in c.body and "12 Nov" not in c.body and "5 Nov" not in c.body


def test_changed_category_changes_voice(data):
    s = fresh(data)
    m = copy.deepcopy(data["merchant"]["m_002_bharat_dentist_mumbai"])
    m["category_slug"] = "salons"
    put(s, "merchant", m["merchant_id"], m)
    c = body_for(s, data, "trg_004_perf_dip_bharat")
    assert c.ok and c.body.startswith("Bharat,") and "Dr. Bharat" not in c.body and "Haircut" in c.body


def test_changed_customer_name_and_language(data):
    s = fresh(data)
    cu = copy.deepcopy(data["customer"]["c_010_rashmi_for_m007"])
    cu["identity"].update({"name": "Farah", "language_pref": "hi"})
    put(s, "customer", cu["customer_id"], cu)
    c = body_for(s, data, "trg_015_winback_rashmi")
    assert c.ok and "Farah" in c.body and "Rashmi" not in c.body and re.search(r"\b(ji|kijiye|hai)\b", c.body)


def test_empty_fields_do_not_crash_or_fabricate(data):
    s = Store()
    s.put_context("category", "gyms", 1, {"slug": "gyms"})
    s.put_context("merchant", "m_bare", 1, {"merchant_id": "m_bare", "category_slug": "gyms", "identity": {"name": "Bare Gym"}})
    for kind in ["perf_dip", "perf_spike", "research_digest", "festival_upcoming", "milestone_reached", "competitor_opened",
                 "curious_ask_due", "renewal_due", "totally_new_kind", "regulation_change", "ipl_match_today"]:
        t = {"id": f"t_{kind}", "kind": kind, "merchant_id": "m_bare", "payload": {}, "urgency": 3, "suppression_key": kind}
        c = build_candidate(s, t, NOW)
        if c.ok:
            assert not re.search(r"\d", c.body), (kind, c.body)   # there are no numbers to use
        else:
            assert c.abstain_reason


def test_missing_merchant_or_category_abstains_without_burning_suppression(data):
    s = Store()
    t = copy.deepcopy(data["trigger"]["trg_001_research_digest_dentists"])
    c = build_candidate(s, t, NOW)
    assert not c.ok and c.assessment.retry_later


def test_unknown_trigger_kind_uses_verified_gap(data):
    s = fresh(data)
    t = {"id": "t_odd", "kind": "weather_heatwave", "merchant_id": "m_010_sunrisepharm_pharmacy_lucknow",
         "payload": {"temp_c": 44}, "urgency": 2, "suppression_key": "odd"}
    c = build_candidate(s, t, NOW)
    assert c.ok and "Vikas" in c.body and "44" not in c.body
