"""Interactive demo at /demo for human visitors.

Fully isolated from the judged bot: it uses its own in-memory Store loaded with the bundled sample dataset,
so demo clicks never change /v1/healthz counts, suppression, conversations or anything the judge sees.
Replies use the deterministic templates only (no LLM calls), so the demo can't eat the Gemini quota.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .compose import build_candidate, new_conversation_id, rationale, to_action
from .playbooks import cust_lang
from .replies import handle_reply
from .store import Conversation, Store

router = APIRouter()
DATA = Path(__file__).resolve().parents[1] / "dataset"
DEMO_NOW = datetime(2026, 4, 26, 10, 30, tzinfo=timezone.utc)   # the sample dataset's own week
_store: Store | None = None
_triggers: list[dict] = []


def _load() -> Store:
    global _store, _triggers
    if _store is not None and len(_store.conversations) < 500:
        return _store
    s = Store()
    for f in sorted((DATA / "categories").glob("*.json")):
        c = json.loads(f.read_text(encoding="utf-8"))
        s.put_context("category", c["slug"], 1, c)
    for name, scope, key in (("merchants_seed.json", "merchant", "merchant_id"), ("customers_seed.json", "customer", "customer_id"),
                             ("triggers_seed.json", "trigger", "id")):
        rows = json.loads((DATA / name).read_text(encoding="utf-8"))[scope + "s"]
        for r in rows:
            s.put_context(scope, r[key], 1, r)
    _triggers = rows
    _store = s
    return s


def _options(s: Store) -> list[dict]:
    out = []
    for t in _triggers:
        m = s.get("merchant", t.get("merchant_id")) or {}
        cu = s.get("customer", t.get("customer_id")) if t.get("customer_id") else None
        who = (m.get("identity") or {}).get("name", t.get("merchant_id"))
        label = f"{who} · {t['kind'].replace('_', ' ')}"
        if cu:
            label += f" (to customer {(cu.get('identity') or {}).get('name', '')})"
        out.append({"id": t["id"], "label": label})
    return out


@router.get("/demo")
async def demo_page():
    return HTMLResponse(PAGE)


@router.get("/demo/api/triggers")
async def demo_triggers():
    try:
        return {"triggers": _options(_load())}
    except Exception as e:
        return JSONResponse({"error": f"demo data unavailable: {type(e).__name__}"}, 500)


@router.post("/demo/api/compose")
async def demo_compose(request: Request):
    s = _load()
    try:
        tid = str((await request.json()).get("trigger_id") or "")
    except Exception:
        tid = ""
    trig = s.get("trigger", tid)
    if not trig:
        return JSONResponse({"error": "unknown trigger"}, 400)
    s.used_suppression.clear()          # let visitors replay the same scenario
    s.merchant_bodies.clear()
    c = build_candidate(s, trig, DEMO_NOW)
    if not c.ok:
        return {"sent": False, "reason": c.abstain_reason.replace("_", " "),
                "explain": "Vera decided not to send. Staying silent is a deliberate choice when the trigger doesn't fit the data."}
    conv_id = new_conversation_id(c, s.conversations)
    a = to_action(c, conv_id)
    conv = Conversation(conv_id, a["merchant_id"], a["customer_id"], c.assessment.audience, trigger_id=trig["id"],
                        kind=c.assessment.effective_kind, topic=c.draft.angle, action_on_yes=c.draft.action_on_yes,
                        yes_artifact=c.draft.yes_artifact,
                        lang=cust_lang(c.ctx) if c.assessment.audience == "customer" else ("mix" if c.ctx.hinglish else "en"),
                        action_hi=c.draft.action_hi, final_en=c.draft.final_en, final_hi=c.draft.final_hi, explain=c.draft.explain)
    if c.draft.cta_type == "binary_confirm_cancel" and c.draft.yes_artifact:
        conv.stage = "action"
    conv.card = {**c.card, "_ledger": c.ledger,
                 "slots": [c.ledger.get(f).text for f in c.draft.used if f.startswith("t.slot.") and c.ledger.get(f)]}
    s.conversations[conv_id] = conv
    s.remember_send(conv, a["body"])
    return {"sent": True, "conversation_id": conv_id, "body": a["body"], "rationale": rationale(c), "cta": a["cta"],
            "send_as": a["send_as"]}


@router.post("/demo/api/reply")
async def demo_reply(request: Request):
    s = _load()
    try:
        b = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, 400)
    conv = s.conversations.get(str(b.get("conversation_id") or ""))
    if not conv:
        return JSONResponse({"error": "start a conversation first"}, 400)
    msg = str(b.get("message") or "")[:1000]
    out = handle_reply(s, {"conversation_id": conv.conversation_id, "merchant_id": conv.merchant_id, "customer_id": conv.customer_id,
                           "from_role": "customer" if conv.customer_id else "merchant", "message": msg}, DEMO_NOW)
    return out


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vera Demo</title>
<style>
:root{--bg:#eef1f5;--card:#fff;--text:#1c2230;--muted:#5b6475;--line:#dde2ea;--accent:#4f46e5;--vera:#ffffff;--me:#d9fdd3;--chat:#efeae2;--note:#fff7d6}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--card:#171b23;--text:#e7eaf0;--muted:#9aa3b2;--line:#2a303b;--accent:#8b86ff;--vera:#202c33;--me:#005c4b;--chat:#0b141a;--note:#3a3320}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,Arial,sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:24px 16px 40px}
h1{margin:0 0 4px;font-size:24px}.sub{color:var(--muted);margin:0 0 16px}.sub a{color:var(--accent)}
.bar{display:flex;gap:8px;flex-wrap:wrap;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px}
select{flex:1;min-width:220px;padding:10px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--text);font-size:14px}
button{padding:10px 16px;border-radius:10px;border:0;background:var(--accent);color:#fff;font-weight:600;cursor:pointer;font-size:14px}
button.ghost{background:transparent;color:var(--accent);border:1px solid var(--line);font-weight:500}
.chat{background:var(--chat);border-radius:14px;margin-top:14px;padding:16px;min-height:320px;display:flex;flex-direction:column;gap:10px}
.msg{max-width:78%;padding:9px 12px;border-radius:10px;white-space:pre-wrap;box-shadow:0 1px 1px rgba(0,0,0,.08)}
.vera{align-self:flex-start;background:var(--vera)}.me{align-self:flex-end;background:var(--me)}
.who{font-size:12px;font-weight:700;color:var(--accent);margin-bottom:2px}
.status{align-self:center;font-size:13px;color:var(--muted);background:var(--card);padding:5px 12px;border-radius:999px;border:1px solid var(--line)}
.why{align-self:flex-start;max-width:90%;font-size:12.5px;color:var(--muted);background:var(--note);padding:8px 11px;border-radius:10px}
.input{display:flex;gap:8px;margin-top:10px}.input input{flex:1;padding:11px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--text);font-size:14px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}.chips button{padding:6px 11px;font-size:13px}
.empty{color:var(--muted);text-align:center;margin:auto}
</style></head><body><div class="wrap">
<h1>Try Vera</h1>
<p class="sub">Pick a scenario from magicpin's sample data, see the message Vera writes and why, then reply as the merchant.
This demo is separate from the judged bot. <a href="/">About</a> · <a href="https://github.com/dwivediansh7/vera-bot" target="_blank" rel="noopener">Source code</a></p>
<div class="bar"><select id="trig"><option>Loading scenarios…</option></select><button id="go">Generate message</button></div>
<div class="chat" id="chat"><div class="empty">Choose a scenario and click Generate message.</div></div>
<div class="input"><input id="txt" placeholder="Reply as the merchant (or customer)…" disabled><button id="send" disabled>Send</button></div>
<div class="chips" id="chips"></div>
</div>
<script>
const $=id=>document.getElementById(id);let conv=null;
const QUICK=["Yes, let's do it","Why?","This is too expensive","Thank you for contacting us! Our team will respond shortly.","Can you help me with GST?","haan kar do","Stop messaging me"];
function add(cls,text,who){const d=document.createElement('div');d.className='msg '+cls;if(who){const w=document.createElement('div');w.className='who';w.textContent=who;d.appendChild(w)}d.appendChild(document.createTextNode(text));$('chat').appendChild(d);$('chat').scrollTop=1e9}
function note(cls,text){const d=document.createElement('div');d.className=cls;d.textContent=text;$('chat').appendChild(d)}
function enable(on){$('txt').disabled=!on;$('send').disabled=!on;$('chips').innerHTML='';if(on)QUICK.forEach(q=>{const b=document.createElement('button');b.className='ghost';b.textContent=q;b.onclick=()=>reply(q);$('chips').appendChild(b)})}
fetch('/demo/api/triggers').then(r=>r.json()).then(d=>{$('trig').innerHTML='';d.triggers.forEach(t=>{const o=document.createElement('option');o.value=t.id;o.textContent=t.label;$('trig').appendChild(o)})});
$('go').onclick=async()=>{$('chat').innerHTML='';enable(false);conv=null;
 const r=await fetch('/demo/api/compose',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({trigger_id:$('trig').value})});const d=await r.json();
 if(!d.sent){note('status','Vera chose not to send: '+d.reason);note('why',d.explain);return}
 add('vera',d.body,d.send_as==='vera'?'Vera':'Sent on behalf of the business');note('why','Why this message: '+d.rationale);conv=d.conversation_id;enable(true)};
async function reply(text){if(!conv||!text.trim())return;add('me',text);$('txt').value='';
 const r=await fetch('/demo/api/reply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({conversation_id:conv,message:text})});const d=await r.json();
 if(d.action==='send')add('vera',d.body,'Vera');else if(d.action==='wait')note('status','Vera waits '+Math.round(d.wait_seconds/3600*10)/10+' h before trying again');else if(d.action==='end'){note('status','Vera ends the conversation');enable(false)}
 if(d.rationale)note('why',d.rationale.replace(/^\[[a-z_]+\]\s*/,''))}
$('send').onclick=()=>reply($('txt').value);$('txt').addEventListener('keydown',e=>{if(e.key==='Enter')reply($('txt').value)});
</script></body></html>"""
