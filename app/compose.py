"""Pipeline: TRIGGER -> SELECT -> FACTS -> ANGLE -> WRITE -> VALIDATE -> send | fallback | abstain.

Deterministic end-to-end. The optional LLM polish (llm_writer.py) runs after this and can only
replace a body with one that passes the same validator against a narrower evidence package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .decide import Assessment, assess
from .evidence import CUSTOMER, DERIVED, MERCHANT, TRIGGER, Ledger, build_ledger
from .playbooks import Ctx, Draft, cust_lang, run_playbook
from .store import Store
import re

from .evidence import numeric_tokens
from .util import as_list, g, norm_text, short_id, stable_hash, utcnow
from .validator import Problem, hard, validate


@dataclass
class Candidate:
    trigger: dict
    merchant: dict | None
    category: dict | None
    customer: dict | None
    assessment: Assessment
    ledger: Ledger | None = None
    ctx: Ctx | None = None
    draft: Draft | None = None
    template_name: str = ""
    body: str = ""
    problems: list[Problem] = field(default_factory=list)
    abstain_reason: str = ""
    card: dict = field(default_factory=dict)
    source: str = "template"           # template | llm

    @property
    def ok(self) -> bool:
        return not self.abstain_reason and self.draft is not None

    @property
    def recipient(self) -> str:
        a = self.assessment
        if a.audience == "customer":
            return f"cust:{self.trigger.get('customer_id')}"
        return f"merch:{self.trigger.get('merchant_id') or g(self.trigger, 'payload', 'merchant_id')}"


def expected_names(c: Candidate) -> list[str]:
    L = c.ledger
    if c.assessment.audience == "customer":
        if not L:
            return []
        names = [n for n in (L.text("cu.parent"), L.text("cu.name")) if n]
        return names + [n.split(" ", 1)[1] for n in names if n.startswith(("Mr. ", "Mrs. ", "Ms. "))]
    return [L.text("m.salutation")] if L and L.text("m.salutation") else []


def check(c: Candidate, body: str, prior: set[str], allowed_ids: list[str] | None = None,
          required_ending: str | None = None) -> list[Problem]:
    taboo = [str(t) for t in as_list(g(c.category, "voice", "vocab_taboo"))]
    return validate(body, L=c.ledger, audience=c.assessment.audience,
                    send_as="merchant_on_behalf" if c.assessment.audience == "customer" else "vera",
                    cta_type=c.draft.cta_type if c.draft else "open_ended", taboo=taboo,
                    expected_names=expected_names(c), prior_bodies=prior, allowed_ids=allowed_ids,
                    required_ending=required_ending,
                    customer_lang=cust_lang(c.ctx) if (c.ctx and c.assessment.audience == "customer") else None)


def build_candidate(store: Store | None, trigger: dict, now: datetime | None,
                    contexts: dict | None = None) -> Candidate:
    """contexts (optional) = {'merchant':..., 'category':..., 'customer':...} for stateless use (bot.compose)."""
    # normalise shapes once, so nothing downstream can trip on a string where a dict was expected
    trigger = dict(trigger) if isinstance(trigger, dict) else {}
    trigger["payload"] = trigger.get("payload") if isinstance(trigger.get("payload"), dict) else {}
    for k in ("merchant_id", "customer_id", "kind", "id", "suppression_key"):
        if trigger.get(k) is not None and not isinstance(trigger.get(k), str):
            trigger[k] = str(trigger[k]) if isinstance(trigger[k], (int, float)) and not isinstance(trigger[k], bool) else None
    mid = trigger.get("merchant_id") or g(trigger, "payload", "merchant_id")
    cid = trigger.get("customer_id") or g(trigger, "payload", "customer_id")
    mid = mid if isinstance(mid, str) else None
    cid = cid if isinstance(cid, str) else None
    if contexts is not None:
        merchant, category, customer = contexts.get("merchant"), contexts.get("category"), contexts.get("customer")
    else:
        merchant = store.get("merchant", mid)
        category = store.category_for(merchant if isinstance(merchant, dict) else None, trigger)
        customer = store.get("customer", cid) if cid else None
    merchant = merchant if isinstance(merchant, dict) else None
    category = category if isinstance(category, dict) else None
    customer = customer if isinstance(customer, dict) else None
    sent_kinds = store.merchant_kinds.get(str(mid), set()) if store else set()
    used = store.used_suppression if store else set()
    a = assess(trigger, merchant, category, customer, now, sent_kinds, used,
               merchant_blocked=bool(store and mid in store.blocked_merchants),
               customer_blocked=bool(store and cid and cid in store.blocked_customers))
    c = Candidate(trigger, merchant, category, customer, a)
    if not a.ok:
        c.abstain_reason = a.abstain_reason
        return c

    ref: date | None = now.date() if now else None
    c.ledger = build_ledger(category, merchant, trigger, customer, a.digest_item, ref)
    if a.audience == "merchant" and not c.ledger.text("m.salutation"):
        c.abstain_reason = "no_addressee_name"          # never open with a blank or a guessed name
        return c
    if a.audience == "customer" and not c.ledger.text("m.name"):
        c.abstain_reason = "merchant_name_missing"      # a customer must know who is writing
        return c
    prior = store.prior_bodies(mid) if store else set()
    # never repeat what Vera already said to this merchant in the pushed conversation history
    vera_hist = [str(h.get("body") or "") for h in as_list((merchant or {}).get("conversation_history"))
                 if isinstance(h, dict) and h.get("from") == "vera"]
    prior = prior | {norm_text(b) for b in vera_hist}
    last_vera = vera_hist[-1] if vera_hist else ""

    # WRITE (deterministic) with up to 3 style variants until one validates
    last_problems: list[Problem] = []
    for attempt in range(3):
        seed = stable_hash([trigger.get("id"), mid, cid, attempt])
        ctx = Ctx(category, merchant, trigger, customer, c.ledger, a, ref, seed)
        draft, tname = run_playbook(ctx)
        if draft is None:
            c.abstain_reason = "insufficient_evidence_for_angle"
            return c
        body = draft.body
        problems = check(c_with(c, ctx, draft), body, prior)
        if last_vera and a.audience == "merchant" and similarity(body, last_vera) >= 0.5:
            problems.append(Problem("repeats_last_vera_message", True))
        if not hard(problems):
            c.ctx, c.draft, c.template_name, c.body, c.problems = ctx, draft, tname, body, problems
            break
        last_problems = problems
    else:
        c.abstain_reason = "validation_failed:" + ",".join(p.code for p in hard(last_problems))
        c.problems = last_problems
        return c

    # COUNTERFACTUAL GATE: the message must rest on this trigger or on merchant/customer-specific evidence,
    # not on a pile of generic category facts.
    roles = {c.ledger.get(fid).role for fid in c.draft.used if c.ledger.get(fid)}
    trigger_dependent = any(f.startswith(("t.", "d.")) for f in c.draft.used) or a.effective_kind not in ("unknown",)
    personal = bool(roles & {MERCHANT, CUSTOMER, TRIGGER, DERIVED})
    if not personal:
        c.abstain_reason = "no_merchant_specific_anchor"
        return c
    confidence = 0.9 if not a.placeholder else 0.7
    if a.reframed:
        confidence -= 0.15
    if a.score < 1.0:
        c.abstain_reason = f"low_decision_score:{a.score}"
        return c
    c.card = decision_card(c, confidence, trigger_dependent)
    return c


def c_with(c: Candidate, ctx: Ctx, draft: Draft) -> Candidate:
    c.ctx, c.draft = ctx, draft
    return c


def decision_card(c: Candidate, confidence: float, trigger_dependent: bool) -> dict:
    a, d = c.assessment, c.draft
    facts = [c.ledger.get(f) for f in d.used if c.ledger.get(f) and c.ledger.get(f).text]
    facts = [f for f in facts if f.id not in ("m.salutation", "m.name", "cu.name", "cu.parent")] or facts
    rank = {DERIVED: 0, TRIGGER: 1, MERCHANT: 2, CUSTOMER: 2}
    import re as _re
    facts = [f for f in facts if not _re.fullmatch(r"[\d\s.:+TZ₹,-]*|\d{4}-\d{2}-\d{2}.*", f.text)] or facts
    facts.sort(key=lambda f: (not any(ch.isdigit() for ch in f.text), len(f.text) < 12, rank.get(f.role, 3)))
    key_facts = list(dict.fromkeys(f.text for f in facts))[:4]
    return {
        "trigger_id": a.trigger_id, "declared_kind": a.kind, "effective_kind": a.effective_kind,
        "audience": a.audience, "send_as": "merchant_on_behalf" if a.audience == "customer" else "vera",
        "why_now": _why_now(c), "angle": d.angle, "lever": d.lever, "evidence": key_facts,
        "evidence_ids": list(d.used), "contradiction": a.contradiction or None, "placeholder_trigger": a.placeholder,
        "cta_type": d.cta_type, "action_on_yes": d.action_on_yes, "confidence": round(confidence, 2),
        "score": a.score, "score_parts": a.score_parts, "trigger_dependent": trigger_dependent,
    }


def _why_now(c: Candidate) -> str:
    a = c.assessment
    k = a.effective_kind.replace("_", " ")
    if a.placeholder:
        return f"{a.kind.replace('_', ' ')} event with no details; anchored on verified account data instead"
    return f"{k} event"


def similarity(a: str, b: str, n: int = 4) -> float:
    ta, tb = norm_text(a).split(), norm_text(b).split()
    sa = {tuple(ta[i:i + n]) for i in range(max(0, len(ta) - n + 1))}
    sb = {tuple(tb[i:i + n]) for i in range(max(0, len(tb) - n + 1))}
    return len(sa & sb) / max(1, min(len(sa), len(sb))) if sa and sb else 0.0


def evidence_in_body(c: Candidate, limit: int = 3) -> list[str]:
    """Facts the FINAL body actually states (after any rewrite), most informative first."""
    body = c.body or ""
    low, nums = body.lower(), numeric_tokens(body)
    facts = [c.ledger.get(f) for f in c.draft.used if c.ledger.get(f) and c.ledger.get(f).text]
    facts = [f for f in facts if f.id not in ("m.salutation", "m.name", "cu.name", "cu.parent")
             and "_" not in f.text and not re.fullmatch(r"[\d\s.,%₹+-]+", f.text)]
    rank = {DERIVED: 0, TRIGGER: 1, MERCHANT: 2, CUSTOMER: 2}
    shown = []
    for f in facts:
        t = f.text
        own = numeric_tokens(t)          # only the fact's own rendering, not its alternate forms
        if t.lower() in low or (own and own <= nums and len(t) < 90):
            shown.append(f)
    shown.sort(key=lambda f: (not any(ch.isdigit() for ch in f.text), len(f.text) < 8, rank.get(f.role, 3)))
    return list(dict.fromkeys(f.text for f in shown))[:limit]


def rationale(c: Candidate) -> str:
    """Built from the final body, so the rationale can never cite something the message doesn't say."""
    card, d = c.card, c.draft
    ev = "; ".join(evidence_in_body(c))
    parts = [f"{card['why_now'].capitalize()}: {d.angle}."]
    if ev:
        parts.append(f"Grounded in: {ev}.")
    if card.get("contradiction"):
        parts.append(f"Data check: {card['contradiction']}.")
    parts.append(f"One CTA ({d.cta_type}) aiming to {d.action_on_yes}." if d.action_on_yes else f"One CTA ({d.cta_type}).")
    return " ".join(parts)


def new_conversation_id(c: Candidate, taken: set[str] | dict) -> str:
    mid = c.trigger.get("merchant_id") or "x"
    base = f"conv_{short_id(mid)}_{c.assessment.effective_kind}"
    if c.assessment.audience == "customer":
        base += f"_{str(c.trigger.get('customer_id') or '').split('_')[1] if '_' in str(c.trigger.get('customer_id') or '') else 'c'}"
    for i in range(50):
        cid = f"{base}_{stable_hash([c.trigger.get('id'), i], 6)}"
        if cid not in taken:
            return cid
    return f"{base}_{stable_hash([utcnow().isoformat()], 8)}"


def to_action(c: Candidate, conversation_id: str) -> dict:
    a = c.assessment
    sup = str(c.trigger.get("suppression_key") or f"{a.kind}:{c.trigger.get('merchant_id')}:{c.trigger.get('customer_id') or ''}")
    params = [str(p) for p in (c.draft.template_params or []) if p is not None and str(p) != ""] or [c.body[:60]]
    return {
        "conversation_id": conversation_id,
        "merchant_id": c.trigger.get("merchant_id") or g(c.merchant, "merchant_id"),
        "customer_id": c.trigger.get("customer_id") if a.audience == "customer" else None,
        "send_as": "merchant_on_behalf" if a.audience == "customer" else "vera",
        "trigger_id": a.trigger_id,
        "template_name": c.template_name,
        "template_params": params,
        "body": c.body,
        "cta": c.draft.cta_type,
        "suppression_key": sup,
        "rationale": rationale(c),
    }
