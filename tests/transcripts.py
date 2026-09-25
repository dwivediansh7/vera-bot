"""Print multi-turn transcripts against a running bot for human review.   BOT_URL=... python -m tests.transcripts"""

from __future__ import annotations

import os

import httpx

from tests.dataset import load_all

C = httpx.Client(base_url=os.environ.get("BOT_URL", "http://127.0.0.1:8080"), timeout=35)

SCRIPTS = [
    ("trg_001_research_digest_dentists", ["Yes please send the abstract. Also draft the patient WhatsApp.", "CONFIRM"], "merchant"),
    ("trg_013_corporate_thali_planning", ["Looks good, what's the delivery radius?", "ok go ahead"], "merchant"),
    ("trg_004_perf_dip_bharat", ["haan kar do", "theek hai confirm"], "merchant"),
    ("trg_023_competitor_opened_dentist", ["Why should I not match the price?", "hmm ok", "fine do it"], "merchant"),
    ("trg_018_supply_atorvastatin_recall", ["Yes send me the list", "Can you also do my GST?", "ok"], "merchant"),
    ("trg_010_ipl_match_delhi", ["Why delivery and not dine-in?", "ok karo"], "merchant"),
    ("trg_003_recall_due_priya", ["1"], "customer"),
    ("trg_015_winback_rashmi", ["Not now, maybe next month"], "customer"),
    ("trg_011_review_theme_late_delivery", ["Aapki jaankari ke liye bahut-bahut shukriya. Main aapki yeh sabhi baatein hamari team tak pahuncha deti hoon."] * 3, "merchant"),
]


def main() -> None:
    d = load_all()
    C.post("/v1/teardown")
    for s in ("category", "merchant", "customer", "trigger"):
        for cid, p in d[s].items():
            C.post("/v1/context", json={"scope": s, "context_id": cid, "version": 1, "payload": p})
    for tid, msgs, role in SCRIPTS:
        acts = C.post("/v1/tick", json={"now": "2026-09-26T10:00:00Z", "available_triggers": [tid]}).json()["actions"]
        if not acts:
            print("NO ACTION", tid)
            continue
        a = acts[0]
        print(f"\n=== {tid}\nBOT: {a['body']}")
        for i, m in enumerate(msgs, 2):
            r = C.post("/v1/reply", json={"conversation_id": a["conversation_id"], "merchant_id": a["merchant_id"],
                                          "customer_id": a["customer_id"], "from_role": role, "message": m, "turn_number": i}).json()
            print(f"USER: {m}\nBOT[{r['action']}]: {r.get('body', r.get('wait_seconds', ''))}   ({r['rationale'][:60]})")


if __name__ == "__main__":
    main()
