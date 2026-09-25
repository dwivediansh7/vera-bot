"""LLM wording must never be able to smuggle in fabrications, and failures must fall back cleanly."""

import asyncio
from datetime import datetime, timezone

from app import llm_writer
from app.compose import build_candidate
from app.config import settings
from app.store import Store
from tests.dataset import load_into

NOW = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)


class FakeLLM:
    """Stands in for the Gemini client. Single-item outputs are wrapped into the batch shape."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.enabled = True
        self.calls = 0
        self.stats = {"last_error": None}

    async def complete_json(self, system, user, timeout=None, **kw):
        self.calls += 1
        if not self.outputs:
            return None
        o = self.outputs.pop(0)
        if isinstance(o, Exception):
            raise o
        if isinstance(o, dict) and "body" in o and "items" not in o and '"items"' in user:
            return {"items": [{"id": "0", "body": o["body"]}]}
        return o


def cand(data, tid="trg_001_research_digest_dentists"):
    s = Store()
    load_into(s, data)
    return build_candidate(s, data["trigger"][tid], NOW)


def run(monkeypatch, data, outputs, tid="trg_001_research_digest_dentists"):
    c = cand(data, tid)
    monkeypatch.setattr(llm_writer, "llm", FakeLLM(outputs))
    return c, asyncio.run(llm_writer.polish(c, set(), 2.0))


def test_fabricated_number_rejected(monkeypatch, data):
    c = cand(data)
    c, out = run(monkeypatch, data, [{"body": f"Dr. Meera, 91% of clinics saw 4,000 new patients. {c.draft.cta}"}])
    assert out is None


def test_url_and_changed_cta_rejected(monkeypatch, data):
    _, out = run(monkeypatch, data, [{"body": "Dr. Meera, read https://jida.in/x now. Want it? Reply YES."}])
    assert out is None
    _, out = run(monkeypatch, data, [{"body": "Dr. Meera, JIDA Oct 2026 has a fluoride item. Call me."}])
    assert out is None


def test_good_wording_accepted(monkeypatch, data):
    c = cand(data)
    good = {"body": f"Dr. Meera, JIDA Oct 2026, p.14 is worth two minutes: a multi-center Indian trial (n=2,100) found 3-month "
                    f"fluoride varnish recall gave 38% lower caries recurrence than 6-month in high-risk adults, and you have "
                    f"124 high-risk adult patients. {c.draft.cta}"}
    _, out = run(monkeypatch, data, [good])
    assert out == good["body"]


def test_rewrite_that_drops_facts_is_rejected(monkeypatch, data):
    c = cand(data)
    thin = {"body": f"Dr. Meera, JIDA has a new fluoride recall study worth reading. {c.draft.cta}"}
    _, out = run(monkeypatch, data, [thin])
    assert out is None


def test_llm_exception_or_empty_falls_back(monkeypatch, data):
    _, out = run(monkeypatch, data, [RuntimeError("boom")])
    assert out is None
    _, out = run(monkeypatch, data, [{"nope": 1}])
    assert out is None


def test_batch_is_one_call_and_validates_each_item(monkeypatch, data):
    s = Store()
    load_into(s, data)
    cs = [build_candidate(s, data["trigger"][t], NOW) for t in
          ("trg_001_research_digest_dentists", "trg_024_perf_spike_zen", "trg_004_perf_dip_bharat")]
    good0 = (f"Dr. Meera, JIDA Oct 2026, p.14: in a multi-center Indian trial (n=2,100), 3-month fluoride varnish recall gave 38% lower "
             f"caries recurrence than 6-month; that's your 124 high-risk adult patients. {cs[0].draft.cta}")
    bad1 = f"Padma, calls are up 99% thanks to magic. {cs[1].draft.cta}"
    fake = FakeLLM([{"items": [{"id": "0", "body": good0}, {"id": "1", "body": bad1}]}])
    monkeypatch.setattr(llm_writer, "llm", fake)
    acc, reasons = asyncio.run(llm_writer.polish_batch(cs, [set()] * 3, 2.0))
    assert fake.calls == 1
    assert acc == {0: good0}
    assert reasons[1].startswith(("validator_rejected", "dropped_facts")) and reasons[2] == "missing_in_batch"
    assert "s 18 calls" not in reasons[1]   # apostrophes in contractions are not quoted titles


def test_dropped_facts_guard():
    from app.llm_writer import dropped_facts
    t = "Suresh, your 'Buy 1 Pizza Get 1 Free (Tue-Thu)' doesn't run on weekends. That's 65% of orders. Reply YES."
    assert dropped_facts(t, "Suresh, your 'Buy 1 Pizza Get 1 Free (Tue-Thu)' is weekday-only; 65% of orders are delivery. Reply YES.") == set()
    assert dropped_facts(t, "Suresh, 65% of orders are delivery. Reply YES.")          # lost the offer title
    assert dropped_facts(t, "Suresh, your 'Buy 1 Pizza Get 1 Free (Tue-Thu)' is weekday-only. Reply YES.")  # lost 65


def test_reply_wording_guarded(monkeypatch, data):
    from app.evidence import Ledger
    body = "Dr. Meera, on it: I'll draft the Google post leading with your review strength and share it here before anything goes live. Reply CONFIRM to approve."
    ok = "Dr. Meera, starting now: I'll draft the Google post around your review strength and share it before it goes live. Reply CONFIRM to approve."
    monkeypatch.setattr(llm_writer, "llm", FakeLLM([{"body": ok}]))
    new, why = asyncio.run(llm_writer.polish_reply(body, lang="en", ledger=Ledger(), audience="merchant", cta="binary_confirm_cancel",
                                                   prior=set(), taboo=[], commitment=True, timeout=2))
    assert new == ok and why == "accepted"
    qual = "Dr. Meera, would you like a post? Reply CONFIRM to approve."
    monkeypatch.setattr(llm_writer, "llm", FakeLLM([{"body": qual}]))
    new, why = asyncio.run(llm_writer.polish_reply(body, lang="en", ledger=Ledger(), audience="merchant", cta="binary_confirm_cancel",
                                                   prior=set(), taboo=[], commitment=True, timeout=2))
    assert new is None and why.startswith("validator_rejected")


def test_llm_disabled_by_default_in_tests():
    assert settings.llm_enabled is False


def test_outbound_is_template_only_by_default(monkeypatch, loaded):
    import app.main as main

    class On:
        enabled = True

    called = []

    async def spy(cands, priors, timeout):
        called.append(1)
        return {}, {}

    monkeypatch.setattr(main, "llm", On())
    monkeypatch.setattr(main, "polish_batch", spy)
    acts = loaded.post("/v1/tick", json={"now": "2026-09-26T10:00:00Z",
                                         "available_triggers": ["trg_001_research_digest_dentists"]}).json()["actions"]
    assert len(acts) == 1 and called == []          # LLM_OUTBOUND=off -> no LLM on outbound
    monkeypatch.setattr(settings, "llm_outbound", True)
    loaded.post("/v1/tick", json={"now": "2026-09-26T10:00:00Z", "available_triggers": ["trg_024_perf_spike_zen"]})
    assert called == [1]                             # flag re-enables the batched path


def test_llm_cache_persists_across_restart(monkeypatch, tmp_path):
    from app import llm as llm_mod
    path = tmp_path / "cache.json"
    monkeypatch.setattr(settings, "llm_cache_path", str(path))
    c1 = llm_mod.LLMClient()
    c1._cache["k1"] = {"body": "hello"}
    c1._save_cache()
    c2 = llm_mod.LLMClient()                          # simulated restart
    assert c2._cache == {"k1": {"body": "hello"}}


def test_hanging_llm_cannot_blow_the_tick_budget(monkeypatch, loaded):
    import time

    import app.main as main

    class On:
        enabled = True

    async def hang(cands, priors, timeout):
        await asyncio.sleep(60)

    monkeypatch.setattr(main, "llm", On())
    monkeypatch.setattr(main, "polish_batch", hang)
    monkeypatch.setattr(settings, "llm_outbound", True)
    monkeypatch.setattr(settings, "tick_budget_s", 3.0)
    monkeypatch.setattr(settings, "llm_timeout_s", 2.0)
    t0 = time.time()
    acts = loaded.post("/v1/tick", json={"now": "2026-09-26T10:00:00Z",
                                         "available_triggers": ["trg_001_research_digest_dentists", "trg_024_perf_spike_zen"]}).json()["actions"]
    assert time.time() - t0 < 4.5
    assert len(acts) == 2 and all(a["body"].strip() for a in acts)
