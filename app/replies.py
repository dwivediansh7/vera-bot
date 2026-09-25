"""Reply engine: deterministic classifier + per-conversation state machine.

Order of precedence: auto-reply > opt-out/hostile > commitment > later > off-topic > question > decline > other.
Bodies are grounded in the conversation's evidence ledger, never claim work the system did not do,
and never repeat within a conversation.
"""

from __future__ import annotations

import re
from datetime import datetime

from .decide import Assessment
from .evidence import Ledger, build_ledger
from .playbooks import Ctx, _finals, best_lever
from .store import Conversation, Store
from .util import g, norm_text, stable_hash
from .validator import hard, validate

AUTO_PHRASES = [
    "thank you for contacting", "thanks for contacting", "thank you for reaching out", "thanks for reaching out",
    "thank you for your message", "thanks for your message", "we will get back", "we'll get back", "will respond shortly",
    "will get back to you", "respond as soon as", "our team will", "team will contact", "automated", "auto-reply",
    "autoreply", "auto reply", "out of office", "currently unavailable", "business hours", "we are closed",
    "this is an automated", "aapki jaankari ke liye", "hamari team", "team tak pahuncha", "jald hi sampark",
    "jald hi aapse", "sampark karenge", "automated assistant", "do not reply", "message has been received",
]
OPT_OUT = re.compile(
    r"\b(stop|unsubscribe|don'?t (message|text|contact|send)|do not (message|text|contact|send)|stop (messaging|sending|texting)|"
    r"not interested|no interest|remove me|leave me alone|band karo|mat bhejo|message mat|nahi chahiye|mujhe nahi chahiye|"
    r"block|spam|spamming|useless|bothering|harass|waste of (my )?time|irritat|pareshan|bakwas|bekaar|bekar|idiot|stupid|"
    r"shut up|nonsense|get lost|fraud|scam|chup)\b", re.I)
NEGATION = re.compile(r"\b(not|no|don'?t|dont|nahi|nahin|mat|never|na)\b", re.I)
COMMIT = re.compile(
    r"\b(yes|yep|yeah|yup|sure|ok|okay|okk|okie|haan|han|haa|ha ji|haan ji|theek hai|thik hai|thike|chalo|done|"
    r"confirm(ed)?|go ahead|proceed|let'?s do it|lets do( it)?|do it|please do|send it|send me|sounds good|go for it|"
    r"karo|kar do|kardo|kar dijiye|kijiye|book it|book karo|i want to join|want to join|join karna|judna|interested|"
    r"i'?m in|count me in|publish|schedule it|agreed|fine)\b", re.I)
LATER = re.compile(r"\b(later|busy|not now|abhi nahi|baad mein|baad me|in a meeting|driving|call me|call back|"
                   r"tomorrow|kal|next week|next month|after some time|thodi der)\b", re.I)
OFF_TOPIC = re.compile(
    r"\b(gst|income tax|tax filing|itr|file my|loan|insurance|electricity|visa|passport|recipe|politic|election|"
    r"stock market|share price|crypto|bitcoin|job|salary|rent agreement|court|lawyer|marriage proposal|movie)\b", re.I)
QUESTION = re.compile(r"\?|\b(what|how|when|which|why|who|where|kitna|kitne|kya|kab|kaise|kaun|kyun|kahan|price|cost|charges?|fees?)\b", re.I)
HINDI_MARKERS = re.compile(r"\b(hai|hain|kya|nahi|nahin|karo|kar|haan|aap|aapka|mujhe|chahiye|main|hum|bhi|theek|thik|"
                           r"acha|accha|ji|kaise|kab|kitna|karna|raha|rahi|wala|bhai|yaar|bolo|batao|dijiye|kijiye|"
                           r"aapki|aapke|yeh|woh|hoon|liye|sabhi|baat|baatein|shukriya|dhanyavad|namaste|ke|ki|ko|se|mein)\b", re.I)
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
OBJECTION = re.compile(r"\b(too expensive|expensive|costly|too much|mehenga|mehnga|mahanga|budget|can'?t afford|afford|"
                       r"price (is )?(too )?high|paisa nahi|zyada (hai|price)|not worth)\b", re.I)
HOW_Q = re.compile(r"\b(how does (it|this) work|how will (it|this|you)|how would|how it works|kaise kaam|kaise karega|kaise hoga|"
                   r"kya karoge|kya karogi|what will you do|what do you do|what happens next|process kya)\b", re.I)
SLOT_PICK = re.compile(r"^\s*(1|2|one|two|first|second|pehla|dusra)\b", re.I)
STOPWORDS = {"what", "which", "when", "where", "why", "how", "the", "this", "that", "your", "you", "does", "will", "have",
             "about", "from", "with", "there", "they", "their", "can", "could", "would", "should", "much", "many", "is",
             "are", "and", "for", "tell", "more", "please", "kya", "hai", "aap", "mujhe", "batao"}


def detect_lang(msg: str) -> str:
    if DEVANAGARI.search(msg or ""):
        return "hi"
    hits = len(HINDI_MARKERS.findall(msg or ""))
    words = max(1, len((msg or "").split()))
    return "mix" if hits >= 2 or (hits >= 1 and words <= 4 and not re.search(r"\b(do it|go|ok)\b", msg or "", re.I)) else "en"


def classify(conv: Conversation, msg: str, store: Store) -> str:
    m = (msg or "").strip()
    low = m.lower()
    if not m:
        return "empty"
    n = norm_text(m)
    if any(p in low for p in AUTO_PHRASES) or (n in conv.inbound_norm and len(n) > 25):
        return "auto_reply"
    if conv.merchant_id and store.merchant_last_autoreply.get(conv.merchant_id) == n and len(n) > 25:
        return "auto_reply"
    if OPT_OUT.search(m):
        return "opt_out"
    if OBJECTION.search(m):
        return "objection"
    if conv.audience == "customer" and SLOT_PICK.match(m):
        return "slot_pick"
    if LATER.search(m) and not re.search(r"\b(yes|go ahead|confirm|haan)\b", low):
        return "later"
    if COMMIT.search(m) and not (NEGATION.search(m) and not re.search(r"no problem|no issue|why not", low)) \
            and not re.search(r"\b(why|kyun|how much|kitna)\b", low):
        return "commitment"
    if OFF_TOPIC.search(m):
        return "off_topic"
    if QUESTION.search(m):
        return "question"
    if re.fullmatch(r"(no|nope|nahi|nahin|no thanks|no thank you|not really)[.! ]*", low):
        return "decline"
    return "other"


# ---------------------------------------------------------------------------- helpers

def T(lang: str, en: str, mix: str) -> str:
    return mix if lang in ("mix", "hi") else en


def progressive_hi(action_hi: str) -> str:
    """'X kar doon' -> 'X kar rahi hoon' (Vera's voice)."""
    s = action_hi.strip().rstrip("?")
    s = re.sub(r"\b(\w+) doon\b", r"\1 rahi hoon", s)
    return s


def _merchant_ledger(store: Store, conv: Conversation) -> tuple[Ledger | None, dict | None, dict | None]:
    merchant = store.get("merchant", conv.merchant_id)
    category = store.category_for(merchant) if merchant else None
    customer = store.get("customer", conv.customer_id) if conv.customer_id else None
    if not (merchant and category):
        return None, merchant, category
    trigger = store.get("trigger", conv.trigger_id) if conv.trigger_id else None
    L = build_ledger(category, merchant, trigger or {}, customer, None, None)
    return L, merchant, category


def _allowed_ledger(conv: Conversation, L: Ledger | None) -> Ledger:
    """Union of the original send's ledger (kept on the conversation) and the current one."""
    base = conv.card.get("_ledger")
    out = Ledger()
    for src in (base, L):
        if src:
            out.facts.update(src.facts)
    return out


def _ensure_action(store: Store, conv: Conversation) -> None:
    """Conversations we did not start (or started without a card) get a grounded default action."""
    if conv.action_on_yes:
        return
    L, merchant, category = _merchant_ledger(store, conv)
    if L and merchant and category:
        a = Assessment("", "generic", "generic", "merchant")
        ctx = Ctx(category, merchant, {}, None, L, a, None, stable_hash([conv.conversation_id]))
        lever = best_lever(ctx)
        conv.card.setdefault("_ledger", L)
        if lever:
            _, en, hi, fids = lever
            conv.action_on_yes, conv.action_hi = en, hi
            conv.explain = "; ".join(L.get(f).text for f in fids if L.get(f) and L.get(f).text)
    if not conv.action_on_yes:
        conv.action_on_yes, conv.action_hi = "set up the next step on your Google profile", "Google profile ka agla step set kar doon"
    if not conv.final_en:
        conv.final_en, conv.final_hi = _finals(conv.action_on_yes)


def _sal(store: Store, conv: Conversation) -> str:
    if conv.audience == "customer":
        c = store.get("customer", conv.customer_id) or {}
        name = str(g(c, "identity", "name", default="") or "")
        return re.split(r"\s*\(", name)[0].strip() if name and not name.startswith("(") else ""
    L, _, _ = _merchant_ledger(store, conv)
    return L.text("m.salutation") if L else ""


def _topical_fact(conv: Conversation, msg: str) -> str | None:
    """Evidence line that shares a content word with the question; None if nothing is on-topic."""
    words = {w for w in re.findall(r"[a-z]{3,}", msg.lower()) if w not in STOPWORDS}
    best, best_hits = None, 0
    for e in conv.card.get("evidence", []) or []:
        ew = set(re.findall(r"[a-z]{3,}", e.lower()))
        hits = len({w.rstrip("s") for w in words} & {w.rstrip("s") for w in ew})
        if hits > best_hits:
            best, best_hits = e, hits
    return best


# ---------------------------------------------------------------------------- main handler

def handle_reply(store: Store, req: dict, now: datetime | None) -> dict:
    cid = str(req.get("conversation_id") or "").strip() or f"conv_unknown_{stable_hash([req], 8)}"
    msg = str(req.get("message") or "")
    conv = store.conversations.get(cid)
    if conv is None:
        audience = "customer" if (req.get("from_role") == "customer" or req.get("customer_id")) else "merchant"
        conv = Conversation(cid, req.get("merchant_id"), req.get("customer_id"), audience, topic="")
        store.conversations[cid] = conv
    if not conv.merchant_id and req.get("merchant_id"):
        conv.merchant_id = req.get("merchant_id")
    lang = detect_lang(msg)
    if lang != "en" or not conv.turns or len(msg.split()) > 3:
        conv.lang = lang
    conv.turns.append({"from": req.get("from_role") or "merchant", "body": msg, "at": req.get("received_at")})

    cls = classify(conv, msg, store)
    mid = conv.merchant_id or ""
    last_auto = store.merchant_autoreply_at.get(mid)
    if cls not in ("auto_reply", "empty") and conv.audience == "merchant":
        store.merchant_autoreply.pop(mid, None)            # a human answered: forget the auto-reply streak
    elif cls == "auto_reply" and last_auto and now and last_auto.tzinfo and now.tzinfo \
            and (now - last_auto).total_seconds() > 6 * 3600:
        store.merchant_autoreply.pop(mid, None)            # stale streak from hours ago doesn't carry over
    if cls == "auto_reply" and now:
        store.merchant_autoreply_at[mid] = now
    out = _route(store, conv, cls, msg, now)
    conv.inbound_norm.append(norm_text(msg))
    if out.get("action") == "send":
        store.remember_send(conv, out["body"])
        conv.turns.append({"from": "bot", "body": out["body"]})
    out["rationale"] = f"[{cls}] " + out.get("rationale", "")
    return out


def _route(store: Store, conv: Conversation, cls: str, msg: str, now: datetime | None) -> dict:
    lang = conv.lang
    sal = _sal(store, conv)
    hs = f"{sal}, " if sal else ""
    who = "Customer" if conv.audience == "customer" else "Merchant"

    # ---- closed conversations stay closed unless the person clearly re-engages
    if conv.status in ("ended", "done"):
        if cls == "commitment" and conv.status == "done":
            return _end("Task already confirmed and in progress; nothing further to add.")
        if cls == "commitment" and conv.merchant_id not in store.blocked_merchants and conv.customer_id not in store.blocked_customers:
            conv.status = "open"
        elif cls in ("question", "off_topic", "other") and not conv.card.get("_post_end_note"):
            conv.card["_post_end_note"] = True
            body = T(lang, f"{hs}understood, I won't send anything further. If you ever need help with your Google profile, just message 'Hi Vera'.",
                     f"{hs}samajh gayi, main aage kuch nahi bhejungi. Google profile mein kabhi madad chahiye ho toh bas 'Hi Vera' likh dijiye.")
            if conv.audience == "customer":
                body = T(lang, "Understood, we won't message you again about this. Take care!",
                         "Samajh gaye, is baare mein dobara message nahi karenge. Dhyan rakhiye!")
            return _send(conv, body, "none", "Conversation was closed; one courteous line, no new pitch.", store)
        else:
            return _end("Conversation already closed; not re-engaging.")

    if conv.bot_sends >= 5:
        conv.status = "ended"
        return _end("Five bot turns reached in this conversation; exiting gracefully instead of pushing further.")

    if cls == "empty":
        return _wait(conv, 3600, "Empty message; waiting for a real reply.")

    if cls == "auto_reply":
        conv.auto_replies += 1
        mid = conv.merchant_id or ""
        store.merchant_autoreply[mid] = store.merchant_autoreply.get(mid, 0) + 1
        store.merchant_last_autoreply[mid] = norm_text(msg)
        n = max(conv.auto_replies, store.merchant_autoreply[mid])
        if n == 1:
            _ensure_action(store, conv)
            body = T(lang, f"Seems like an automated reply. Whenever the owner sees this: just reply YES and I'll {conv.action_on_yes}.",
                     f"Lagta hai yeh auto-reply hai. Owner jab dekhein, bas YES reply kar dein: {conv.action_hi or conv.action_on_yes}.")
            return _send(conv, body, "binary_yes_no", "Detected WhatsApp Business auto-reply; one short owner-directed nudge.", store)
        if n == 2:
            conv.status = "waiting"
            return _wait(conv, 86400, "Same auto-reply again: owner is not at the phone. Backing off 24h instead of burning turns.")
        conv.status = "ended"
        return _end(f"Auto-reply {n}x with no human response; closing the conversation.")

    if cls == "opt_out":
        conv.status = "ended"
        if conv.audience == "customer" and conv.customer_id:
            store.blocked_customers.add(conv.customer_id)
        elif conv.merchant_id:
            store.blocked_merchants.add(conv.merchant_id)
        return _end("Opt-out / frustration signal; closing now and suppressing all further outreach to this recipient.")

    if cls == "slot_pick":
        return _slot_pick(store, conv, msg, lang)

    if cls == "objection":
        return _objection(store, conv, lang, hs)

    if cls == "commitment":
        return _commit(store, conv, lang, hs)

    if cls == "later":
        secs = 57600 if re.search(r"tomorrow|kal", msg, re.I) else 259200 if re.search(r"next (week|month)", msg, re.I) else 14400
        conv.status = "waiting"
        return _wait(conv, secs, f"{who} asked for time; backing off rather than pushing.")

    _ensure_action(store, conv)
    if cls == "off_topic":
        topic = "GST" if re.search(r"gst|tax|itr", msg, re.I) else "That"
        decline = T(lang, f"{hs}{topic} is best handled by your CA; it's outside what I can help with here.",
                    f"{hs}{topic} ke liye aapke CA sahi rahenge, yeh mere scope se bahar hai.")
        if conv.stage == "action":
            back = T(lang, f"On our thread: reply CONFIRM and I'll {conv.final_en}.",
                     f"Apni baat par: CONFIRM reply kijiye, main {conv.final_hi}.")
        else:
            back = T(lang, f"On our thread: reply YES and I'll {conv.action_on_yes}.",
                     f"Apni baat par: YES reply kijiye, {progressive_hi(conv.action_hi) if conv.action_hi else conv.action_on_yes} shuru kar dungi.")
        return _send(conv, f"{decline} {back}", "binary_yes_no", "Off-topic ask declined in one line; steered back to the original thread.", store)

    if cls == "question":
        return _answer(store, conv, msg, lang, hs)

    if cls == "decline":
        conv.status = "ended"
        return _end(f"Soft 'no' from the {who.lower()}; ending politely without another pitch.")

    # other / engaged statement: smallest useful next step
    if conv.stage == "action":
        body = T(lang, f"{hs}noted. Whenever you're ready, reply CONFIRM and I'll {conv.final_en}.",
                 f"{hs}theek hai. Jab ready hon, CONFIRM reply kijiye, main {conv.final_hi}.")
    else:
        body = T(lang, f"{hs}noted. The next step is small: reply YES and I'll {conv.action_on_yes}.",
                 f"{hs}theek hai. Agla step chhota hai: YES reply kijiye, {conv.action_hi.rstrip('?') if conv.action_hi else conv.action_on_yes}.")
    return _send(conv, body, "binary_yes_no", "Engaged but non-committal reply; offering the single next step.", store)


def _commit(store: Store, conv: Conversation, lang: str, hs: str) -> dict:
    _ensure_action(store, conv)
    if conv.audience == "customer":
        conv.stage, conv.status = "confirmed", "done"
        body = T(lang, "Great, you're confirmed. We'll send a reminder before your visit. See you soon!",
                 "Badhiya, aapka confirm hai. Visit se pehle reminder bhej denge. Milte hain!")
        return _send(conv, body, "none", "Customer said yes; confirmed immediately with no extra questions.", store)
    if conv.stage == "action":
        conv.stage, conv.status = "confirmed", "done"
        body = T(lang, f"Confirmed. I'll {conv.final_en} and report back here once it's done.",
                 f"Confirm ho gaya. Main {conv.final_hi} aur ho jaane par yahin update dungi.")
        return _send(conv, body, "none", "Merchant confirmed; proceeding and promising a report-back, no further asks.", store)
    conv.stage = "action"
    if conv.yes_artifact:
        body = T(lang, f"{hs}done, here's the draft:\n{conv.yes_artifact}\nReply CONFIRM and I'll {conv.final_en}, or send edits.",
                 f"{hs}ho gaya, yeh raha draft:\n{conv.yes_artifact}\nCONFIRM reply kijiye, main {conv.final_hi}, ya edits bhej dijiye.")
    else:
        body = T(lang, f"{hs}on it: I'll {_first_person(conv.action_on_yes)} and share it here before anything goes live. Reply CONFIRM to approve.",
                 f"{hs}shuru kar diya. Main {progressive_hi(conv.action_hi) if conv.action_hi else conv.action_on_yes}, draft yahin bhejungi. Final ke liye CONFIRM reply kijiye.")
    return _send(conv, body, "binary_confirm_cancel", "Explicit commitment: switched from pitch to action immediately; no qualifying questions.",
                 store, forbid_q=True)


def _objection(store: Store, conv: Conversation, lang: str, hs: str) -> dict:
    """One respectful, value-grounded reply using only real account numbers; a second objection is accepted."""
    if conv.card.get("_objections"):
        conv.status = "ended"
        return _end("Second objection: accepting the no gracefully instead of pushing.")
    conv.card["_objections"] = 1
    _ensure_action(store, conv)
    L, _, _ = _merchant_ledger(store, conv)
    calls, views, win = (L.text("m.perf.calls"), L.text("m.perf.views"), L.text("m.perf.window")) if L else ("", "", "")
    value = ""
    if calls and views and conv.audience == "merchant":
        value = T(lang, f"For context, your profile brought {calls} and {views} in the last {win or '30 days'}.",
                  f"Context ke liye: pichhle {win or '30 days'} mein aapke profile se {calls} aur {views} aaye.")
    ans = T(lang, f"{hs}fair point, and no pressure. {value} If it still doesn't feel worth it, just say no and I'll leave it there.",
            f"{hs}bilkul samajh sakti hoon, koi pressure nahi. {value} Agar phir bhi theek na lage, bas 'no' likh dijiye, main yahin rok dungi.")
    nxt = T(lang, f"Reply YES if you'd like me to {conv.action_on_yes}.", "Aage badhna ho toh YES reply kijiye.")
    return _send(conv, re.sub(r"\s+", " ", f"{ans} {nxt}").strip(), "binary_yes_no",
                 "Price objection: one respectful reply grounded in the merchant's own numbers; a no will be accepted.", store)


def _first_person(action: str) -> str:
    """'put X live on your profile' -> 'put X live on your profile' (already imperative -> fine as 'I ...')."""
    return action[0].lower() + action[1:] if action else "set it up"


def _slot_pick(store: Store, conv: Conversation, msg: str, lang: str) -> dict:
    slots = [s for s in conv.card.get("slots", []) if s]
    idx = 1 if re.match(r"^\s*(2|two|second|dusra)", msg, re.I) else 0
    conv.stage, conv.status = "confirmed", "done"
    if idx < len(slots):
        body = T(lang, f"Done, you're booked for {slots[idx]}. We'll send a reminder the day before.",
                 f"Ho gaya, aapka slot {slots[idx]} ke liye book hai. Ek din pehle reminder bhej denge.")
    else:
        body = T(lang, "Noted your choice. We'll confirm the exact time shortly.",
                 "Aapki choice note kar li. Exact time jaldi confirm karenge.")
    return _send(conv, body, "none", "Customer picked a slot; booked it without further questions.", store)


def _answer(store: Store, conv: Conversation, msg: str, lang: str, hs: str) -> dict:
    low = msg.lower()
    ev = [e for e in conv.card.get("evidence", []) if e]
    nxt_yes = T(lang, f"If useful, reply YES and I'll {conv.action_on_yes}.",
                f"Kaam ka lage toh YES reply kijiye: {conv.action_hi.rstrip('?') if conv.action_hi else conv.action_on_yes}.")
    nxt = T(lang, f"Reply CONFIRM when you're happy and I'll {conv.final_en}.", f"Theek lage toh CONFIRM reply kijiye, main {conv.final_hi}.") \
        if conv.stage == "action" else nxt_yes
    if re.search(r"\b(why|kyun|kyon)\b", low):
        ans = conv.explain or (f"it's based on {ev[0]}." if ev else "")
        if ans:
            first = ans.split(" ", 1)[0].lower()
            if hs and first in ("a", "an", "the", "it", "it's", "your", "this", "that", "these", "because", "since"):
                ans = ans[0].lower() + ans[1:]          # only common words; keep proper nouns like 'Saturday'
            ans = T(lang, f"{hs}{ans}", f"{hs}{ans}")
        else:
            ans = T(lang, f"{hs}fair question; I only suggest what your own account data supports.",
                    f"{hs}sahi sawaal; main sirf wahi suggest karti hoon jo aapke account data se support hota hai.")
    elif re.search(r"\b(source|study|proof|where.*from|kahan se|trial|paper|abstract)\b", low) and ev:
        src = next((e for e in ev if re.search(r"\d{4}|journal|circular|alert|source", e, re.I)), ev[0])
        ans = T(lang, f"{hs}it's from {src}.", f"{hs}yeh {src} se hai.")
    elif HOW_Q.search(msg):
        act = conv.action_on_yes
        ans = T(lang, f"{hs}simple: I {_first_person(act)}, share it here first, and nothing goes live without your final OK. You can edit anything before that.",
                f"{hs}simple hai: main {progressive_hi(conv.action_hi) if conv.action_hi else act}, pehle yahin share karungi, "
                f"aur aapke final OK ke bina kuch live nahi hoga. Usse pehle aap kuch bhi badal sakte hain.")
        nxt = T(lang, "Reply YES to start.", "Shuru karne ke liye YES reply kijiye.")
        return _send(conv, f"{ans} {nxt}", "binary_yes_no", "Explained the proposed action in plain terms, then one small next step.", store)
    elif re.search(r"\b(look like|details|example|sample|draft|kaisa|dikhao|show me)\b", low) and conv.yes_artifact:
        conv.stage = "action"
        body = T(lang, f"{hs}here's the draft:\n{conv.yes_artifact}\nReply CONFIRM and I'll {conv.final_en}, or send edits.",
                 f"{hs}yeh raha draft:\n{conv.yes_artifact}\nCONFIRM reply kijiye, main {conv.final_hi}, ya edits bhej dijiye.")
        return _send(conv, body, "binary_confirm_cancel", "Question asked for specifics; delivered the drafted artifact.", store)
    else:
        fact = _topical_fact(conv, msg)
        price_q = re.search(r"\b(price|cost|charge|fees?|kitna|kitne|paisa|rate)\b", low)
        if fact and (not price_q or "₹" in fact):
            ans = T(lang, f"{hs}here's what I have: {fact}.", f"{hs}mere paas yeh jaankari hai: {fact}.")
        elif price_q:
            ans = T(lang, f"{hs}I don't have a confirmed price for that in your account data, so I won't guess; nothing is charged without your OK.",
                    f"{hs}iska confirmed price mere data mein nahi hai, isliye guess nahi karungi; aapke OK ke bina kuch charge nahi hoga.")
        else:
            ans = T(lang, f"{hs}I don't have that detail in your account data, so I won't guess. Share it and I'll build it in.",
                    f"{hs}yeh detail mere data mein nahi hai, isliye guess nahi karungi. Aap bata dijiye, main add kar dungi.")
    return _send(conv, f"{ans} {nxt}", "binary_yes_no", "Answered from verified context only (or said so honestly), then offered the next step.", store)


# ---------------------------------------------------------------------------- response builders

def _send(conv: Conversation, body: str, cta: str, why: str, store: Store, forbid_q: bool = False) -> dict:
    L, _, category = _merchant_ledger(store, conv)
    allowed = _allowed_ledger(conv, L)
    problems = hard(validate(body, L=allowed, audience=conv.audience,
                             send_as="merchant_on_behalf" if conv.audience == "customer" else "vera",
                             cta_type=cta, taboo=list(g(category, "voice", "vocab_taboo", default=[]) or []),
                             expected_names=[], prior_bodies=conv.sent_bodies, forbid_qualifying=forbid_q))
    if problems:
        if all(p.code == "duplicate_body" for p in problems):
            return _wait(conv, 3600, why + " (avoiding a verbatim repeat; waiting instead)")
        safe = T(conv.lang, "Understood. I'll take it from here and update you shortly.",
                 "Samajh gayi. Main aage handle karti hoon aur jaldi update deti hoon.")
        if norm_text(safe) in conv.sent_bodies:
            return _wait(conv, 3600, why + " (fallback body already used; waiting)")
        return {"action": "send", "body": safe, "cta": "none",
                "rationale": why + f" (validator replaced draft: {', '.join(p.code for p in problems)})"}
    return {"action": "send", "body": body, "cta": cta, "rationale": why}


def _wait(conv: Conversation, secs: int, why: str) -> dict:
    return {"action": "wait", "wait_seconds": int(secs), "rationale": why}


def _end(why: str) -> dict:
    return {"action": "end", "rationale": why}
