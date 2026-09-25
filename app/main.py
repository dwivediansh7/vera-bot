"""HTTP surface: /v1/context, /v1/tick, /v1/reply, /v1/healthz, /v1/metadata (+ /v1/teardown).

Every handler returns valid JSON on every path. Tick and reply work is time-boxed; the LLM is optional
and never on the critical path for correctness.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, Request
from pathlib import Path

from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from .landing import render_page
from .demo import router as demo_router

from . import __version__
from .compose import Candidate, build_candidate, new_conversation_id, to_action
from .config import settings
from .llm import llm
from .llm_writer import polish_batch, polish_reply
from .playbooks import cust_lang
from .replies import _allowed_ledger, _merchant_ledger, handle_reply
from .store import SCOPES, Conversation, store
from .util import iso_now, norm_text, parse_dt, utcnow

log = logging.getLogger("vera")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="Vera", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(demo_router)


# ------------------------------------------------------------------ helpers

async def _json(request: Request) -> tuple[Any, str | None]:
    try:
        raw = await request.body()
        if len(raw) > settings.max_payload_bytes + 64 * 1024:
            return None, "payload_too_large"
        return json.loads(raw.decode("utf-8") or "null"), None
    except Exception as e:
        return None, f"invalid_json: {type(e).__name__}"


def _now(v: Any) -> datetime:
    dt = parse_dt(v) if isinstance(v, str) else None
    if dt is None:
        return utcnow()
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@app.middleware("http")
async def guard(request: Request, call_next):
    t0 = time.time()
    try:
        resp = await call_next(request)
    except Exception:  # last line of defence: never a bare 500 page
        store.counters["errors"] += 1
        log.exception("unhandled error on %s", request.url.path)
        path = request.url.path
        if path.endswith("/tick"):
            return JSONResponse({"actions": []})
        if path.endswith("/reply"):
            return JSONResponse({"action": "wait", "wait_seconds": 1800, "rationale": "internal error; backing off safely"})
        return JSONResponse({"status": "error"}, status_code=500)
    ms = (time.time() - t0) * 1000
    if ms > 5000:
        log.warning("slow %s %.0fms", request.url.path, ms)
    return resp


# ------------------------------------------------------------------ health + metadata

_README = Path(__file__).resolve().parents[1] / "README.md"


@app.get("/")
async def root():
    """Human-readable landing page: README.md rendered as styled HTML; JSON fallback if the file is absent."""
    try:
        return HTMLResponse(render_page())
    except Exception:
        return {"service": "vera", "status": "ok", "endpoints": ["/v1/healthz", "/v1/metadata", "/v1/context", "/v1/tick", "/v1/reply"]}


@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - store.started),
        "contexts_loaded": store.counts(),
        "conversations": len(store.conversations),
        "counters": store.counters,
        "llm": {k: v for k, v in llm.status().items() if k in ("enabled", "provider", "model", "circuit_open", "ok", "errors", "timeouts")},
        "last_ok": store.last_ok,
        "version": __version__,
    }


@app.get("/v1/metadata")
async def metadata():
    model = settings.llm_model if settings.llm_enabled else "deterministic-templates (no LLM)"
    return {
        "team_name": settings.team_name,
        "team_members": settings.team_members,
        "model": model,
        "approach": ("Deterministic evidence-ledger pipeline: trigger arbitration with contradiction checks and consent gating -> "
                     "provenance-tagged fact ledger -> per-trigger playbook (one hook, one CTA) -> claim-level validator "
                     "(every number/price/date must trace to context) -> optional constrained LLM polish that must re-pass "
                     "the validator; rule-based reply state machine for auto-replies, commitment, opt-out and off-topic."),
        "contact_email": settings.contact_email,
        "version": __version__,
        "submitted_at": settings.submitted_at,
    }


# ------------------------------------------------------------------ context

@app.post("/v1/context")
async def context(request: Request):
    store.counters["context"] += 1
    body, err = await _json(request)
    if err:
        return JSONResponse({"accepted": False, "reason": "payload_too_large" if "large" in err else "invalid_json", "details": err}, 400)
    if not isinstance(body, dict):
        return JSONResponse({"accepted": False, "reason": "invalid_body", "details": "expected a JSON object"}, 400)
    scope = body.get("scope")
    if scope not in SCOPES:
        return JSONResponse({"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {list(SCOPES)}"}, 400)
    cid = body.get("context_id")
    if not isinstance(cid, str) or not cid.strip():
        return JSONResponse({"accepted": False, "reason": "invalid_context_id", "details": "context_id must be a non-empty string"}, 400)
    version = body.get("version")
    if isinstance(version, bool) or not isinstance(version, (int, float)) or int(version) != version:
        return JSONResponse({"accepted": False, "reason": "invalid_version", "details": "version must be an integer"}, 400)
    payload = body.get("payload")
    if not isinstance(payload, dict):
        return JSONResponse({"accepted": False, "reason": "invalid_payload", "details": "payload must be an object"}, 400)
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > settings.max_payload_bytes:
        return JSONResponse({"accepted": False, "reason": "payload_too_large", "details": "payload exceeds 500 KB"}, 400)
    if scope == "trigger" and not payload.get("id"):
        payload = {**payload, "id": cid}
    ok, current = store.put_context(scope, cid.strip(), int(version), payload)
    if not ok:
        return JSONResponse({"accepted": False, "reason": "stale_version", "current_version": current}, 409)
    store.last_ok["context"] = iso_now()
    return {"accepted": True, "ack_id": f"ack_{cid}_v{int(version)}", "stored_at": iso_now()}


# ------------------------------------------------------------------ tick

@app.post("/v1/tick")
async def tick(request: Request):
    t0 = time.time()
    store.counters["tick"] += 1
    body, err = await _json(request)
    if err or not isinstance(body, dict):
        return {"actions": []}
    now = _now(body.get("now"))
    ids = [str(x) for x in (body.get("available_triggers") or []) if isinstance(x, (str, int))]
    ids = list(dict.fromkeys(ids))[:200]

    chosen: dict[str, Candidate] = {}
    for tid in ids:
        trig = store.get("trigger", tid)
        if not trig:
            _log(tid, "unknown_trigger_id", now)
            continue
        if not trig.get("id"):
            trig = {**trig, "id": tid}
        try:
            c = build_candidate(store, trig, now)
        except Exception as e:
            store.counters["errors"] += 1
            log.exception("compose failed for %s", tid)
            _log(tid, f"compose_error:{type(e).__name__}", now)
            continue
        if not c.ok:
            store.counters["abstentions"] += 1
            _log(tid, c.abstain_reason, now, c)
            continue
        mid = str(trig.get("merchant_id") or "")
        wait_until = store.merchant_wait_until.get(mid)
        if c.assessment.audience == "merchant" and wait_until and now < wait_until and c.assessment.score < 5:
            _log(tid, "merchant_in_wait_window", now, c)
            continue
        key = c.recipient
        cur = chosen.get(key)
        if cur is None or c.assessment.score > cur.assessment.score:
            if cur is not None:
                _log(cur.assessment.trigger_id, f"outranked_by:{tid}", now, cur)
            chosen[key] = c
        else:
            _log(tid, f"outranked_by:{cur.assessment.trigger_id}", now, c)

    # cap: highest urgency first (score breaks ties); anything cut is logged and NOT suppressed, so it goes next tick
    ordered = sorted(chosen.values(), key=lambda c: (-(c.assessment.score_parts.get("urgency") or 0), -c.assessment.score))
    ranked = ordered[: settings.max_actions_per_tick]
    for c in ordered[settings.max_actions_per_tick:]:
        _log(c.assessment.trigger_id, "deferred_by_cap:next_tick", now, c)

    # Outbound is template-only by default (LLM_OUTBOUND=off). If re-enabled: ONE batched call per tick,
    # strictly inside the remaining budget, every rewrite re-validated.
    if llm.enabled and settings.llm_outbound and ranked:
        remaining = settings.tick_budget_s - (time.time() - t0)
        reasons: dict[int, str] = {}
        accepted: dict[int, str] = {}
        if remaining > 1.5:
            budget = min(settings.llm_timeout_s, remaining - 0.5)
            priors = [store.prior_bodies(c.trigger.get("merchant_id")) for c in ranked]
            try:
                accepted, reasons = await asyncio.wait_for(polish_batch(ranked, priors, budget), timeout=budget + 0.3)
            except Exception as e:
                reasons = {i: f"llm_timeout_or_error:{type(e).__name__}" for i in range(len(ranked))}
        else:
            reasons = {i: "no_time_budget_left" for i in range(len(ranked))}
        for i, c in enumerate(ranked):
            if i in accepted:
                c.body, c.source = accepted[i], "llm"
                store.counters["llm_used"] += 1
            else:
                store.counters["fallbacks"] += 1
                _note_fallback("tick", c.assessment.trigger_id, reasons.get(i, "unknown"))

    actions = []
    for c in ranked:
        conv_id = new_conversation_id(c, store.conversations)
        action = to_action(c, conv_id)
        _register(c, action, now)
        actions.append(action)
        _log(c.assessment.trigger_id, "sent", now, c, action)
    store.counters["actions"] += len(actions)
    store.last_ok["tick"] = iso_now()
    return {"actions": actions}


def _register(c: Candidate, action: dict, now: datetime) -> None:
    a = c.assessment
    lang = cust_lang(c.ctx) if a.audience == "customer" else ("mix" if c.ctx.hinglish else "en")
    conv = Conversation(action["conversation_id"], action["merchant_id"], action["customer_id"], a.audience,
                        trigger_id=a.trigger_id, kind=a.effective_kind, topic=c.draft.angle,
                        action_on_yes=c.draft.action_on_yes, yes_artifact=c.draft.yes_artifact, lang=lang,
                        action_hi=c.draft.action_hi, final_en=c.draft.final_en, final_hi=c.draft.final_hi,
                        explain=c.draft.explain)
    if c.draft.cta_type == "binary_confirm_cancel" and c.draft.yes_artifact:
        conv.stage = "action"   # the artifact was already delivered in the opening message
    conv.card = {**c.card, "_ledger": c.ledger, "slots": [c.ledger.get(f).text for f in c.draft.used
                                                           if f.startswith("t.slot.") and c.ledger.get(f)]}
    store.conversations[conv.conversation_id] = conv
    store.remember_send(conv, action["body"])
    conv.turns.append({"from": "bot", "body": action["body"], "at": now.isoformat()})
    if action.get("suppression_key"):
        store.used_suppression.add(action["suppression_key"])
    store.merchant_kinds.setdefault(str(action["merchant_id"]), set()).add(a.effective_kind)


def _note_fallback(where: str, ref: str, reason: str) -> None:
    store.llm_fallbacks.append({"at": iso_now(), "where": where, "ref": ref, "reason": reason})
    if len(store.llm_fallbacks) > 500:
        del store.llm_fallbacks[:100]


def _log(tid: str, outcome: str, now: datetime, c: Candidate | None = None, action: dict | None = None) -> None:
    entry = {"at": now.isoformat(), "trigger_id": tid, "outcome": outcome}
    if c is not None:
        entry["kind"] = c.assessment.kind
        entry["effective_kind"] = c.assessment.effective_kind
        entry["score"] = c.assessment.score
        if c.assessment.contradiction:
            entry["contradiction"] = c.assessment.contradiction
    if action:
        entry["conversation_id"] = action["conversation_id"]
        entry["source"] = c.source if c else "template"
    store.decision_log.append(entry)
    if len(store.decision_log) > 5000:
        del store.decision_log[:1000]


# ------------------------------------------------------------------ reply

@app.post("/v1/reply")
async def reply(request: Request):
    store.counters["reply"] += 1
    body, err = await _json(request)
    if err or not isinstance(body, dict):
        return {"action": "wait", "wait_seconds": 600, "rationale": "Unreadable reply payload; waiting."}
    now = _now(body.get("received_at"))
    try:
        out = handle_reply(store, body, now)  # pure CPU, milliseconds; runs on the loop so state updates stay atomic
    except Exception:
        store.counters["errors"] += 1
        log.exception("reply failed")
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Could not process this turn safely; backing off."}
    if out.get("action") == "wait":
        mid = str(body.get("merchant_id") or (store.conversations.get(str(body.get("conversation_id"))) or Conversation("", None, None, "merchant")).merchant_id or "")
        if mid:
            store.merchant_wait_until[mid] = now + timedelta(seconds=int(out.get("wait_seconds", 0)))
    if out.get("action") == "send" and not str(out.get("body") or "").strip():
        out = {"action": "wait", "wait_seconds": 1800, "rationale": "Nothing grounded to add; waiting."}
    if out.get("action") == "send" and llm.enabled:
        await _polish_reply(body, out)
    store.last_ok["reply"] = iso_now()
    return out


async def _polish_reply(req: dict, out: dict) -> None:
    """Rules already chose the move and wrote a valid template; Gemini may only reword it."""
    conv = store.conversations.get(str(req.get("conversation_id") or ""))
    if conv is None:
        return
    old = out["body"]
    cls = str(out.get("rationale", ""))[1:].split("]", 1)[0]
    try:
        L, _, category = _merchant_ledger(store, conv)
        allowed = _allowed_ledger(conv, L)
        prior = set(conv.sent_bodies) - {norm_text(old)}
        taboo = [str(t) for t in ((category or {}).get("voice") or {}).get("vocab_taboo", []) or []]
        new, why = await asyncio.wait_for(
            polish_reply(old, lang=conv.lang, ledger=allowed, audience=conv.audience, cta=out.get("cta", "none"),
                         prior=prior, taboo=taboo, commitment=(cls == "commitment"), timeout=settings.llm_timeout_s),
            timeout=settings.llm_timeout_s + 0.5)
    except Exception as e:
        new, why = None, f"llm_timeout_or_error:{type(e).__name__}"
    if new:
        conv.sent_bodies.discard(norm_text(old))
        store.merchant_bodies.get(conv.merchant_id or "", set()).discard(norm_text(old))
        store.remember_send(conv, new)
        conv.bot_sends -= 1                      # remember_send counted it again
        if conv.turns and conv.turns[-1].get("body") == old:
            conv.turns[-1]["body"] = new
        out["body"] = new
        store.counters["llm_used"] += 1
    else:
        store.counters["fallbacks"] += 1
        _note_fallback("reply", conv.conversation_id, why)


# ------------------------------------------------------------------ teardown / debug

@app.post("/v1/teardown")
async def teardown():
    store.reset()
    return {"ok": True, "wiped_at": iso_now()}


@app.get("/v1/debug/decisions")
async def debug_decisions(limit: int = 100):
    if not settings.debug_endpoints:
        return JSONResponse({"error": "disabled"}, 404)
    return {"decisions": store.decision_log[-limit:], "llm_fallbacks": store.llm_fallbacks[-limit:],
            "llm": llm.status()}
