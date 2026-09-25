"""Brief §7.1 interface: stateless compose() over the four contexts.

    from bot import compose
    compose(category, merchant, trigger, customer=None) -> {body, cta, send_as, suppression_key, rationale}

Deterministic (no LLM on this path). If the trigger should not be acted on (contradiction, consent,
category mismatch, missing evidence) it returns an empty body with the abstention reason in rationale.
The HTTP service (app/main.py) is the primary submission; this module is for offline/batch use.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.compose import build_candidate, rationale


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None,
            now: datetime | None = None) -> dict:
    trigger = dict(trigger or {})
    trigger.setdefault("merchant_id", (merchant or {}).get("merchant_id"))
    if customer and not trigger.get("customer_id"):
        trigger["customer_id"] = customer.get("customer_id")
    c = build_candidate(None, trigger, now or datetime.now(timezone.utc),
                        contexts={"category": category, "merchant": merchant, "customer": customer})
    sup = str(trigger.get("suppression_key") or f"{trigger.get('kind')}:{trigger.get('merchant_id')}")
    if not c.ok:
        return {"body": "", "cta": "none", "send_as": "vera", "suppression_key": sup,
                "rationale": f"Abstained: {c.abstain_reason}"}
    return {
        "body": c.body,
        "cta": c.draft.cta_type,
        "send_as": "merchant_on_behalf" if c.assessment.audience == "customer" else "vera",
        "suppression_key": sup,
        "rationale": rationale(c),
    }
