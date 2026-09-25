"""Judge-style audit against a RUNNING bot. Writes runs/audit_pairs.md and runs/audit_results.json.
   BOT_URL=http://127.0.0.1:8080 python -m tests.audit
Gemini usage is kept small: the 30 pairs go out in a handful of ticks (one batched call each)."""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.evidence import numeric_tokens  # noqa: E402
from app.validator import QUALIFYING, URL_RE  # noqa: E402
from tests.dataset import ROOT, load_all  # noqa: E402

BOT = os.environ.get("BOT_URL", "http://127.0.0.1:8080").rstrip("/")
C = httpx.Client(base_url=BOT, timeout=35)
R: list[dict] = []          # results: {id, status, evidence}


def rec(item: str, status: str, evidence: str) -> None:
    R.append({"id": item, "status": status, "evidence": evidence})
    print(f"[{status}] {item}: {evidence[:300]}")


def push(scope, cid, payload, v=1):
    r = C.post("/v1/context", json={"scope": scope, "context_id": cid, "version": v, "payload": payload,
                                    "delivered_at": "2026-09-26T10:00:00Z"})
    return r.status_code, r.json()


def load_everything(d, triggers=True):
    C.post("/v1/teardown")
    for s in ("category", "merchant", "customer") + (("trigger",) if triggers else ()):
        for cid, p in d[s].items():
            push(s, cid, p)


def now_iso(dt=None):
    return (dt or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")


def reply(conv, msg, turn, mid=None, cust=None, role="merchant", **extra):
    body = {"conversation_id": conv, "from_role": role, "message": msg, "turn_number": turn}
    if mid:
        body["merchant_id"] = mid
    if cust:
        body["customer_id"] = cust
    body.update(extra)
    t0 = time.time()
    r = C.post("/v1/reply", json=body)
    return r.status_code, r.json(), (time.time() - t0) * 1000


# ----------------------------------------------------------------------------- tracing helpers

def raw_tokens(*objs) -> set[str]:
    out = set()
    for o in objs:
        if not o:
            continue
        raw = json.dumps(o, ensure_ascii=False)
        toks = numeric_tokens(raw)
        out |= toks | {t.lstrip("0") or "0" for t in toks}
        for tok in re.findall(r"-?\d+\.\d+", raw):
            x = abs(float(tok))
            if x <= 1.5:
                out.add(str(int(round(x * 100))))
                out |= numeric_tokens(f"{x * 100:.1f}")
    return out


def raw_text(*objs) -> str:
    return " ".join(json.dumps(o, ensure_ascii=False) for o in objs if o).lower()


COMMON = set("""hi hello the a an your you we our this that it its is are was were be been to of in on for with from by at as and or
but if so not no yes reply confirm bas kijiye kar doon ek ke ki ka ko se mein hai hain aap aapka aapke aapki want me shall i draft
google post profile week this month days calls views quick one new worth look research roundup heads-up urgent compliance
deadline what do dr namaste ji clinic here message offer live team salon gym pharmacy restaurant practice business summary
saturday sunday monday tuesday wednesday thursday friday jan feb mar apr may jun jul aug sep oct nov dec ipl cde ors
rx gbp ctr whatsapp tonight today tomorrow""".split())


def proper_nouns(body: str) -> set[str]:
    words = re.findall(r"\b[A-Z][A-Za-z'&.-]{2,}\b", body)
    return {w.strip(".'") for w in words if w.lower().strip(".'") not in COMMON}


# ----------------------------------------------------------------------------- section 1-5: the 30 pairs

def pairs_section(d) -> list[dict]:
    load_everything(d)
    pairs = d["pairs"]
    groups: list[list[dict]] = []
    for p in pairs:
        t = d["trigger"][p["trigger_id"]]
        key = f"cust:{t.get('customer_id')}" if (t.get("scope") == "customer" or t.get("customer_id")) else f"merch:{t['merchant_id']}"
        for g_ in groups:
            if key not in {x["_key"] for x in g_}:
                g_.append({**p, "_key": key})
                break
        else:
            groups.append([{**p, "_key": key}])
    rows = []
    tick_lat = []
    for g_ in groups:
        t0 = time.time()
        acts = C.post("/v1/tick", json={"now": now_iso(), "available_triggers": [x["trigger_id"] for x in g_]}).json()["actions"]
        tick_lat.append((time.time() - t0) * 1000)
        by = {a["trigger_id"]: a for a in acts}
        for x in g_:
            rows.append({"pair": x, "action": by.get(x["trigger_id"])})
        time.sleep(1)
    dec = C.get("/v1/debug/decisions", params={"limit": 500}).json()
    out = {x["trigger_id"]: x for x in dec["decisions"]}
    fb = {x["ref"]: x["reason"] for x in dec["llm_fallbacks"]}
    for r in rows:
        tid = r["pair"]["trigger_id"]
        r["outcome"] = out.get(tid, {}).get("outcome")
        r["source"] = out.get(tid, {}).get("source")
        r["fallback"] = fb.get(tid)
    rec("0.pairs_ticks", "INFO", f"{len(groups)} ticks for 30 pairs, latencies ms {[round(x) for x in tick_lat]}")
    return sorted(rows, key=lambda r: r["pair"]["test_id"])


def analyse_pairs(d, rows) -> dict:
    md = ["# Audit — 30 canonical pairs (live bot, Gemini wording on)\n"]
    fabrications, dense, mism, noconcrete, nosource, generic, taboo_hits, hype, names_missing = [], [], [], [], [], [], [], [], []
    no_dr, cta_bad, preamble, long_bodies, hinglish_miss, cust_lang_miss, offers_not_verbatim, proper_unknown = [], [], [], [], [], [], [], []
    sent = 0
    for r in rows:
        p, a = r["pair"], r["action"]
        t = d["trigger"][p["trigger_id"]]
        m = d["merchant"][p["merchant_id"]]
        cat = d["category"][m["category_slug"]]
        cu = d["customer"].get(p["customer_id"]) if p.get("customer_id") else None
        md.append(f"## {p['test_id']} · {t['kind']} · {m['category_slug']} · {m['identity'].get('owner_first_name')}"
                  f"{' · customer ' + cu['identity']['name'] if cu else ''}")
        if not a:
            md.append(f"**No message** — outcome: `{r['outcome']}`\n")
            continue
        sent += 1
        b = a["body"]
        why = re.split(r"\.\s", a["rationale"], maxsplit=1)[0]
        md.append(f"- **Signal chosen:** {why}\n- **Wording source:** {r['source']}"
                  f"{' (fallback: ' + r['fallback'] + ')' if r['fallback'] else ''}\n- **Body:**\n\n> " + b.replace("\n", "\n> ") +
                  f"\n\n- **Rationale:** {a['rationale']}\n- cta `{a['cta']}` · send_as `{a['send_as']}` · {len(b)} chars\n")
        tid = p["test_id"]
        # 2.1 fabrication: numbers
        allowed = raw_tokens(t, m, cat, cu)
        extra = sorted(numeric_tokens(b) - allowed)
        if extra:
            fabrications.append(f"{tid}: numbers {extra}")
        # quoted strings must exist in input
        for q in re.findall(r"(?:(?<=[\s(])|^)'([^'\n]{3,80})'(?=[\s.,;:)?!]|$)|\"([^\"]{3,})\"", b):
            s = (q[0] or q[1]).strip()
            if s.lower() not in raw_text(t, m, cat, cu) and len(s) < 60:
                fabrications.append(f"{tid}: quoted text not in input: '{s}'")
        # proper nouns not in input (manual review list)
        unk = [w for w in proper_nouns(b) if w.lower() not in raw_text(t, m, cat, cu)]
        if unk:
            proper_unknown.append(f"{tid}: {unk}")
        # 1.2 density
        digit_sents = [s for s in re.split(r"(?<=[.!?])\s+", b) if re.search(r"\d", s)]
        if len(numeric_tokens(b)) >= 9 or len(digit_sents) >= 4:
            dense.append(f"{tid}: {len(numeric_tokens(b))} numbers across {len(digit_sents)} sentences")
        # 1.3 rationale evidence present in body
        ev = re.search(r"Grounded in: (.*?)\. (One CTA|Data check)", a["rationale"])
        if ev:
            for item in ev.group(1).split("; "):
                toks = numeric_tokens(item)
                if toks and not toks & numeric_tokens(b):
                    mism.append(f"{tid}: rationale cites '{item}' not reflected in body")
        # 2.2 concrete fact
        loc = (m["identity"].get("locality") or "").lower()
        if not (re.search(r"\d|₹", b) or (loc and loc in b.lower())):
            noconcrete.append(tid)
        # 2.3 source for research/compliance
        if t["kind"] in ("research_digest", "regulation_change", "cde_opportunity", "supply_alert"):
            if not re.search(r"JIDA|DCI|Dental Council|IDA|CDSCO|ICMR|Salon India|Google Trends|magicpin|circular|alert|calendar", b):
                nosource.append(tid)
        # 2.4 generic copy
        if re.search(r"increase your (sales|business)|\b\d+% off\b|boost your", b, re.I) and not re.search(r"15% OFF|20% OFF", b):
            generic.append(tid)
        # 3.2 taboo/hype
        for tb in cat["voice"].get("vocab_taboo", []):
            t0 = re.sub(r"\(.*?\)", "", tb).strip().lower()
            if t0 and t0 in b.lower():
                taboo_hits.append(f"{tid}: {t0}")
        if re.search(r"!!|AMAZING|guaranteed|HURRY", b):
            hype.append(tid)
        # 3.1 dentists Dr.
        if m["category_slug"] == "dentists" and a["send_as"] == "vera" and not b.startswith("Dr."):
            no_dr.append(tid)
        # 4.1 owner / customer name
        owner = (m["identity"].get("owner_first_name") or "").replace("Dr. ", "")
        if a["send_as"] == "vera" and owner and owner not in b[:60]:
            names_missing.append(f"{tid}: owner {owner}")
        if cu:
            cname = re.split(r"\s*\(", cu["identity"]["name"])[0].replace("Mr. ", "")
            if cname not in b:
                names_missing.append(f"{tid}: customer {cname}")
        # offers quoted that look like offer titles must be active offers or catalog suggestions
        active = {o["title"] for o in m.get("offers", []) if o.get("status") == "active"}
        catalog = {o["title"] for o in cat.get("offer_catalog", [])}
        for q in re.findall(r"'([^'\n]*@[^'\n]*)'", b):
            if q not in active and q not in catalog and q not in json.dumps(t, ensure_ascii=False):
                offers_not_verbatim.append(f"{tid}: '{q}'")
        # 5.1 CTA
        last = re.split(r"(?<=[.!?])\s+|\n", b.strip())[-1]
        directives = re.findall(r"\breply\s+(?:yes|confirm|1)|(?:yes|confirm|\d ya \d)\s+reply", re.sub(r'"[^"]*"', "", b), re.I)
        if not re.search(r"\?|reply|confirm|kijiye", last, re.I) or len(directives) > 1:
            cta_bad.append(f"{tid}: last='{last[-70:]}' directives={len(directives)}")
        # 5.3 preamble / length
        if re.search(r"hope you|reaching out|i am vera|i'm vera|this is vera", b, re.I):
            preamble.append(tid)
        if len(b) > 400:
            long_bodies.append(f"{tid}: {len(b)}")
        # 4.3 language
        langs = m["identity"].get("languages", [])
        hing = bool(re.search(r"\b(kijiye|doon|hai|hain|aap|karein|kar|rahi|mein|kis|ya)\b", b, re.I))
        if a["send_as"] == "vera" and "hi" in langs and cat["voice"].get("code_mix") == "hindi_english_natural" and not hing:
            hinglish_miss.append(tid)
        if cu:
            lp = str(cu["identity"].get("language_pref", "")).lower()
            if lp in ("hi", "hi-en mix") and not hing:
                cust_lang_miss.append(f"{tid}: pref {lp}")
            if lp in ("en", "english") and re.search(r"\b(kijiye|aapki|hain)\b", b, re.I):
                cust_lang_miss.append(f"{tid}: pref {lp} but Hindi used")
        if URL_RE.search(b):
            fabrications.append(f"{tid}: URL in body")
    (ROOT / "runs").mkdir(exist_ok=True)
    (ROOT / "runs" / "audit_pairs.md").write_text("\n".join(md), encoding="utf-8")
    return dict(sent=sent, fabrications=fabrications, dense=dense, mism=mism, noconcrete=noconcrete, nosource=nosource,
                generic=generic, taboo=taboo_hits, hype=hype, no_dr=no_dr, names_missing=names_missing, cta_bad=cta_bad,
                preamble=preamble, long=long_bodies, hinglish_miss=hinglish_miss, cust_lang_miss=cust_lang_miss,
                offers_not_verbatim=offers_not_verbatim, proper_unknown=proper_unknown)


def st(ok: bool, partial: bool = False) -> str:
    return "PASS" if ok else ("PARTIAL" if partial else "FAIL")


# ----------------------------------------------------------------------------- main

def main() -> None:
    d = load_all()
    rows = pairs_section(d)
    A = analyse_pairs(d, rows)
    by = {r["pair"]["test_id"]: r for r in rows}
    rec("1.1", "PASS", f"runs/audit_pairs.md written; {A['sent']}/30 sent; no-send: "
        + ", ".join(f"{r['pair']['test_id']}={r['outcome']}" for r in rows if not r["action"]))
    rec("1.2", st(not A["dense"], len(A["dense"]) <= 3), f"dense bodies: {A['dense'] or 'none'}")
    rec("1.3", st(not A["mism"], len(A["mism"]) <= 2), f"rationale/body mismatches: {A['mism'] or 'none'}")
    t25, t08, t29 = by["T25"], by["T08"], by["T29"]
    ok25 = bool(t25["action"]) and re.search(r"(\+8%|up 8%)", t25["action"]["body"]) and not re.search(r"\b(down|dropped|dip)\b", t25["action"]["body"], re.I)
    rec("1.4a T25", st(ok25), (t25["action"] or {}).get("body", "no action")[:160])
    rec("1.4b T08", st(t08["action"] is None), f"outcome={t08['outcome']}")
    ok29 = bool(t29["action"]) and "recall" not in t29["action"]["body"].lower()
    rec("1.4c T29", st(ok29), (t29["action"] or {}).get("body", "no action")[:160])
    # c_015 consent
    push("trigger", "trg_audit_c015", {"id": "trg_audit_c015", "scope": "customer", "kind": "customer_lapsed_soft",
                                       "merchant_id": "m_010_sunrisepharm_pharmacy_lucknow", "customer_id": "c_015_anonymous_for_m010",
                                       "payload": {}, "urgency": 3, "suppression_key": "audit:c015"})
    acts = C.post("/v1/tick", json={"now": now_iso(), "available_triggers": ["trg_audit_c015"]}).json()["actions"]
    rec("1.4e c_015", st(acts == []), f"actions={acts}")
    ph = [r for r in rows if d["trigger"][r["pair"]["trigger_id"]]["payload"].get("placeholder")]
    ph_sent = [r for r in ph if r["action"]]
    rec("1.4d placeholders", "INFO", f"{len(ph)} placeholder pairs; sent {len(ph_sent)}; no-send "
        f"{[(r['pair']['test_id'], r['outcome']) for r in ph if not r['action']]}")

    # 1.5 restraint: all 100 triggers in one tick
    load_everything(d)
    acts = C.post("/v1/tick", json={"now": now_iso(), "available_triggers": list(d["trigger"])}).json()["actions"]
    vera = [a["merchant_id"] for a in acts if a["send_as"] == "vera"]
    custs = [a["customer_id"] for a in acts if a["send_as"] != "vera"]
    per_merchant_all = {}
    for a in acts:
        per_merchant_all[a["merchant_id"]] = per_merchant_all.get(a["merchant_id"], 0) + 1
    multi = {k: v for k, v in per_merchant_all.items() if v > 1}
    acts2 = C.post("/v1/tick", json={"now": now_iso(), "available_triggers": list(d["trigger"])}).json()["actions"]
    resent = {a["suppression_key"] for a in acts} & {a["suppression_key"] for a in acts2}
    rec("1.5", st(len(vera) == len(set(vera)) and len(custs) == len(set(custs)) and not resent and len(acts) <= 20, not multi),
        f"tick of 100 triggers -> {len(acts)} actions (cap 20); vera dup merchants={len(vera) - len(set(vera))}; "
        f"merchants receiving >1 message (vera + customer-on-behalf) = {multi or 'none'}; second tick {len(acts2)} actions, re-sent keys={resent or 'none'}")
    body_acts = acts + acts2

    # 2.x
    rec("2.1", st(not A["fabrications"]), f"untraceable-to-raw-input items: {A['fabrications'] or 'none'}")
    rec("2.1b proper nouns", "INFO", f"capitalised words not found verbatim in input (manual review): {A['proper_unknown'] or 'none'}")
    rec("2.2", st(not A["noconcrete"]), f"bodies without a concrete fact: {A['noconcrete'] or 'none'}")
    rec("2.3", st(not A["nosource"]), f"research/compliance without source: {A['nosource'] or 'none'}")
    rec("2.4", st(not A["generic"]), f"generic copy: {A['generic'] or 'none'}")
    rec("3.1", st(not A["no_dr"]), f"dentist merchant msgs not opening 'Dr.': {A['no_dr'] or 'none'}")
    rec("3.2", st(not A["taboo"] and not A["hype"]), f"taboo={A['taboo'] or 'none'} hype={A['hype'] or 'none'}")
    rec("4.1", st(not A["names_missing"] and not A["offers_not_verbatim"]),
        f"names missing={A['names_missing'] or 'none'}; quoted offers not in active/catalog/trigger={A['offers_not_verbatim'] or 'none'}")
    rec("4.3", st(not A["hinglish_miss"] and not A["cust_lang_miss"], len(A["hinglish_miss"]) <= 5),
        f"merchant Hinglish expected but absent={A['hinglish_miss'] or 'none'}; customer language issues={A['cust_lang_miss'] or 'none'}")
    rec("5.1", st(not A["cta_bad"]), f"CTA issues: {A['cta_bad'] or 'none'}")
    rec("5.3", st(not A["preamble"] and not A["long"], not A["preamble"]), f"preamble={A['preamble'] or 'none'}; >400 chars={A['long'] or 'none'}")

    # 4.2 conversation history continuity (m_001 whitening/aligners, m_006 thali, m_009 recall list)
    hist = []
    for tid_, mid in (("T09", "m_001"), ("T30", "m_001"), ("T06", "m_001"), ("T01", "m_006"), ("T12", "m_006"), ("T05", "m_009"), ("T07", "m_009")):
        a = by[tid_]["action"]
        if a:
            hist.append(f"{tid_}: " + ("refs history" if re.search(r"whitening|aligner|list you asked|as you asked|you asked|we discussed|corporate", a["body"], re.I) else "no history reference"))
    t18 = None
    load_everything(d)
    acts = C.post("/v1/tick", json={"now": now_iso(), "available_triggers": ["trg_018_supply_atorvastatin_recall"]}).json()["actions"]
    if acts:
        t18 = acts[0]["body"]
    repitch = bool(t18 and re.search(r"kar doon\?|Want me to|Shall I", t18))
    rec("4.2", "PARTIAL" if hist else "FAIL", f"{hist}; m_009 (already said 'Yes send me the list') gets supply_alert: "
        f"{'RE-PITCHES the list as a question' if repitch else 'delivers'} -> '{(t18 or '')[-120:]}'; re-introduction of Vera: none found")

    # 5.2 levers
    lev = {"proof": r"reviews|similar|peer|trial|n=|across metros", "urgency/loss": r"urgent|deadline|down|dropped|lapsed|renews|ends in|before",
           "curiosity": r"new in|worth|research|what's been|\?", "effort done-for-you": r"draft|I'll|ready|live kar|set up",
           "convenience (booking)": r"slot|reminder|book|we'll hold|confirm"}
    nolever = []
    for r in rows:
        if r["action"] and not any(re.search(rx, r["action"]["body"], re.I) for rx in lev.values()):
            nolever.append(r["pair"]["test_id"])
    rec("5.2", st(not nolever), f"bodies with no detectable lever: {nolever or 'none'}")

    # ------------------------------------------------------------------ 6 adaptation
    load_everything(d)
    cat = copy.deepcopy(d["category"]["dentists"])
    cat["digest"].insert(0, {"id": "d_audit_new", "kind": "research", "title": "Night-guard use cuts bruxism-linked fractures 41% over 2 years",
                             "source": "IJDR Aug 2026", "trial_n": 880, "summary": "880 adults; 41% fewer cusp fractures with nightly guards."})
    s1, _ = push("category", "dentists", cat, 2)
    push("trigger", "trg_audit_dig", {"id": "trg_audit_dig", "scope": "merchant", "kind": "research_digest", "merchant_id": "m_014_dr_asha_dentist_chandigarh",
                                      "payload": {"top_item_id": "d_audit_new"}, "urgency": 2, "suppression_key": "audit:dig"})
    a = C.post("/v1/tick", json={"now": now_iso(), "available_triggers": ["trg_audit_dig"]}).json()["actions"]
    b = a[0]["body"] if a else ""
    rec("6.1", st(s1 == 200 and "41%" in b and "IJDR" in b), f"v2 push={s1}; body: {b[:200]}")
    mer = copy.deepcopy(d["merchant"]["m_003_studio11_salon_hyderabad"])
    mer["performance"].update({"calls": 41, "views": 3900})
    mer["performance"]["delta_7d"] = {"views_pct": -0.21, "calls_pct": -0.34}
    push("merchant", mer["merchant_id"], mer, 2)
    push("trigger", "trg_audit_dip", {"id": "trg_audit_dip", "kind": "perf_dip", "merchant_id": mer["merchant_id"],
                                      "payload": {"metric": "calls", "delta_pct": -0.34}, "urgency": 4, "suppression_key": "audit:dip"})
    a = C.post("/v1/tick", json={"now": now_iso(), "available_triggers": ["trg_audit_dip"]}).json()["actions"]
    b = a[0]["body"] if a else ""
    rec("6.2", st("34%" in b and "62" not in b), f"body: {b[:220]}")
    push("category", "salons", d["category"]["salons"], 3)
    newm = {"merchant_id": "m_audit_new", "category_slug": "salons", "identity": {"name": "Hair Hub", "owner_first_name": "Zoya", "city": "Pune",
                                                                                  "locality": "Kothrud", "languages": ["en", "hi"]},
            "performance": {"views": 1500, "calls": 20, "ctr": 0.03, "delta_7d": {"views_pct": 0.1, "calls_pct": -0.02}},
            "offers": [{"title": "Hair Spa @ ₹449", "status": "active"}], "subscription": {"status": "active", "days_remaining": 90}}
    push("merchant", "m_audit_new", newm)
    push("customer", "c_audit_ria", {"customer_id": "c_audit_ria", "merchant_id": "m_audit_new", "identity": {"name": "Ria", "language_pref": "hi-en mix"},
                                     "relationship": {"last_visit": "2026-06-10", "visits_total": 4}, "state": "lapsed_soft",
                                     "preferences": {"reminder_opt_in": True}, "consent": {"opted_in_at": "2026-01-01", "scope": ["recall_reminders"]}})
    push("trigger", "trg_audit_recall", {"id": "trg_audit_recall", "scope": "customer", "kind": "recall_due", "merchant_id": "m_audit_new",
                                         "customer_id": "c_audit_ria", "payload": {"service_due": "hair_spa", "last_service_date": "2026-06-10",
                                                                                   "available_slots": [{"iso": "2026-10-02T17:00:00+05:30", "label": "Fri 2 Oct, 5pm"}]},
                                         "urgency": 3, "suppression_key": "audit:recall"})
    a = C.post("/v1/tick", json={"now": now_iso(), "available_triggers": ["trg_audit_recall"]}).json()["actions"]
    b = a[0] if a else {}
    rec("6.3", st(bool(a) and b.get("send_as") == "merchant_on_behalf" and "Ria" in b.get("body", "") and "Fri 2 Oct" in b.get("body", ""), bool(a)),
        f"action: {json.dumps(b, ensure_ascii=False)[:380] if b else 'NONE (outcome in log)'}")
    push("trigger", "trg_audit_odd", {"id": "trg_audit_odd", "kind": "weather_heatwave", "merchant_id": "m_audit_new", "weird_field": {"x": [1, 2]},
                                      "payload": {"temp_c": 44, "city": "Pune"}, "urgency": 2, "suppression_key": "audit:odd"})
    push("trigger", "trg_audit_bare", {"id": "trg_audit_bare", "merchant_id": "m_audit_new"})
    r = C.post("/v1/tick", json={"now": now_iso(), "available_triggers": ["trg_audit_odd", "trg_audit_bare"]})
    acts = r.json()["actions"]
    bb = " | ".join(x["body"] for x in acts)
    rec("6.4", st(r.status_code == 200 and "44" not in bb), f"HTTP {r.status_code}; {len(acts)} action(s): {bb[:220]}")
    old = copy.deepcopy(d["trigger"]["trg_025_dormancy_glamour"])
    old.update({"id": "trg_audit_old", "suppression_key": "audit:old", "expires_at": "2025-01-01T00:00:00Z"})
    push("trigger", "trg_audit_old", old)
    a = C.post("/v1/tick", json={"now": now_iso(), "available_triggers": ["trg_audit_old"]}).json()["actions"]
    b = a[0]["body"] if a else ""
    rec("6.5", st(bool(a) and "38 days" in b), f"expires_at 2025-01-01 still sent; day count from payload: {b[:160]}")

    # ------------------------------------------------------------------ 7 conversations
    load_everything(d)
    code, out, ms = reply("conv_min_1", "Ok lets do it", 2)
    rec("7.1", st(code == 200 and out.get("action") in ("send", "wait", "end")), f"minimal body -> {code} {json.dumps(out, ensure_ascii=False)[:200]}")
    p = subprocess.run(["curl", "-s", "-o", "-", "-w", "\n%{http_code}", "-X", "POST", f"{BOT}/v1/reply", "-d",
                        '{"conversation_id":"conv_noct","from_role":"merchant","message":"yes please","turn_number":2}'],
                       capture_output=True, text=True, encoding="utf-8")
    code_nc = p.stdout.strip().splitlines()[-1]
    p2 = subprocess.run(["curl", "-s", "-w", "\n%{http_code}", "-X", "POST", f"{BOT}/v1/context", "-d",
                         json.dumps({"scope": "category", "context_id": "gyms", "version": 9, "payload": d["category"]["gyms"]})],
                        capture_output=True, text=True, encoding="utf-8")
    rec("7.2", st(code_nc == "200" and '"action"' in p.stdout and p2.stdout.strip().splitlines()[-1] == "200"),
        f"curl -d (form content-type) /v1/reply -> {code_nc} {p.stdout.splitlines()[0][:120]}; /v1/context -> {p2.stdout.strip().splitlines()[-1]}")
    code, out, _ = reply("conv_never_seen_zz", "what is this about?", 2, mid="m_001_drmeera_dentist_delhi")
    rec("7.3", st(code == 200 and out.get("action") in ("send", "wait", "end")), f"{code} {out.get('action')} {out.get('body', '')[:120]}")
    acts = C.post("/v1/tick", json={"now": now_iso(), "available_triggers": ["trg_022_cde_webinar_dentists"]}).json()["actions"]
    conv = acts[0]["conversation_id"]
    canned = "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."
    seq1 = [reply(conv, canned, i, mid="m_001_drmeera_dentist_delhi")[1]["action"] for i in range(2, 6)]
    seq2 = [reply(f"conv_audit_auto_{i}", "Thank you for contacting us! Our team will respond shortly.", i + 1, mid="m_005_pizzajunction_restaurant_delhi")[1]["action"] for i in range(4)]
    rec("7.4", st(seq1[:3] == ["send", "wait", "end"] and seq2[:3] == ["send", "wait", "end"]), f"same conv: {seq1}; fresh ids: {seq2}")
    code, out, _ = reply("conv_intent_x", "Ok lets do it. Whats next?", 2, mid="m_003_studio11_salon_hyderabad")
    low = out.get("body", "").lower()
    ok = any(w in low for w in ["done", "sending", "draft", "confirm", "next"]) and not any(q in low for q in QUALIFYING)
    rec("7.5", st(ok), f"{out.get('body', '')[:200]}")
    acts = C.post("/v1/tick", json={"now": now_iso(), "available_triggers": ["trg_005_renewal_due_bharat"]}).json()["actions"]
    c5 = acts[0]["conversation_id"] if acts else "conv_obj"
    _, o1, _ = reply(c5, "This is too expensive for me.", 2, mid="m_002_bharat_dentist_mumbai")
    _, o2, _ = reply("conv_obj_2", "not now", 2, mid="m_004_glamour_salon_pune")
    pushy = re.search(r"only today|last chance|hurry|must", o1.get("body", ""), re.I)
    rec("7.6", st(o1.get("action") in ("send", "wait", "end") and not pushy and o2.get("action") == "wait", o2.get("action") == "wait"),
        f"'too expensive' -> {o1.get('action')}: {o1.get('body', '')[:180]} | 'not now' -> {o2.get('action')} {o2.get('wait_seconds', '')}")
    acts = C.post("/v1/tick", json={"now": now_iso(), "available_triggers": ["trg_009_winback_glamour"]}).json()["actions"]
    ch = acts[0]["conversation_id"] if acts else "conv_h"
    _, h1, _ = reply(ch, "Why are you bothering me. This is useless. Stop sending these.", 2, mid="m_004_glamour_salon_pune")
    _, h2, _ = reply(ch, "can you also help me file my GST?", 3, mid="m_004_glamour_salon_pune")
    _, g1, _ = reply("conv_gst_only", "Btw can you help me with my GST filing this month?", 2, mid="m_006_southindiancafe_restaurant_bangalore")
    rec("7.7", st(h1.get("action") == "end" and h2.get("action") in ("end", "send") and "CA" in g1.get("body", "")),
        f"hostile -> {h1.get('action')}; then GST -> {h2.get('action')} {h2.get('body', '')[:100]}; GST alone -> {g1.get('body', '')[:160]}")
    acts = C.post("/v1/tick", json={"now": now_iso(), "available_triggers": ["trg_012_milestone_mylari"]}).json()["actions"]
    cm = acts[0]["conversation_id"]
    bodies = [acts[0]["body"]]
    trail = []
    for i, msg in enumerate(["accha, kya aap mujhe batayenge ye kaise kaam karega?", "hmm", "theek hai", "haan kar do", "ok"], 2):
        _, o, _ = reply(cm, msg, i, mid="m_006_southindiancafe_restaurant_bangalore")
        trail.append(o.get("action"))
        if o.get("action") == "send":
            bodies.append(o["body"])
    hindi_first = re.search(r"\b(hai|kijiye|dungi|aap|theek|raha|rahi|ke)\b", bodies[1] if len(bodies) > 1 else "", re.I)
    rec("7.8", st(len(bodies) == len(set(bodies)) and bool(hindi_first)), f"5 turns actions={trail}; unique bodies={len(set(bodies))}/{len(bodies)}; "
        f"reply to Hindi: {bodies[1][:140] if len(bodies) > 1 else '-'}")

    # ------------------------------------------------------------------ 8 contract
    C.post("/v1/teardown")
    for s in ("category", "merchant", "customer"):
        for cid, pl in d[s].items():
            push(s, cid, pl)
    h = C.get("/v1/healthz").json()
    m = C.get("/v1/metadata").json()
    rec("8.1", st(h["contexts_loaded"] == {"category": 5, "merchant": 50, "customer": 200, "trigger": 0} and bool(m.get("contact_email")),
                  h["contexts_loaded"] == {"category": 5, "merchant": 50, "customer": 200, "trigger": 0}),
        f"counts={h['contexts_loaded']}; metadata team={m['team_name']} members={m['team_members']} email='{m['contact_email']}' model={m['model']}")
    c1 = push("category", "dentists", d["category"]["dentists"], 1)
    c2 = push("category", "dentists", d["category"]["dentists"], 2)
    c3 = C.post("/v1/context", json={"scope": "shop", "context_id": "x", "version": 1, "payload": {}})
    big = {**d["merchant"]["m_001_drmeera_dentist_delhi"], "blob": "x" * 480_000}
    c4 = push("merchant", "m_big", big, 1)
    rec("8.2", st(c1[0] == 409 and c1[1].get("current_version") == 1 and c2[0] == 200 and c3.status_code == 400 and c4[0] == 200),
        f"same v -> {c1[0]} {c1[1]}; higher v -> {c2[0]}; bad scope -> {c3.status_code}; ~480KB payload -> {c4[0]}")
    empty = C.post("/v1/tick", json={"now": now_iso(), "available_triggers": []}).json()
    for tid, pl in d["trigger"].items():
        push("trigger", tid, pl)
    acts = C.post("/v1/tick", json={"now": now_iso(), "available_triggers": list(d["trigger"])}).json()["actions"]
    fields = {"conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name", "template_params", "body", "cta",
              "suppression_key", "rationale"}
    allf = all(fields <= set(a) for a in acts)
    ids = [a["conversation_id"] for a in acts]
    acts2 = C.post("/v1/tick", json={"now": now_iso(), "available_triggers": list(d["trigger"])}).json()["actions"]
    rec("8.3", st(empty == {"actions": []} and allf and len(acts) <= 20 and len(ids) == len(set(ids))
                  and not ({a["suppression_key"] for a in acts} & {a["suppression_key"] for a in acts2})
                  and not (set(ids) & {a["conversation_id"] for a in acts2})),
        f"empty={empty}; {len(acts)} actions all 11 fields={allf}; unique ids={len(set(ids))}/{len(ids)}; 2nd tick {len(acts2)} new, no key/id reuse")

    json.dump(R, open(ROOT / "runs" / "audit_results.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\nDONE", sum(r["status"] == "PASS" for r in R), "PASS /", sum(r["status"] == "FAIL" for r in R), "FAIL /",
          sum(r["status"] == "PARTIAL" for r in R), "PARTIAL")


if __name__ == "__main__":
    main()
