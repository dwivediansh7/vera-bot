"""Template-quality, history, rationale and customer-reminder guarantees (audit fixes)."""

import copy
import re
from datetime import datetime, timezone

import pytest

from app.compose import build_candidate, evidence_in_body, rationale
from app.evidence import Ledger, numeric_tokens
from app.store import Store
from app.validator import validate
from tests.dataset import load_into

NOW = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def all_cands(data):
    s = Store()
    load_into(s, data)
    return {tid: build_candidate(s, t, NOW) for tid, t in data["trigger"].items()}


def test_no_signed_minus_patterns_or_raw_notes(all_cands, data):
    raw_notes = {b["note"] for c in data["category"].values() for b in c.get("seasonal_beats", [])}
    for tid, c in all_cands.items():
        if not c.ok:
            continue
        assert not re.search(r"(down|dropped|dipped)\s+-\d", c.body), (tid, c.body)
        assert not re.search(r"\s-\d+%", c.body), (tid, c.body)            # deltas are spoken: "down 30%"
        for note in raw_notes:
            assert note not in c.body, (tid, note)                          # seasonal notes are rephrased


def test_bodies_short_where_possible(all_cands):
    lens = sorted((len(c.body), tid) for tid, c in all_cands.items() if c.ok and "\n" not in c.body)
    over = [x for x in lens if x[0] > 400]
    assert not over, over
    assert sum(1 for L, _ in lens if L <= 350) / len(lens) >= 0.8


def test_day_counts_only_when_consistent(all_cands):
    t18 = all_cands["trg_006_festival_diwali"]
    assert "31 Oct" in t18.body and "188" not in t18.body                  # 188 disagrees with a Sep 26 'now'
    kavya = all_cands["trg_007_bridal_followup_kavya"]
    assert "8 Nov" in kavya.body and "196" not in kavya.body
    rashmi = all_cands["trg_015_winback_rashmi"]
    assert "57 days" not in rashmi.body and "28 Feb" in rashmi.body


def test_day_count_kept_when_consistent(data):
    s = Store()
    load_into(s, data)
    c = build_candidate(s, data["trigger"]["trg_006_festival_diwali"], datetime(2026, 4, 26, tzinfo=timezone.utc))
    assert "188 days" in c.body                                              # consistent with the dataset's own week


def test_rationale_only_cites_what_body_says(all_cands):
    for tid, c in all_cands.items():
        if not c.ok:
            continue
        for item in evidence_in_body(c):
            toks = numeric_tokens(item)
            assert item.lower() in c.body.lower() or (toks and toks <= numeric_tokens(c.body)), (tid, item)
        r = rationale(c)
        ev = re.search(r"Grounded in: (.*?)\. (One CTA|Data check)", r)
        if ev:
            for item in ev.group(1).split("; "):
                toks = numeric_tokens(item)
                assert not toks or toks <= numeric_tokens(c.body), (tid, item)


def test_history_accepted_request_is_delivered_not_reasked(all_cands):
    c = all_cands["trg_018_supply_atorvastatin_recall"]      # m_009 already said "Yes send me the list"
    assert c.ok and c.draft.cta_type == "binary_confirm_cancel"
    assert "kar doon?" not in c.body and "Want me to" not in c.body
    assert "CONFIRM" in c.body and "Customer note draft" in c.body


def test_history_pending_request_is_picked_up(all_cands):
    c = all_cands["trg_023_competitor_opened_dentist"]       # m_001 asked for whitening + aligner posts
    assert re.search(r"whitening (and|aur) aligners", c.body)


def test_never_repeats_last_vera_message(data):
    s = Store()
    load_into(s, data)
    first = build_candidate(s, data["trigger"]["trg_024_perf_spike_zen"], NOW)
    m = copy.deepcopy(data["merchant"]["m_008_zenyoga_gym_chennai"])
    m["conversation_history"].append({"ts": "2026-09-25T10:00:00Z", "from": "vera", "body": first.body})
    s.put_context("merchant", m["merchant_id"], 2, m)
    again = build_candidate(s, data["trigger"]["trg_024_perf_spike_zen"], NOW)
    assert not again.ok or again.body != first.body


def test_thin_customer_reminders_have_a_concrete_fact(all_cands, data):
    for tid in ("trg_076_appointment_tomorrow_m_019_karim_salon_lu", "trg_077_appointment_tomorrow_m_020_renu_salon_luc",
                "trg_072_customer_lapsed_soft_m_049_komal_pharmaci"):
        c = all_cands[tid]
        m = data["merchant"][c.trigger["merchant_id"]]
        assert c.ok and (m["identity"]["locality"] in c.body or re.search(r"\d|₹", c.body)), (tid, c.body)
        assert not re.search(r"\d{1,2}(:\d{2})?\s?(am|pm)", c.body)           # never an invented time


def test_recall_with_slots_works_for_any_category(data):
    s = Store()
    load_into(s, data)
    s.put_context("customer", "c_x", 1, {"customer_id": "c_x", "merchant_id": "m_003_studio11_salon_hyderabad",
                                         "identity": {"name": "Ria", "language_pref": "english"}, "state": "active",
                                         "relationship": {"last_visit": "2026-06-10"}, "preferences": {"reminder_opt_in": True},
                                         "consent": {"opted_in_at": "2026-01-01", "scope": ["recall_reminders"]}})
    t = {"id": "t_x", "scope": "customer", "kind": "recall_due", "merchant_id": "m_003_studio11_salon_hyderabad", "customer_id": "c_x",
         "payload": {"service_due": "hair_spa", "last_service_date": "2026-06-10",
                     "available_slots": [{"iso": "2026-10-02T17:00:00+05:30", "label": "Fri 2 Oct, 5pm"}]},
         "urgency": 3, "suppression_key": "x"}
    c = build_candidate(s, t, NOW)
    assert c.ok and c.assessment.effective_kind == "recall_due"
    assert "Fri 2 Oct, 5pm" in c.body and "hair spa" in c.body.lower() and "recall" not in c.body.lower()


def test_misattribution_guard():
    L = Ledger()
    L.add("c.trend.0", "CATEGORY", "trend", {}, "searches for 'weekday lunch thali' are up 34% year-on-year")
    bad = "Suresh, your searches for 'weekday lunch thali' are up 34% year-on-year. Reply YES."
    good = "Suresh, across metros, searches for 'weekday lunch thali' are up 34% year-on-year. Reply YES."
    kw = dict(L=L, audience="merchant", send_as="vera", cta_type="binary_yes_no", taboo=[], expected_names=[], prior_bodies=set())
    assert any(p.code == "misattribution" for p in validate(bad, **kw))
    assert not any(p.code == "misattribution" for p in validate(good, **kw))


def test_t01_tiers_are_explicit_suggestions(all_cands):
    b = all_cands["trg_013_corporate_thali_planning"].body
    assert "suggest" in b.lower() and "₹149" in b and "10%" in b and "15%" in b and "% off" not in b


def test_cap_keeps_highest_urgency_and_defers_the_rest(loaded, data):
    acts = loaded.post("/v1/tick", json={"now": "2026-09-26T10:00:00Z", "available_triggers": list(data["trigger"])}).json()["actions"]
    assert len(acts) == 20
    sent_urg = sorted(data["trigger"][a["trigger_id"]].get("urgency", 0) for a in acts)
    from app.store import store
    deferred = [d["trigger_id"] for d in store.decision_log if d["outcome"].startswith("deferred_by_cap")]
    assert deferred, "cap drops must be logged"
    assert min(sent_urg) >= max(data["trigger"][t].get("urgency", 0) for t in deferred)
    # everything deferred by the cap goes out on the following ticks
    acts2 = loaded.post("/v1/tick", json={"now": "2026-09-26T10:05:00Z", "available_triggers": list(data["trigger"])}).json()["actions"]
    assert acts2, "deferred triggers must still be sendable next tick"
    assert min(sent_urg) >= max(data["trigger"][a["trigger_id"]].get("urgency", 0) for a in acts2) - 3
