"""Optional LLM wording layer (Gemini / any OpenAI-compatible model, temperature 0).

Code has already decided everything (trigger, facts, angle, CTA) and produced a validated template.
The LLM may only reword it, using ONLY the approved facts, and must keep the CTA sentence verbatim.
Each output is re-validated against that narrow evidence package; anything that fails keeps the template.

- polish_batch(): ONE call per tick for all selected messages.
- polish_reply(): one call per reply send (rule-based classification already chose the move).
"""

from __future__ import annotations

import json
import re

from .compose import Candidate, check
from .evidence import Ledger, numeric_tokens
from .llm import llm
from .playbooks import cust_lang
from .util import as_list, g, norm_text
from .validator import QUALIFYING, hard, last_sentence, validate

VOICE = {
    "dentists": "clinical peer to a doctor: precise, respectful, technical terms welcome, no hype",
    "salons": "warm and practical, like a friendly industry insider",
    "restaurants": "operator to operator: busy, practical, talks covers, orders, footfall",
    "gyms": "coach voice: energetic, disciplined, direct",
    "pharmacies": "trustworthy and precise, never alarmist",
}

BATCH_SYSTEM = """You polish WhatsApp messages for Vera, magicpin's assistant for Indian local businesses.
For EACH item you get: addressee, voice, language, banned words, the exact CTA sentence, the approved facts, and a draft.
Rewrite the draft so it reads naturally, like a sharp human colleague on WhatsApp. Hard rules per item:
- The draft's content is final: keep EVERY fact, number, date, source, insight and recommendation it contains. You improve flow only; never drop or shorten away a fact.
- Use ONLY that item's facts. Never add any number, price, date, name, offer, source, statistic or claim not in them.
- Keep every number exactly as written. Keep quoted offer titles verbatim, including their quotes.
- Start with the addressee exactly as given. No greeting preamble, no self-introduction, no URLs, no hashtags, no ALL CAPS hype.
- One central point, 2-4 short sentences, then the CTA sentence copied EXACTLY as the last sentence.
- Never use the banned words. Match the requested language (Hinglish = natural Hindi-English code-mix in Roman script).
Return JSON: {"items": [{"id": "<id>", "body": "<full message>"}]} with one entry per input item."""

REPLY_SYSTEM = """You polish one WhatsApp reply from Vera (magicpin's assistant) inside an ongoing conversation.
Rewrite everything before the final sentence so it sounds natural and brief; the final sentence must be copied EXACTLY.
Rules: keep every fact, number and quoted text from the reply; use only the facts and wording given (no new numbers,
prices, dates, names or claims); do not greet or re-introduce;
do not ask qualifying questions; keep the same language ({lang}); keep it to at most 3 short sentences.
Return JSON: {{"body": "<full reply>"}}"""


def _lang(c: Candidate) -> str:
    if c.assessment.audience == "customer":
        return {"hi": "Hindi written in Roman script", "mix": "Hinglish"}.get(cust_lang(c.ctx), "simple English")
    return "English with a light Hinglish touch" if c.ctx.hinglish else "English"


def _item_ids(c: Candidate) -> list[str]:
    extra = [i for i in ("m.salutation", "m.name", "m.locality", "cu.name", "cu.parent") if c.ledger.has(i)]
    return list(dict.fromkeys(c.draft.used + extra))


def _item(c: Candidate, idx: int) -> dict:
    ids = _item_ids(c)
    name = (c.ledger.text("cu.parent") or c.ledger.text("cu.name")) if c.assessment.audience == "customer" else c.ledger.text("m.salutation")
    return {
        "id": str(idx),
        "addressee": name or "(none)",
        "voice": VOICE.get(c.ctx.slug, "peer, practical") + ("; written as the business to its customer" if c.assessment.audience == "customer" else ""),
        "language": _lang(c),
        "banned_words": [str(t) for t in as_list(g(c.category, "voice", "vocab_taboo"))][:8],
        "cta_sentence": c.draft.cta,
        "angle": c.draft.angle,
        "facts": [c.ledger.get(i).text for i in ids if c.ledger.get(i) and c.ledger.get(i).text],
        "draft": c.body,
    }


async def polish_batch(cands: list[Candidate], priors: list[set[str]], timeout: float) -> tuple[dict[int, str], dict[int, str]]:
    """Returns ({index: accepted_body}, {index: fallback_reason})."""
    accepted: dict[int, str] = {}
    reasons: dict[int, str] = {}
    todo = [(i, c) for i, c in enumerate(cands) if c.ok and "\n" not in c.body]  # multi-line drafts are artifacts: keep verbatim
    for i, c in enumerate(cands):
        if c.ok and "\n" in c.body:
            reasons[i] = "artifact_kept_verbatim"
    if not todo or not llm.enabled:
        for i, _ in todo:
            reasons[i] = "llm_disabled"
        return accepted, reasons
    payload = json.dumps({"items": [_item(c, i) for i, c in todo]}, ensure_ascii=False)
    try:
        out = await llm.complete_json(BATCH_SYSTEM, payload, timeout=timeout, max_tokens=min(4000, 260 * len(todo) + 200))
    except Exception:
        out = None
    if not isinstance(out, dict) or not isinstance(out.get("items"), list):
        why = llm.stats.get("last_error") or "no_or_bad_response"
        for i, _ in todo:
            reasons[i] = f"llm_unavailable:{why}"
        return accepted, reasons
    got = {str(x.get("id")): str(x.get("body") or "").strip() for x in out["items"] if isinstance(x, dict)}
    for i, c in todo:
        body = got.get(str(i), "")
        if not body:
            reasons[i] = "missing_in_batch"
            continue
        if norm_text(body) == norm_text(c.body):
            reasons[i] = "unchanged"
            continue
        lost = dropped_facts(c.body, body)
        if lost:
            reasons[i] = "dropped_facts:" + ",".join(sorted(lost))[:80]
            continue
        probs = hard(check(c, body, priors[i], allowed_ids=_item_ids(c), required_ending=c.draft.cta))
        if probs:
            reasons[i] = "validator_rejected:" + ",".join(p.code for p in probs)
        else:
            accepted[i] = body
    return accepted, reasons


def dropped_facts(template: str, rewrite: str) -> set[str]:
    """Coverage guard: every number and every quoted offer/title in the template must survive the rewrite,
    so the LLM can improve flow but never quietly remove the specifics the decision was based on."""
    lost = numeric_tokens(template) - numeric_tokens(rewrite)
    for q in re.findall(r"(?:(?<=[\s(])|^)'([^'\n]{3,80})'(?=[\s.,;:)?!]|$)|\"([^\"]{3,})\"", template):
        t = q[0] or q[1]
        if t not in rewrite:
            lost.add(t[:24])
    return lost


async def polish(c: Candidate, prior: set[str], timeout: float) -> str | None:
    """Single-candidate convenience wrapper (kept for tests / offline use)."""
    try:
        acc, _ = await polish_batch([c], [prior], timeout)
    except Exception:
        return None
    return acc.get(0)


async def polish_reply(body: str, *, lang: str, ledger: Ledger, audience: str, cta: str, prior: set[str],
                       taboo: list[str], commitment: bool, timeout: float) -> tuple[str | None, str]:
    """Reword a rule-chosen reply. Returns (accepted_body | None, reason)."""
    if not llm.enabled:
        return None, "llm_disabled"
    if "\n" in body or len(body) < 60:
        return None, "kept_verbatim"                      # drafts/artifacts and one-liners stay exact
    ending = last_sentence(body)
    sys = REPLY_SYSTEM.format(lang={"mix": "Hinglish", "hi": "Hindi in Roman script"}.get(lang, "English"))
    try:
        out = await llm.complete_json(sys, json.dumps({"reply": body, "final_sentence": ending}, ensure_ascii=False),
                                      timeout=timeout, max_tokens=300)
    except Exception:
        out = None
    new = str((out or {}).get("body") or "").strip() if isinstance(out, dict) else ""
    if not new:
        return None, f"llm_unavailable:{llm.stats.get('last_error') or 'no_response'}"
    lost = dropped_facts(body, new)
    if lost:
        return None, "dropped_facts:" + ",".join(sorted(lost))[:80]
    probs = hard(validate(new, L=ledger, audience=audience, send_as="merchant_on_behalf" if audience == "customer" else "vera",
                          cta_type=cta, taboo=taboo, expected_names=[], prior_bodies=prior, required_ending=ending,
                          forbid_qualifying=commitment))
    if commitment and any(q in new.lower() for q in QUALIFYING):
        return None, "validator_rejected:qualifying"
    if probs:
        return None, "validator_rejected:" + ",".join(p.code for p in probs)
    return new, "accepted"
