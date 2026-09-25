"""Tick a chosen set of test pairs in ONE tick (one batched LLM call) and print body, rationale and source.
   BOT_URL=... python -m tests.samples T09 T21 ..."""

from __future__ import annotations

import os
import sys

import httpx

from tests.dataset import load_all

C = httpx.Client(base_url=os.environ.get("BOT_URL", "http://127.0.0.1:8080"), timeout=35)


def main(ids: list[str]) -> None:
    d = load_all()
    pairs = {p["test_id"]: p for p in d["pairs"]}
    C.post("/v1/teardown")
    for s in ("category", "merchant", "customer", "trigger"):
        for cid, p in d[s].items():
            C.post("/v1/context", json={"scope": s, "context_id": cid, "version": 1, "payload": p})
    tids = [pairs[i]["trigger_id"] for i in ids]
    acts = C.post("/v1/tick", json={"now": "2026-09-26T10:00:00Z", "available_triggers": tids}).json()["actions"]
    dec = C.get("/v1/debug/decisions", params={"limit": 50}).json()
    src = {x["trigger_id"]: x.get("source") for x in dec["decisions"] if x["outcome"] == "sent"}
    fb = {x["ref"]: x["reason"] for x in dec["llm_fallbacks"]}
    by_t = {a["trigger_id"]: a for a in acts}
    for i in ids:
        tid = pairs[i]["trigger_id"]
        a = by_t.get(tid)
        if not a:
            print(f"\n## {i} ({tid}) — no action"); continue
        print(f"\n## {i} [{src.get(tid)}{' / fallback: ' + fb[tid] if tid in fb else ''}]  cta={a['cta']} send_as={a['send_as']}")
        print(a["body"])
        print("RATIONALE:", a["rationale"])
    llm = dec["llm"]
    print("\nLLM stats:", {k: llm.get(k) for k in ("calls", "ok", "errors", "timeouts", "http_429", "cache_hits", "rate_limited", "last_error")})


if __name__ == "__main__":
    main(sys.argv[1:])
