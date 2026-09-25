"""End-to-end evaluation harness against a RUNNING bot (local or deployed).

  BOT_URL=http://127.0.0.1:8080 python -m tests.harness

Phases (mirrors the testing brief):
  1. warmup: healthz, metadata, push 5 categories + 50 merchants + 200 customers, verify exact counts
  2. canonical 30 pairs: one tick per pair -> runs/<ts>/pairs.jsonl + proxy scores
  3. lifecycle: 12 ticks of 5 simulated minutes with mid-test injections (new digest version, perf shifts,
     new customer + recall_due 2 min later, brand-new trigger kinds) and scripted recipient replies
  4. replay scenarios: auto-reply hell, intent transition, hostile/off-topic
  5. latency + load: tick with 20 triggers, 10 req/s mixed traffic for N seconds
Writes runs/<ts>/report.md. Proxy scores are labelled as such; they are NOT official judge scores.
"""

from __future__ import annotations

import concurrent.futures as cf
import copy
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.validator import QUALIFYING  # noqa: E402
from tests.dataset import ROOT, load_all  # noqa: E402
from tests.proxy_judge import score  # noqa: E402

BOT = os.environ.get("BOT_URL", "http://127.0.0.1:8080").rstrip("/")
LOAD_SECONDS = int(os.environ.get("LOAD_SECONDS", "20"))
INJECTED_MERCHANTS = {"m_001_drmeera_dentist_delhi", "m_003_studio11_salon_hyderabad", "m_011_dr_sameer_dentist_bangalore"}
C = httpx.Client(base_url=BOT, timeout=35)
REPORT: list[str] = []
FAILS: list[str] = []


def say(line: str = "") -> None:
    print(line)
    REPORT.append(line)


def check(cond: bool, what: str) -> bool:
    say(f"- [{'PASS' if cond else 'FAIL'}] {what}")
    if not cond:
        FAILS.append(what)
    return cond


def push(scope, cid, payload, v=1):
    r = C.post("/v1/context", json={"scope": scope, "context_id": cid, "version": v, "payload": payload,
                                    "delivered_at": datetime.now(timezone.utc).isoformat()})
    return r.status_code, r.json()


def tick(ids, now):
    t0 = time.time()
    r = C.post("/v1/tick", json={"now": now.isoformat().replace("+00:00", "Z"), "available_triggers": ids})
    return r.json(), (time.time() - t0) * 1000


def reply(conv, mid, msg, turn, cust=None, role="merchant"):
    t0 = time.time()
    r = C.post("/v1/reply", json={"conversation_id": conv, "merchant_id": mid, "customer_id": cust, "from_role": role,
                                  "message": msg, "received_at": datetime.now(timezone.utc).isoformat(), "turn_number": turn})
    return r.json(), (time.time() - t0) * 1000


def main() -> int:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = ROOT / "runs" / ts
    out.mkdir(parents=True, exist_ok=True)
    d = load_all()
    say(f"# Vera harness run {ts}\nBot: {BOT}\n")
    C.post("/v1/teardown")

    # ---------------------------------------------------------------- 1 warmup
    say("## 1. Warmup")
    h = C.get("/v1/healthz").json()
    check(h.get("status") == "ok", "healthz ok")
    m = C.get("/v1/metadata").json()
    check(all(k in m for k in ("team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at")), "metadata fields")
    for scope in ("category", "merchant", "customer"):
        for cid, p in d[scope].items():
            push(scope, cid, p)
    counts = C.get("/v1/healthz").json()["contexts_loaded"]
    check(counts == {"category": 5, "merchant": 50, "customer": 200, "trigger": 0}, f"exact counts {counts}")
    code, body = push("category", "dentists", d["category"]["dentists"], 1)
    check(code == 409 and body.get("current_version") == 1, "re-push same version -> 409")

    # ---------------------------------------------------------------- 2 canonical pairs
    say("\n## 2. Canonical 30 pairs (proxy-scored)")
    for tid, t in d["trigger"].items():
        push("trigger", tid, t)
    now = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)
    rows, lat = [], []
    for p in d["pairs"]:
        res, ms = tick([p["trigger_id"]], now)
        lat.append(ms)
        acts = res.get("actions", [])
        t = d["trigger"][p["trigger_id"]]
        mer = d["merchant"][p["merchant_id"]]
        cat = d["category"][mer["category_slug"]]
        cus = d["customer"].get(p["customer_id"]) if p["customer_id"] else None
        if acts:
            a = acts[0]
            sc = score(a, mer, cat, t, cus)
            rows.append({"test_id": p["test_id"], **a, "proxy": sc})
        else:
            rows.append({"test_id": p["test_id"], "trigger_id": p["trigger_id"], "abstained": True})
    with open(out / "pairs.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    sent = [r for r in rows if not r.get("abstained")]
    dims = ["specificity", "category_fit", "merchant_fit", "decision_quality", "engagement_compulsion"]
    say(f"sent {len(sent)}/30, abstained: {[r['test_id'] for r in rows if r.get('abstained')]}")
    for dname in dims:
        say(f"  proxy avg {dname}: {statistics.mean(r['proxy'][dname] for r in sent):.1f}")
    say(f"  proxy avg total: {statistics.mean(r['proxy']['total'] for r in sent):.1f}/50  (INTERNAL PROXY, not the official judge)")
    check(all(r["proxy"]["penalties"] == 0 for r in sent), "no proxy penalties (url/jargon/case-copy)")
    check(max(lat) < 8000, f"pair tick latency max {max(lat):.0f}ms")
    worst = sorted(sent, key=lambda r: r["proxy"]["total"])[:3]
    best = sorted(sent, key=lambda r: -r["proxy"]["total"])[:3]
    say("\nBest 3 (proxy):")
    for r in best:
        say(f"  {r['test_id']} ({r['proxy']['total']}): {r['body'][:220]}")
    say("Worst 3 (proxy):")
    for r in worst:
        say(f"  {r['test_id']} ({r['proxy']['total']}): {r['body'][:220]}")

    # ---------------------------------------------------------------- 3 lifecycle with injections
    say("\n## 3. Lifecycle: 12 ticks with adaptive injections")
    C.post("/v1/teardown")
    for scope in ("category", "merchant", "customer", "trigger"):
        for cid, p in d[scope].items():
            push(scope, cid, p)
    all_ids = list(d["trigger"].keys())
    rng_ids = [all_ids[i::12] for i in range(12)]
    t0 = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)
    seen_bodies: dict[str, list[str]] = {}
    lifecycle_actions, tick_lat = [], []
    new_digest_cited = False
    perf_updated_used = False
    recall_sent = False
    for i in range(12):
        now = t0 + timedelta(minutes=5 * i)
        if i == 3:  # new digest version for dentists
            cat = copy.deepcopy(d["category"]["dentists"])
            cat["digest"].insert(0, {"id": "d_inj_1", "kind": "research", "title": "Night-guard use cuts bruxism-linked fractures 41% over 2 years",
                                     "source": "IJDR Sep 2026", "trial_n": 880, "summary": "Cohort of 880 adults; 41% fewer cusp fractures with nightly guards."})
            push("category", "dentists", cat, 2)
            push("trigger", "trg_inj_digest", {"id": "trg_inj_digest", "scope": "merchant", "kind": "research_digest", "source": "external",
                                                "merchant_id": "m_011_dr_sameer_dentist_bangalore", "payload": {"top_item_id": "d_inj_1"},
                                                "urgency": 2, "suppression_key": "inj:digest"})
            rng_ids[i].append("trg_inj_digest")
        if i == 5:  # performance shift + dip trigger
            mer = copy.deepcopy(d["merchant"]["m_003_studio11_salon_hyderabad"])
            mer["performance"].update({"calls": 41, "views": 3900})
            mer["performance"]["delta_7d"] = {"views_pct": -0.21, "calls_pct": -0.34}
            push("merchant", mer["merchant_id"], mer, 2)
            push("trigger", "trg_inj_dip", {"id": "trg_inj_dip", "scope": "merchant", "kind": "perf_dip", "merchant_id": mer["merchant_id"],
                                             "payload": {"metric": "calls", "delta_pct": -0.34, "window": "7d"}, "urgency": 4,
                                             "suppression_key": "inj:dip"})
            rng_ids[i].append("trg_inj_dip")
        if i == 7:  # new customer, then recall 2 min later (next tick)
            push("customer", "c_inj_neha", {"customer_id": "c_inj_neha", "merchant_id": "m_001_drmeera_dentist_delhi",
                                            "identity": {"name": "Neha", "language_pref": "english"},
                                            "relationship": {"last_visit": "2026-03-20", "visits_total": 3, "services_received": ["scaling"]},
                                            "state": "lapsed_soft", "preferences": {"preferred_slots": "saturday_morning", "reminder_opt_in": True},
                                            "consent": {"opted_in_at": "2025-10-01", "scope": ["recall_reminders"]}})
        if i == 8:
            push("trigger", "trg_inj_recall", {"id": "trg_inj_recall", "scope": "customer", "kind": "recall_due", "merchant_id": "m_001_drmeera_dentist_delhi",
                                                "customer_id": "c_inj_neha", "payload": {"service_due": "6_month_scaling", "last_service_date": "2026-03-20",
                                                                                         "due_date": "2026-09-20", "available_slots": [{"iso": "2026-10-03T10:00:00+05:30", "label": "Sat 3 Oct, 10am"}]},
                                                "urgency": 3, "suppression_key": "inj:recall"})
            push("trigger", "trg_inj_weird", {"id": "trg_inj_weird", "kind": "local_news_event", "merchant_id": "m_005_pizzajunction_restaurant_delhi",
                                               "payload": {"headline": "Metro line closed 3h"}, "urgency": 2, "suppression_key": "inj:weird"})
            rng_ids[i] += ["trg_inj_recall", "trg_inj_weird"]
        res, ms = tick(rng_ids[i], now)
        tick_lat.append(ms)
        for a in res.get("actions", []):
            lifecycle_actions.append(a)
            b = a["body"]
            if a["trigger_id"] == "trg_inj_digest" and "IJDR Sep 2026" in b and "41%" in b:
                new_digest_cited = True
            if a["trigger_id"] == "trg_inj_dip" and "34%" in b:
                perf_updated_used = True
            if a["trigger_id"] == "trg_inj_recall" and "Neha" in b and "Sat 3 Oct" in b:
                recall_sent = True
            seen_bodies.setdefault(a["merchant_id"], []).append(b)
            # scripted recipient reply rotation
            script = ["Yes please go ahead", "Thank you for contacting us! Our team will respond shortly.",
                      "How much does this cost?", "Not interested, stop.", "Can you help me with GST?"]
            msg = script[len(lifecycle_actions) % len(script)]
            if a["merchant_id"] in INJECTED_MERCHANTS and "stop" in msg.lower():
                msg = "Sounds good, tell me more"   # keep injection targets reachable for the adaptation checks
            r1, rl = reply(a["conversation_id"], a["merchant_id"], msg, 2, a.get("customer_id"),
                           "customer" if a.get("customer_id") else "merchant")
            ok = r1.get("action") in ("send", "wait", "end") and (r1.get("action") != "send" or r1.get("body", "").strip())
            if not ok:
                FAILS.append(f"bad reply shape {r1}")
    check(all(ms < 10000 for ms in tick_lat), f"lifecycle tick latency max {max(tick_lat):.0f}ms")
    check(new_digest_cited, "new digest item (category v2) cited in a later send")
    check(perf_updated_used, "updated performance numbers used after merchant v2")
    check(recall_sent, "new customer + recall_due composed with real slot")
    dup = any(len(v) != len(set(v)) for v in seen_bodies.values())
    check(not dup, "no merchant received the same body twice")
    per_tick_merchants_ok = True
    check(len(lifecycle_actions) > 0, f"lifecycle produced {len(lifecycle_actions)} actions across 12 ticks")
    with open(out / "lifecycle.jsonl", "w", encoding="utf-8") as f:
        for a in lifecycle_actions:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")

    # ---------------------------------------------------------------- 4 replay (standalone, fresh state)
    say("\n## 4. Replay scenarios")
    C.post("/v1/teardown")
    for scope in ("category", "merchant", "customer"):
        for cid, p in d[scope].items():
            push(scope, cid, p)
    canned = "Thank you for contacting us! Our team will respond shortly."
    acts = [reply(f"conv_harness_auto_{i}", "m_002_bharat_dentist_mumbai", canned, i + 1)[0]["action"] for i in range(4)]
    check(acts[:3] == ["send", "wait", "end"], f"auto-reply hell -> {acts}")
    r, _ = reply("conv_harness_intent", "m_003_studio11_salon_hyderabad", "Ok lets do it. Whats next?", 3)
    low = r.get("body", "").lower()
    check(r.get("action") == "send" and not any(q in low for q in QUALIFYING)
          and any(w in low for w in ["done", "sending", "draft", "here", "confirm", "proceed", "next"]), f"intent -> action: {r.get('body', '')[:120]}")
    r1, _ = reply("conv_harness_hostile", "m_004_glamour_salon_pune", "Why are you bothering me. Useless. Stop.", 2)
    r2, _ = reply("conv_harness_hostile", "m_004_glamour_salon_pune", "can you help me file GST?", 3)
    check(r1.get("action") == "end" and r2.get("action") in ("end", "send"), f"hostile -> {r1.get('action')}, then {r2.get('action')}")

    # ---------------------------------------------------------------- 5 latency + load
    say("\n## 5. Latency + load")
    ids20 = all_ids[:40]
    C.post("/v1/teardown")
    for scope in ("category", "merchant", "customer", "trigger"):
        for cid, p in d[scope].items():
            push(scope, cid, p)
    lat20 = []
    for k in range(5):
        res, ms = tick(ids20, datetime(2026, 9, 26, 12, k, tzinfo=timezone.utc))
        lat20.append(ms)
    check(max(lat20) < 8000, f"tick with 40 candidate triggers: max {max(lat20):.0f}ms, p50 {statistics.median(lat20):.0f}ms")

    def one(n):
        try:
            if n % 3 == 0:
                return C.get("/v1/healthz").status_code
            if n % 3 == 1:
                return C.post("/v1/reply", json={"conversation_id": f"conv_load_{n}", "merchant_id": "m_001_drmeera_dentist_delhi",
                                                 "from_role": "merchant", "message": "ok go ahead", "turn_number": 2}).status_code
            return C.post("/v1/tick", json={"now": "2026-09-26T13:00:00Z", "available_triggers": ids20[:5]}).status_code
        except Exception:
            return 0
    codes, lats = [], []
    start = time.time()
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        n = 0
        while time.time() - start < LOAD_SECONDS:
            batch_t = time.time()
            futs = [ex.submit(one, n + j) for j in range(10)]
            n += 10
            codes += [f.result() for f in futs]
            lats.append((time.time() - batch_t) * 1000)
            time.sleep(max(0, 1 - (time.time() - batch_t)))
    check(all(c == 200 for c in codes), f"load {len(codes)} requests @10 rps for {LOAD_SECONDS}s: non-200 = {sum(c != 200 for c in codes)}")
    check(C.get("/v1/healthz").status_code == 200, "healthz after load")

    say(f"\n## Result: {'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAIL(S)'}")
    for f_ in FAILS:
        say(f"  - {f_}")
    (out / "report.md").write_text("\n".join(REPORT), encoding="utf-8")
    say(f"\nArtifacts: {out}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
