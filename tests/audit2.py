"""Audit part 2: 8.4 examples, 8.5 determinism, 8.7 latency/load against BOT_URL."""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
import statistics
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.dataset import ROOT, load_all  # noqa: E402

BOT = os.environ.get("BOT_URL", "http://127.0.0.1:8080").rstrip("/")
C = httpx.Client(base_url=BOT, timeout=35)
MODE = sys.argv[1] if len(sys.argv) > 1 else "all"


def P(path, body):
    r = C.post(path, json=body)
    return r.status_code, r.json()


def ctx(scope, cid, v, payload):
    return P("/v1/context", {"scope": scope, "context_id": cid, "version": v, "payload": payload, "delivered_at": "2026-04-26T09:45:00Z"})


def examples():
    out = []
    C.post("/v1/teardown")
    h = C.get("/v1/healthz").json()
    out.append(("1.1 healthz fresh", h["contexts_loaded"] == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}, h["contexts_loaded"]))
    m = C.get("/v1/metadata").json()
    out.append(("1.2 metadata keys", set(m) >= {"team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"}, list(m)))
    cat = {"slug": "dentists", "voice": {"tone": "peer_clinical", "vocab_taboo": ["guaranteed", "100% safe"]},
           "offer_catalog": [{"id": "den_001", "title": "Dental Cleaning @ ₹299", "value": "299", "audience": "new_user", "type": "service_at_price"}],
           "peer_stats": {"avg_rating": 4.4, "avg_ctr": 0.030},
           "digest": [{"id": "d_2026W17_jida_fluoride", "kind": "research", "title": "3-month fluoride recall cuts caries 38% better", "source": "JIDA Oct 2026, p.14"}],
           "patient_content_library": [], "seasonal_beats": [{"month_range": "Nov-Feb", "note": "exam-stress bruxism spike"}],
           "trend_signals": [{"query": "clear aligners delhi", "delta_yoy": 0.62}]}
    s, b = ctx("category", "dentists", 1, cat)
    out.append(("1.3 category push (abbreviated)", s == 200 and b.get("ack_id") and b.get("stored_at"), b))
    mer = {"merchant_id": "m_001_drmeera_dentist_delhi", "category_slug": "dentists",
           "identity": {"name": "Dr. Meera's Dental Clinic", "city": "Delhi", "locality": "Lajpat Nagar", "verified": True, "languages": ["en", "hi"], "owner_first_name": "Meera"},
           "subscription": {"status": "active", "plan": "Pro", "days_remaining": 82},
           "performance": {"window_days": 30, "views": 2410, "calls": 18, "directions": 45, "ctr": 0.021, "delta_7d": {"views_pct": 0.18, "calls_pct": -0.05}},
           "offers": [{"id": "o_meera_001", "title": "Dental Cleaning @ ₹299", "status": "active"}], "conversation_history": [],
           "customer_aggregate": {"total_unique_ytd": 540, "lapsed_180d_plus": 78, "retention_6mo_pct": 0.38, "high_risk_adult_count": 124},
           "signals": ["stale_posts:22d", "ctr_below_peer_median", "high_risk_adult_cohort"]}
    s, b = ctx("merchant", mer["merchant_id"], 1, mer)
    out.append(("1.4 merchant push", s == 200, b))
    s, b = ctx("merchant", mer["merchant_id"], 1, mer)
    out.append(("1.5 same version -> 409", s == 409 and b == {"accepted": False, "reason": "stale_version", "current_version": 1}, b))
    mer2 = json.loads(json.dumps(mer))
    mer2["performance"]["views"] = 2580
    s, b = ctx("merchant", mer["merchant_id"], 2, mer2)
    out.append(("1.6 version bump", s == 200, b))
    h = C.get("/v1/healthz").json()
    out.append(("1.7 healthz counts", h["contexts_loaded"]["merchant"] == 1 and h["contexts_loaded"]["category"] == 1, h["contexts_loaded"]))
    trg = {"id": "trg_001_research_digest_dentists", "scope": "merchant", "kind": "research_digest", "source": "external",
           "merchant_id": "m_001_drmeera_dentist_delhi", "customer_id": None, "payload": {"category": "dentists", "top_item_id": "d_2026W17_jida_fluoride"},
           "urgency": 2, "suppression_key": "research:dentists:2026-W17", "expires_at": "2026-05-03T00:00:00Z"}
    s, b = ctx("trigger", trg["id"], 1, trg)
    out.append(("2.1 trigger push", s == 200, b))
    s, b = P("/v1/tick", {"now": "2026-04-26T10:35:00Z", "available_triggers": [trg["id"]]})
    a = (b.get("actions") or [{}])[0]
    fields = {"conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name", "template_params", "body", "cta", "suppression_key", "rationale"}
    out.append(("2.2 tick sends (shape)", s == 200 and fields <= set(a) and a.get("suppression_key") == "research:dentists:2026-W17", a.get("body", "")[:230]))
    s, b = P("/v1/tick", {"now": "2026-04-26T10:40:00Z", "available_triggers": [trg["id"]]})
    out.append(("2.3 nothing to send -> []", b == {"actions": []}, b))
    conv = a.get("conversation_id", "c")
    base = {"conversation_id": conv, "merchant_id": mer["merchant_id"], "customer_id": None, "from_role": "merchant", "received_at": "2026-04-26T10:42:00Z", "turn_number": 2}
    s, b = P("/v1/reply", {**base, "message": "Yes please send the abstract. Also draft the patient WhatsApp."})
    out.append(("2.4 engaged reply -> send w/ action", b.get("action") == "send" and "draft" in b.get("body", "").lower(), b.get("body", "")[:260]))
    s, b = P("/v1/reply", {"conversation_id": "conv_ex_auto", "from_role": "merchant", "message": "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly.", "received_at": "2026-04-26T10:42:00Z", "turn_number": 2})
    out.append(("2.5 auto-reply (example shows wait; send-once is the 4.1 pattern)", b.get("action") in ("wait", "send"), b))
    s, b = P("/v1/reply", {"conversation_id": "conv_ex_no", "from_role": "merchant", "message": "Not interested. Stop messaging me.", "received_at": "2026-04-26T10:42:00Z", "turn_number": 2})
    out.append(("2.6 hard no -> end", b.get("action") == "end", b))
    s, b = P("/v1/reply", {**base, "conversation_id": "conv_ex_gst", "message": "Btw can you also help me with my GST filing this month?"})
    out.append(("2.7 curveball -> decline + steer", b.get("action") == "send" and "CA" in b.get("body", ""), b.get("body", "")[:200]))
    cat2 = {"slug": "dentists", "voice": {"tone": "peer_clinical"}, "digest": [
        {"id": "d_2026W17_jida_fluoride", "kind": "research", "title": "3-month fluoride recall cuts caries 38% better", "source": "JIDA Oct 2026, p.14"},
        {"id": "d_2026W17_dci_radiograph_NEW", "kind": "compliance", "title": "DCI revised radiograph dose limits effective 2026-12-15",
         "source": "DCI circular 2026-11-04", "summary": "Max dose drops 1.5→1.0 mSv per IOPA. E-speed film passes; D-speed does not."}], "// other fields": "..."}
    s, b = ctx("category", "dentists", 2, cat2)
    out.append(("2.8 category v2 injection", s == 200, b))
    trg2 = {"id": "trg_ex_dci", "scope": "merchant", "kind": "regulation_change", "merchant_id": mer["merchant_id"],
            "payload": {"top_item_id": "d_2026W17_dci_radiograph_NEW", "deadline_iso": "2026-12-15"}, "urgency": 4, "suppression_key": "ex:dci"}
    ctx("trigger", trg2["id"], 1, trg2)
    s, b = P("/v1/tick", {"now": "2026-04-26T10:55:00Z", "available_triggers": [trg2["id"]]})
    body = (b.get("actions") or [{}])[0].get("body", "")
    out.append(("2.8b next send uses new item", "1.0 mSv" in body or "radiograph" in body, body[:220]))
    ds = load_all()
    ctx("customer", "c_001_priya_for_m001", 1, ds["customer"]["c_001_priya_for_m001"])
    ctx("trigger", "trg_003_recall_due_priya", 1, ds["trigger"]["trg_003_recall_due_priya"])
    s, b = P("/v1/tick", {"now": "2026-04-26T11:00:00Z", "available_triggers": ["trg_003_recall_due_priya"]})
    a = (b.get("actions") or [{}])[0]
    out.append(("2.9 customer recall", a.get("send_as") == "merchant_on_behalf" and a.get("cta") == "multi_choice_slot" and a.get("customer_id") == "c_001_priya_for_m001",
                a.get("body", "")[:230]))
    raw = C.post("/v1/context", content=open(ROOT / "dataset" / "categories" / "dentists.json", "rb").read(), headers={"content-type": "application/json"})
    out.append(("curl sample: -d @dentists.json (raw category, no envelope)", raw.status_code in (200, 400), f"{raw.status_code} {raw.text[:120]}"))
    for name, ok, ev in out:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {json.dumps(ev, ensure_ascii=False)[:260] if not isinstance(ev, str) else ev}")


def determinism():
    d = load_all()
    res = []
    for run in range(2):
        C.post("/v1/teardown")
        for s in ("category", "merchant", "customer", "trigger"):
            for cid, p in d[s].items():
                ctx(s, cid, 1, p)
        b = C.post("/v1/tick", json={"now": "2026-09-26T10:00:00Z", "available_triggers": [
            "trg_023_competitor_opened_dentist", "trg_010_ipl_match_delhi", "trg_031_perf_dip_m_023_sushma_salon_p"]}).content
        res.append(b)
        time.sleep(2)
    dec = C.get("/v1/debug/decisions", params={"limit": 10}).json()
    print("byte-identical:", res[0] == res[1], "| len", len(res[0]), len(res[1]), "| llm", {k: dec["llm"].get(k) for k in ("calls", "ok", "cache_hits", "timeouts")})
    if res[0] != res[1]:
        a0 = json.loads(res[0])["actions"]
        a1 = json.loads(res[1])["actions"]
        for x, y in zip(a0, a1):
            if x != y:
                print(" DIFF", x["trigger_id"], "\n  run1:", x["body"][:160], "\n  run2:", y["body"][:160])


def latency(load_seconds=120):
    d = load_all()
    C.post("/v1/teardown")
    for s in ("category", "merchant", "customer", "trigger"):
        for cid, p in d[s].items():
            ctx(s, cid, 1, p)
    ids = list(d["trigger"])
    tl = []
    for k in range(5):
        t0 = time.time()
        C.post("/v1/tick", json={"now": "2026-09-26T12:00:00Z", "available_triggers": ids[k * 20:(k + 1) * 20]})
        tl.append((time.time() - t0) * 1000)
    rl = []
    for k in range(20):
        t0 = time.time()
        C.post("/v1/reply", json={"conversation_id": f"conv_lat_{k}", "merchant_id": "m_003_studio11_salon_hyderabad", "from_role": "merchant",
                                  "message": ["yes go ahead", "how much does it cost?", "why?", "hmm ok", "not now"][k % 5], "turn_number": 2})
        rl.append((time.time() - t0) * 1000)
    p95 = lambda xs: sorted(xs)[max(0, int(round(0.95 * len(xs))) - 1)]
    print(f"tick(20 triggers) ms: {[round(x) for x in tl]} p95={p95(tl):.0f}")
    print(f"reply ms p50={statistics.median(rl):.0f} p95={p95(rl):.0f} max={max(rl):.0f}")

    def one(n):
        t0 = time.time()
        try:
            if n % 3 == 0:
                c = C.get("/v1/healthz").status_code
            elif n % 3 == 1:
                c = C.post("/v1/reply", json={"conversation_id": f"conv_ld_{n}", "merchant_id": "m_001_drmeera_dentist_delhi", "from_role": "merchant",
                                              "message": "ok go ahead", "turn_number": 2}).status_code
            else:
                c = C.post("/v1/tick", json={"now": "2026-09-26T13:00:00Z", "available_triggers": ids[:5]}).status_code
        except Exception as e:
            c = type(e).__name__
        return c, (time.time() - t0) * 1000
    codes, lats = [], []
    start = time.time()
    with cf.ThreadPoolExecutor(max_workers=20) as ex:
        n = 0
        while time.time() - start < load_seconds:
            bt = time.time()
            for c, ms in [f.result() for f in [ex.submit(one, n + j) for j in range(10)]]:
                codes.append(c)
                lats.append(ms)
            n += 10
            time.sleep(max(0, 1 - (time.time() - bt)))
    bad = [c for c in codes if c != 200]
    print(f"load: {len(codes)} requests in {load_seconds}s, non-200={len(bad)} {set(map(str, bad)) or ''}, p95={p95(lats):.0f}ms max={max(lats):.0f}ms")
    dec = C.get("/v1/debug/decisions", params={"limit": 500}).json()
    reasons = {}
    for f in dec["llm_fallbacks"]:
        key = f["where"] + ":" + f["reason"].split(":")[0] + (":" + f["reason"].split(":")[1][:20] if ":" in f["reason"] else "")
        reasons[key] = reasons.get(key, 0) + 1
    print("llm:", {k: dec["llm"].get(k) for k in ("calls", "ok", "errors", "timeouts", "http_429", "rate_limited", "cache_hits")}, "fallback reasons:", reasons)


if __name__ == "__main__":
    if MODE in ("all", "examples"):
        examples()
    if MODE in ("all", "determinism"):
        determinism()
    if MODE in ("all", "latency"):
        latency(int(os.environ.get("LOAD_SECONDS", "120")))
