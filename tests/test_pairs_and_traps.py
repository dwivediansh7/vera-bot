"""The 30 canonical pairs + the known data traps. Checks invariants, not exact wording."""

import re
from datetime import datetime, timezone

import pytest

from app.compose import build_candidate
from app.evidence import numeric_tokens
from app.store import Store
from app.validator import URL_RE, case_similarity
from tests.dataset import load_into

NOW = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def st(data):
    s = Store()
    load_into(s, data)
    return s


def _cand(st, data, tid, now=NOW):
    return build_candidate(st, data["trigger"][tid], now)


def raw_numbers(*objs) -> set[str]:
    """Every numeric token anywhere in the raw contexts (the outer bound for any message)."""
    import json
    out = set()
    for o in objs:
        if o:
            raw = json.dumps(o, ensure_ascii=False)
            out |= numeric_tokens(raw)
            for tok in re.findall(r"-?\d+\.\d+", raw):   # fractions are rendered as percentages
                x = abs(float(tok))
                if x <= 1.5:
                    out.add(str(int(round(x * 100))))
                    out |= numeric_tokens(f"{x * 100:.1f}")
    return out


def test_all_pairs_valid_or_justified(st, data):
    sent, abstained = 0, {}
    for p in data["pairs"]:
        c = _cand(st, data, p["trigger_id"])
        if not c.ok:
            abstained[p["test_id"]] = c.abstain_reason
            continue
        sent += 1
        b = c.body
        assert not URL_RE.search(b)
        assert case_similarity(b) < 0.30, p["test_id"]
        # CTA lands in the final sentence
        last = re.split(r"(?<=[.!?])\s+|\n", b.strip())[-1]
        assert re.search(r"\?|reply|confirm|yes|kijiye", last, re.I), (p["test_id"], last)
        # every number traces back to the raw contexts or to a code-derived fact
        derived = set()
        for f in c.ledger.facts.values():
            if f.role == "DERIVED":
                derived |= f.numbers
        allowed = raw_numbers(c.category, c.merchant, c.trigger, c.customer) | derived
        extra = numeric_tokens(b) - allowed
        assert not extra, (p["test_id"], extra, b)
        # rationale mentions the angle actually used
        assert c.card["angle"] and c.card["evidence"], p["test_id"]
    assert sent >= 27, abstained
    assert set(abstained) <= {"T08"}, abstained


def test_trap_refill_for_dentist_abstains(st, data):
    c = _cand(st, data, "trg_081_chronic_refill_due_m_011_dr_sameer_dent")
    assert not c.ok and c.abstain_reason.startswith("kind_category_mismatch")


def test_trap_perf_dip_with_rising_metrics_is_not_called_a_dip(st, data):
    c = _cand(st, data, "trg_031_perf_dip_m_023_sushma_salon_p")
    assert c.ok and c.assessment.effective_kind == "perf_check"
    assert not re.search(r"\b(down|dropped|dip(ped)? by|decline)\b", c.body.split("nothing is dipping")[0], re.I)
    assert "up 8%" in c.body


def test_trap_recall_for_gym_is_reframed_without_clinical_language(st, data):
    c = _cand(st, data, "trg_066_recall_due_m_008_zenyoga_gym_ch")
    assert c.ok and c.assessment.reframed
    assert "recall" not in c.body.lower() and c.card["send_as"] == "merchant_on_behalf"


def test_expired_triggers_not_dropped_by_wall_clock(st, data):
    c = _cand(st, data, "trg_001_research_digest_dentists")
    assert c.ok and c.assessment.expired  # expired vs 2026-09 clock, still sent
    assert "2,100" in c.body or "2100" in c.body


def test_future_publication_dates_dropped_from_sources():
    from datetime import date
    from app.playbooks import clean_source
    ref = date(2026, 9, 26)
    assert clean_source("Dental Council of India circular 2026-11-04", ref) == "Dental Council of India circular"
    assert clean_source("JIDA Oct 2026, p.14", ref) == "JIDA, p.14"
    assert clean_source("Swiggy partner blog 2026-04-12", ref) == "Swiggy partner blog 2026-04-12"   # past: kept
    assert clean_source("Google Trends Apr 2026", ref) == "Google Trends Apr 2026"
    assert clean_source("JIDA Oct 2026, p.14", None) == "JIDA Oct 2026, p.14"                          # no clock: untouched


def test_regulation_message_has_no_future_circular_date(st, data):
    c = _cand(st, data, "trg_002_compliance_dci_radiograph")
    assert c.ok and "2026-11-04" not in c.body
    assert "2026-12-15" in c.body and "1.0 mSv" in c.body          # the deadline and the rule itself stay
    later = build_candidate(st, {**data["trigger"]["trg_002_compliance_dci_radiograph"], "suppression_key": "x2"},
                            datetime(2026, 11, 20, tzinfo=timezone.utc))
    assert "2026-11-04" in later.body                                 # once issued, the date is cited


def test_placeholder_triggers_still_specific(st, data):
    for p in data["pairs"]:
        t = data["trigger"][p["trigger_id"]]
        if not t["payload"].get("placeholder"):
            continue
        c = _cand(st, data, p["trigger_id"])
        if c.ok:
            assert re.search(r"\d", c.body) or c.assessment.audience == "customer" or c.assessment.kind == "curious_ask_due", (p["test_id"], c.body)


def test_owner_name_and_dr_prefix(st, data):
    c = _cand(st, data, "trg_004_perf_dip_bharat")
    assert c.body.startswith("Dr. Bharat")
    c = _cand(st, data, "trg_031_perf_dip_m_023_sushma_salon_p")
    assert c.body.startswith("Sushma")
    for tid, t in data["trigger"].items():  # generated dentists store 'Dr. X' as first name: never 'Dr. Dr.'
        c = _cand(st, data, tid)
        if c.ok:
            assert "Dr. Dr." not in c.body


def test_no_consent_customer_is_skipped(data):
    s = Store()
    load_into(s, data)
    trig = {"id": "trg_x", "scope": "customer", "kind": "customer_lapsed_soft", "merchant_id": "m_010_sunrisepharm_pharmacy_lucknow",
            "customer_id": "c_015_anonymous_for_m010", "payload": {}, "urgency": 3, "suppression_key": "x"}
    c = build_candidate(s, trig, NOW)
    assert not c.ok and c.abstain_reason in ("no_reachable_phone", "no_customer_consent", "no_reachable_channel")


def test_customer_language_honoured(st, data):
    priya = _cand(st, data, "trg_003_recall_due_priya")          # hi-en mix
    assert re.search(r"\b(aapki|hain|kijiye|ya)\b", priya.body, re.I)
    sharma = _cand(st, data, "trg_019_chronic_refill_grandfather")  # hi
    assert re.search(r"\b(namaste|ji|kijiye)\b", sharma.body, re.I)
    rashmi = _cand(st, data, "trg_015_winback_rashmi")            # english
    assert not re.search(r"\b(kijiye|aapki|hain)\b", rashmi.body, re.I)


def test_determinism(data):
    outs = []
    for _ in range(2):
        s = Store()
        load_into(s, data)
        outs.append([(p["test_id"], _cand(s, data, p["trigger_id"]).body) for p in data["pairs"]])
    assert outs[0] == outs[1]
