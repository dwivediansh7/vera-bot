"""INTERNAL PROXY rubric scorer. This is NOT the official judge.

It approximates the 5 dimensions with transparent heuristics so regressions are visible without an LLM key.
Use judge_simulator.py (tests/run_official_sim.py) with a real key for LLM-judged scores.
"""

from __future__ import annotations

import re

from app.evidence import numeric_tokens
from app.validator import JARGON, URL_RE, case_similarity

VOICE_MARKERS = {
    "dentists": [r"\bDr\.", r"patient", r"recall", r"caries|fluoride|IOPA|scaling|clinic|CDE|radiograph|aligner"],
    "salons": [r"salon|client|stylist|bridal|spa|haircut|balayage|keratin|booking"],
    "restaurants": [r"covers|orders|delivery|footfall|thali|menu|dine-in|table|match"],
    "gyms": [r"members?|session|trial|retention|coach|class|PT|challenge"],
    "pharmacies": [r"Rx|refill|batch|molecule|generic|pharmac|medicines|dawa|delivery|shelf"],
}
LEVERS = {
    "loss": r"down|dropped|lapsed|gap|missing|before|deadline|ends in|renews|urgent|recall",
    "social": r"reviews|similar|peer|across metros|clinics|salons|gyms|restaurants|pharmacies",
    "curiosity": r"worth a look|worth knowing|new in|research|quick one|what's been|\?",
    "effort": r"draft|I'll|I can|set up|put .* live|handle|ready",
    "binary": r"reply (yes|confirm|1)|yes reply|confirm reply",
}


def score(action: dict, merchant: dict, category: dict, trigger: dict, customer: dict | None = None) -> dict:
    body = action.get("body", "")
    low = body.lower()
    slug = category.get("slug", "")
    nums = numeric_tokens(body)
    owner = str((merchant.get("identity") or {}).get("owner_first_name") or "").replace("Dr. ", "")
    penalties, notes = 0, []

    # specificity: verifiable anchors
    anchors = len(nums) + len(re.findall(r"₹|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b", low)) + \
        (2 if re.search(r"JIDA|DCI|CDSCO|ICMR|IDA|circular|Trends|magazine|alert", body) else 0)
    spec = min(10, 3 + anchors)

    # category fit
    marks = sum(1 for rx in VOICE_MARKERS.get(slug, []) if re.search(rx, body, re.I))
    cat = min(10, 5 + 2 * marks)
    if re.search(r"!!|amazing|hurry|best in", low):
        cat -= 3

    # merchant fit
    mf = 4
    if owner and owner.lower() in low:
        mf += 2
    if customer and str((customer.get("identity") or {}).get("name", "")).split(" (")[0].lower() in low:
        mf += 2
    if any(o.get("title", "").lower() in low for o in merchant.get("offers", []) if isinstance(o, dict)):
        mf += 1
    perf = merchant.get("performance") or {}
    if any(str(v) in body.replace(",", "") for v in (perf.get("views"), perf.get("calls")) if v):
        mf += 1
    langs = (merchant.get("identity") or {}).get("languages") or []
    if "hi" in langs and re.search(r"\b(kijiye|kar doon|hai|hain|aap)\b", low):
        mf += 1
    mf = min(10, mf)

    # decision quality: trigger reflected + not a fact dump
    kind = str(trigger.get("kind", "")).replace("_", " ")
    payload_vals = [str(v).lower() for v in (trigger.get("payload") or {}).values() if isinstance(v, (str, int, float)) and not isinstance(v, bool)]
    reflected = sum(1 for v in payload_vals if v and len(v) > 1 and v in low.replace(",", ""))
    dq = 5 + min(3, reflected) + (1 if len(re.split(r"(?<=[.!?])\s+", body)) <= 6 else -1)
    dq = min(10, dq)

    # engagement
    lev = [k for k, rx in LEVERS.items() if re.search(rx, body, re.I)]
    eng = min(10, 4 + len(lev) + (1 if re.search(r"(\?|reply|kijiye)[^.?!]*[.?!]?\s*$", body, re.I) else 0))

    if URL_RE.search(body):
        penalties += 3
        notes.append("url")
    if any(j in low for j in JARGON):
        penalties += 1
        notes.append("jargon")
    if case_similarity(body) >= 0.3:
        penalties += 2
        notes.append("case-study copy")
    total = spec + cat + mf + dq + eng - penalties
    return {"specificity": spec, "category_fit": cat, "merchant_fit": mf, "decision_quality": dq,
            "engagement_compulsion": eng, "penalties": penalties, "notes": notes, "levers": lev, "total": total}
