"""ANGLE + deterministic WRITE: one playbook per trigger family.

Every playbook builds its message only from ledger facts (ctx.t / ctx.use / ctx.derive).
Anything a playbook computes (a gap, a proposed price) is registered as a DERIVED fact first,
so the validator can prove every number in the body. Style variants never change facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

from .decide import Assessment
from .evidence import (
    AUDIENCE_WORD, CATEGORY, Ledger, beats_for_month, display_name, numeric_tokens, owner_first,
)
from .util import (
    as_dict, as_list, fmt_ctr, fmt_day_month, fmt_dow_day_month, fmt_int, fmt_pct, fmt_rupee, fmt_time,
    g, humanize, join_human, num, parse_date, parse_dt, pick,
)

# ---------------------------------------------------------------------------- data classes


@dataclass
class Draft:
    lead: list[str]                 # sentences before the CTA
    cta: str                        # final sentence(s): the one action
    cta_type: str                   # binary_yes_no | binary_confirm_cancel | multi_choice_slot | open_ended | none
    angle: str                      # one-line description of the chosen angle (feeds rationale)
    used: list[str]                 # fact ids the body relies on
    action_on_yes: str = ""         # what "YES" commits us to (reply engine follows through)
    yes_artifact: str = ""          # a ready deliverable to send on commitment, if we have one
    template_params: list[str] = field(default_factory=list)
    lever: str = ""                 # compulsion lever used
    action_hi: str = ""             # Hinglish form of action_on_yes ("... kar doon")
    final_en: str = ""              # what CONFIRM triggers, e.g. "send it to the affected customers"
    final_hi: str = ""
    explain: str = ""               # grounded answer to "why?" about this recommendation
    optional: list[str] = field(default_factory=list)   # lead sentences that may be dropped to stay short

    MAX_LEN = 350

    @property
    def body(self) -> str:
        lead = [x for x in self.lead if x and x.strip()]
        drop = [o for o in self.optional if o in lead]
        while drop and len(" ".join(lead + [self.cta])) > self.MAX_LEN:
            lead.remove(drop.pop())            # drop the least important optional sentence first
        parts = [s.strip() for s in lead + [self.cta] if s and s.strip()]
        blocks = ("•", '"', "Customer note draft", "Draft for your", "Summary (", "Audit checklist")
        if not any(p.startswith(blocks) for p in parts):
            return " ".join(parts)
        out = parts[0]
        for i, p in enumerate(parts[1:], 1):
            newline = (p.startswith(blocks) or parts[i - 1].startswith(blocks)
                       or i == len(parts) - 1 or out.endswith(":"))
            out += ("\n" if newline else " ") + p
        return out


class Ctx:
    """Everything a playbook may look at, plus fact-tracking helpers."""

    def __init__(self, category: dict, merchant: dict, trigger: dict, customer: dict | None,
                 ledger: Ledger, assessment: Assessment, ref_date: date | None, variant_seed: str) -> None:
        self.category, self.merchant, self.trigger, self.customer = category, merchant, trigger, customer
        self.L, self.a = ledger, assessment
        self.slug = str(category.get("slug") or merchant.get("category_slug") or "")
        self.payload = as_dict(trigger.get("payload"))
        self.item = assessment.digest_item
        self.ref_date = ref_date
        self.seed = variant_seed
        self.used: list[str] = []
        self.sal = ledger.text("m.salutation") or ""
        self.biz = display_name(merchant)
        self.owner = owner_first(merchant)
        langs = [str(x).lower() for x in as_list(g(merchant, "identity", "languages"))]
        code_mix = str(g(category, "voice", "code_mix", default=""))
        self.hinglish = "hi" in langs and code_mix == "hindi_english_natural"
        self.aud = AUDIENCE_WORD.get(self.slug, "customers")
        self.last_hi_action = ""
        self.hist = history_state(merchant)

    # --- fact helpers
    def t(self, fid: str, default: str = "") -> str:
        f = self.L.get(fid)
        if f and f.text:
            if fid not in self.used:
                self.used.append(fid)
            return f.text
        return default

    def v(self, fid: str, default: Any = None) -> Any:
        return self.L.val(fid, default)

    def use(self, fid: str) -> None:
        if self.L.has(fid) and fid not in self.used:
            self.used.append(fid)

    def derive(self, fid: str, value: Any, text: str, sources: tuple = (), alt: tuple = ()) -> str:
        self.L.derive(fid, value, text, sources, alt)
        if fid not in self.used:
            self.used.append(fid)
        for s in sources:
            self.use(s)
        return text

    def pick(self, *options: str) -> str:
        return pick(list(options), self.seed, len(options), options[0][:12])

    def hl(self, en: str, hi: str) -> str:
        return hi if self.hinglish else en

    # --- merchant helpers
    def active_offers(self) -> list[tuple[str, str]]:
        return [(f.id, f.text) for f in self.L.prefix("m.offer.active.")]

    def catalog(self, types: tuple = ("service_at_price",)) -> list[tuple[str, str]]:
        out = []
        for f in self.L.prefix("c.catalog."):
            if as_dict(f.value).get("type") in types:
                out.append((f.id, f.text))
        return out

    def review(self, sentiment: str) -> list:
        out = []
        for f in self.L.prefix("m.review."):
            r = as_dict(f.value)
            if r.get("sentiment") == sentiment:
                out.append(f)
        return sorted(out, key=lambda f: -(num(as_dict(f.value).get("occurrences_30d")) or 0))

    def signal(self, name: str) -> bool:
        return self.L.has(f"m.signal.{name}") or self.L.has(f"m.signal_raw.{name}")

    def month(self) -> int | None:
        return self.ref_date.month if self.ref_date else None


# ---------------------------------------------------------------------------- shared pieces

def yes_cta(ctx: Ctx, en_action: str, hi_action: str) -> str:
    """The single CTA sentence. Always the last sentence of the body."""
    ctx.last_hi_action = hi_action
    if ctx.hinglish:
        return ctx.pick(f"{hi_action}? Bas YES reply kijiye.", f"{hi_action}? Reply YES.")
    return ctx.pick(f"Want me to {en_action}? Reply YES.", f"Shall I {en_action}? Just reply YES.")


_ABBR = re.compile(r"(?:\b(?:Dr|Mr|Mrs|Ms|No|St|vs|p|approx|Rs)|\b[A-Z])\.$")


def split_sentences(text: str) -> list[str]:
    out, buf = [], ""
    for piece in re.split(r"(?<=[.!?])\s+", str(text or "").strip()):
        buf = f"{buf} {piece}".strip() if buf else piece
        if not _ABBR.search(buf):
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return [s for s in out if s]


_MON = {m.lower(): i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def clean_source(src: Any, ref: date | None) -> str:
    """Drop a publication date from a source citation when it is later than 'now'
    ('Dental Council of India circular 2026-11-04' -> 'Dental Council of India circular').
    Citing a document as already issued on a future date reads as an error; the publisher and page stay."""
    s = str(src or "").strip()
    if not ref or not s:
        return s

    def future_iso(m: re.Match) -> str:
        try:
            return "" if date(int(m.group(1)), int(m.group(2)), int(m.group(3))) > ref else m.group(0)
        except ValueError:
            return m.group(0)

    def future_month(m: re.Match) -> str:
        mon, yr = _MON.get(m.group(1)[:3].lower()), int(m.group(2))
        return "" if mon and date(yr, mon, 1) > ref else m.group(0)

    s = re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b", future_iso, s)
    s = re.sub(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{4})\b", future_month, s)
    s = re.sub(r"\s+,", ",", re.sub(r"\s{2,}", " ", s)).strip(" ,;")
    return s


def first_sentence(text: str, must_have_digit: bool = False, max_len: int = 220) -> str:
    sents = split_sentences(text)
    for s in sents:
        s = s.strip()
        if not s:
            continue
        if must_have_digit and not re.search(r"\d", s):
            continue
        if len(s) <= max_len:
            return s if s.endswith((".", "!", "?")) else s + "."
    return ""


def sentence_with(text: str, words: tuple[str, ...], max_len: int = 220) -> str:
    for s in split_sentences(text):
        if any(w in s.lower() for w in words) and len(s) <= max_len:
            return s if s.endswith((".", "!", "?")) else s + "."
    return ""


def lcfirst(s: str) -> str:
    return s[:1].lower() + s[1:] if s and not s[:2].isupper() else s


MONTH_FULL = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
              "November", "December"]
CAT_NOUN = {"dentists": "dental clinics", "salons": "salons", "restaurants": "restaurants", "gyms": "gyms",
            "pharmacies": "pharmacies"}
IMPERATIVE = r"(focus|push|book|run|move|pause|add|consider|audit|skip|save|keep|start|stock|offer)\b"


def speak(note: Any) -> str:
    """Turn a terse category note into speech: '+' -> 'and', ';' -> ', and', 'x/y' -> 'x and y'."""
    t = lcfirst(str(note or "").strip().rstrip("."))
    t = re.sub(r"\s*\+\s*", " and ", t)
    t = re.sub(r"\s*;\s*", ", and ", t)
    t = re.sub(r"(?<=[a-z])/(?=[a-z])", " and ", t)
    t = re.sub(r"\bto counter visibility\b", "to the counter", t)
    t = re.sub(r"\bto back shelf\b", "to the back shelf", t)
    return t


def month_phrase(mr: str) -> str:
    """'Nov-Feb' -> 'November to February'; 'Jan' -> 'January'; 'Feb 14' -> 'Mid-February'."""
    found = re.findall(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", str(mr).lower())
    if not found:
        return ""
    names = [MONTH_FULL[_MON[f] - 1] for f in found]
    if re.search(r"\d", str(mr)) and len(names) == 1:
        return f"Mid-{names[0]}"
    return names[0] if len(names) == 1 else f"{names[0]} to {names[1]}"


def beat_sentence(ctx: "Ctx", fid: str) -> str:
    """Turn a raw seasonal note ('primary wedding/festival season — bridal package bookings 4x baseline')
    into a spoken sentence. Only words and numbers from the note are used."""
    f = ctx.L.get(fid)
    if not f:
        return ""
    ctx.use(fid)
    b = as_dict(f.value)
    note = str(b.get("note", "")).strip()
    parts = re.split(r"\s+[—–-]\s+", note, maxsplit=1)
    head, tail = parts[0].strip().replace(" + ", " and "), (parts[1] if len(parts) > 1 else "")
    noun = CAT_NOUN.get(ctx.slug, ctx.slug or "businesses")
    mp = month_phrase(str(b.get("month_range", "")))
    lead_in = mp if mp else "This stretch"
    if re.search(r"(season|window|surge|spike|peak|lull|slowdown|rush|focus)$", head, re.I):
        out = f"{lead_in} is usually the {head} for {noun}"
    else:
        out = f"{lead_in} is usually when {noun} see {head}"
    tail = tail.split(";")[0].strip()
    if tail:
        tail = re.sub(r"(\d+(?:\.\d+)?)x baseline", r"\1x the usual level", tail)
        tail = tail.replace("baseline", "usual").replace(" + ", " and ")
        if re.match(IMPERATIVE, tail, re.I):
            out += f", so the smart move is to {lcfirst(tail)}"
        elif re.search(r"\d", tail):
            out += f" ({tail})"
        elif re.search(r"\b(return|returns|rise|rises|peak|peaks|drop|drops|surge|surges|spike|spikes)\b", tail):
            out += f", when {tail}"
        else:
            out += f", led by {tail}"
    return out + "."


def beat_for(ctx: "Ctx", month: int | None) -> str | None:
    if not month:
        return None
    for i, b in enumerate(as_list(ctx.category.get("seasonal_beats"))):
        if month in _months(str(as_dict(b).get("month_range", ""))):
            return f"c.beat.{i}"
    return None


def days_consistent(target: date | None, days: float | None, ref: date | None, past: bool = False) -> bool:
    """A payload day count is only stated if it agrees (±2 days) with the tick's 'now'."""
    if days is None or target is None or ref is None:
        return False
    actual = (ref - target).days if past else (target - ref).days
    return abs(actual - days) <= 2


COMMIT_WORDS = re.compile(r"\b(yes|haan|ok|okay|sure|go ahead|please|send|do it|karo|kar do)\b", re.I)


def history_state(merchant: dict) -> dict:
    """What the merchant already said to Vera: last Vera message, and whether they accepted it."""
    hist = [h for h in as_list(g(merchant, "conversation_history")) if isinstance(h, dict)]
    st = {"last_vera": "", "accepted": False, "accepted_vera": "", "merchant_reply": "", "reply_ts": ""}
    for i, h in enumerate(hist):
        if h.get("from") == "vera":
            st["last_vera"] = str(h.get("body") or "")
            nxt = hist[i + 1] if i + 1 < len(hist) else None
            if nxt and nxt.get("from") == "merchant":
                body = str(nxt.get("body") or "")
                acc = str(nxt.get("engagement") or "") == "intent_action" or bool(COMMIT_WORDS.search(body))
                st.update(accepted=acc, accepted_vera=st["last_vera"] if acc else "", merchant_reply=body if acc else "",
                          reply_ts=str(nxt.get("ts") or ""))
            else:
                st.update(accepted=False, accepted_vera="", merchant_reply="")
    return st


_STOP = {"want", "your", "with", "that", "this", "from", "have", "them", "will", "draft", "please", "send", "yes",
         "list", "posts", "post", "google", "customer", "customers", "message"}


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", str(text).lower()) if w not in _STOP}


def pending_focus(ctx: "Ctx") -> str:
    """'Yes please, focus on whitening and aligners' -> 'whitening and aligners'."""
    m = re.search(r"focus on ([a-z ,&+]+)", ctx.hist.get("merchant_reply", ""), re.I)
    return m.group(1).strip(" ,.") if m else ""


def best_lever(ctx: Ctx) -> tuple[str, str, str, list[str]] | None:
    """The one concrete, evidence-backed improvement for this merchant.
    Returns (observation_sentence, en_action, hi_action, fact_ids) or None."""
    offers = ctx.active_offers()
    if not offers:
        cat = ctx.catalog(("service_at_price",)) or ctx.catalog(("free_service", "free_trial"))
        if cat:
            fid, title = cat[0]
            if not ctx.L.has("m.no_active_offer"):
                ctx.L.add("m.no_active_offer", "MERCHANT", "offers", [], "no active offer on the profile")
            obs = ctx.pick("There's no live offer on your profile right now, and a service-plus-price offer usually pulls better than a % discount.",
                           "Your profile has no active offer at the moment; a clear service-plus-price offer tends to convert better than a % discount.",
                           "Nothing is live in your offers section right now, which is the easiest gap to close.")
            return (obs, f"put '{title}' live on your profile", f"'{title}' aapke profile par live kar doon",
                    ["m.no_active_offer", fid])
    if ctx.v("m.unverified") is False or ctx.signal("unverified_gbp"):
        return ("Your Google profile is still unverified, which caps how often it shows up in local search.",
                "start the Google verification for you", "Google verification abhi start kar doon", ["m.unverified"])
    stale = ctx.L.get("m.signal.stale_posts")
    if stale:
        return (f"Your {stale.text}.", "draft a fresh Google post for you to approve",
                "ek fresh Google post draft kar doon", ["m.signal.stale_posts"])
    ctrp = ctx.L.get("d.ctr_vs_peer")
    if ctrp and as_dict(ctrp.value).get("rel") == "below":
        return (f"Your profile {ctrp.text} means people see you but don't tap through.",
                "rework your profile photos and description to lift that", "profile photos aur description improve kar doon",
                ["d.ctr_vs_peer"])
    if offers:
        priced = sorted(offers, key=lambda o: -(_price(o[1]) or 0))
        fid, title = priced[0]
        return (f"Your '{title}' is live but not being pushed; a fresh post puts it back in front of searchers.",
                f"put '{title}' in a fresh Google post this week", f"'{title}' ko ek fresh Google post mein daal doon", [fid])
    return None


def offer_line(ctx: Ctx) -> tuple[str, str] | None:
    offers = ctx.active_offers()
    if offers:
        return offers[0]
    return None


def digest_by_kind(ctx: Ctx, kinds: tuple[str, ...]) -> dict | None:
    best, best_score = None, -1.0
    for f in ctx.L.prefix("c.digest."):
        d = as_dict(f.value)
        if d.get("kind") not in kinds:
            continue
        s = 1.0 + (1.0 if _segment_fact(ctx, d) else 0.0) - kinds.index(d.get("kind")) * 0.1
        if s > best_score:
            best, best_score = d, s
    return best


def _segment_fact(ctx: Ctx, item: dict) -> str | None:
    seg = str(item.get("patient_segment") or "").lower()
    if not seg:
        return None
    toks = [t for t in re.split(r"[_\s-]+", seg) if len(t) > 2]
    for f in ctx.L.prefix("m.agg."):
        key = f.id.split(".", 2)[-1]
        if toks and all(t.rstrip("s") in key for t in toks[:2]):
            return f.id
    return None


# ---------------------------------------------------------------------------- merchant playbooks

def pb_research(ctx: Ctx) -> Draft | None:
    item = ctx.item or digest_by_kind(ctx, ("research", "trend", "tech", "seasonal", "compete"))
    if not item:
        return None
    fid = "t.item" if ctx.item else f"c.digest.{item['id']}"
    ctx.use(fid)
    src, title = clean_source(item.get("source"), ctx.ref_date).strip(), str(item.get("title") or "").strip()
    kind = item.get("kind")
    lead = [ctx.pick(f"{ctx.sal}, new in {src}: {title}.", f"{ctx.sal}, one from this week's research roundup ({src}): {title}.")
            if src else f"{ctx.sal}, one from this week's research roundup: {title}."]
    finding = first_sentence(item.get("summary"), must_have_digit=True)
    if finding:
        tn = num(item.get("trial_n"))
        lead.append(finding[:-1] + (f" (n={fmt_int(tn)})." if tn and fmt_int(tn) not in finding else "."))
    seg = _segment_fact(ctx, item)
    if seg:
        lead.append(ctx.pick(f"Directly relevant to your {ctx.t(seg)}.", f"That's your {ctx.t(seg)} in particular."))
    else:
        tie = _offer_tie(ctx, item)
        if tie:
            lead.append(tie)
        elif ctx.L.has("m.locality"):
            noun = {"dentists": "practice", "gyms": "gym", "salons": "salon", "restaurants": "restaurant",
                    "pharmacies": "pharmacy"}.get(ctx.slug, "business")
            lead.append(f"Worth a look for your {ctx.t('m.locality')} {noun}.")
    short_open = f"{ctx.sal}, new in {src}: {title}." if src else lead[0]
    if len(" ".join(lead)) > 260 and lead[0] != short_open:
        lead[0] = short_open
    if kind == "research":
        who = {"dentists": "patient", "gyms": "member", "salons": "client"}.get(ctx.slug, "customer")
        cta = yes_cta(ctx, f"pull the summary and draft a {who}-friendly WhatsApp note on it",
                      f"Summary nikaal ke ek {who}-friendly WhatsApp note draft kar doon")
        yes = f"pull the {src} abstract and draft the patient WhatsApp note"
    else:
        act = str(item.get("actionable") or "").strip()
        opt = []
        if act:
            lead.append(f"Suggested move: {speak(act)}.")
            opt.append(lead[-1])
        cta = yes_cta(ctx, "draft a Google post around this for your profile",
                      "Iske around ek Google post draft kar doon")
        yes = f"draft the Google post on '{title}'"
    return Draft(lead, cta, "binary_yes_no", f"digest item '{title}' ({src}) tied to merchant data" if seg else
                 f"digest item '{title}' ({src})", ctx.used, action_on_yes=yes,
                 yes_artifact=_patient_note(ctx, item), template_params=[ctx.sal, title, src], lever="curiosity + reciprocity",
                 optional=opt if kind != "research" else [])


def _patient_note(ctx: Ctx, item: dict) -> str:
    title = str(item.get("title") or "").rstrip(".")
    src = clean_source(item.get("source"), ctx.ref_date)
    summary = str(item.get("summary") or "").strip()
    who = {"dentists": "patients", "gyms": "members", "salons": "clients"}.get(ctx.slug, "customers")
    parts = []
    if summary:
        parts.append(f"Summary ({src}): {summary}")
    if ctx.slug == "dentists":
        parts.append(f"Draft for your {who}: \"New research ({src}): {title}. If you've had fillings or cavities recently, "
                     f"ask us at your next visit whether a shorter check-up interval makes sense for you.\"")
    else:
        parts.append(f"Draft for your {who}: \"New from {src}: {title}. Ask us how this applies to you on your next visit.\"")
    return "\n".join(parts)


def pb_regulation(ctx: Ctx) -> Draft | None:
    item = ctx.item
    if not item:
        return None
    ctx.use("t.item")
    src, title = clean_source(item.get("source"), ctx.ref_date).strip(), str(item.get("title") or "").strip()
    lead = [ctx.pick(f"{ctx.sal}, compliance heads-up: {title} ({src}).", f"{ctx.sal}, {src} — {title}.")]
    detail = [d if d.endswith(".") else d + "." for d in split_sentences(item.get("summary"))[:2]]
    opt = detail[1:]
    lead.extend(detail)
    dl = parse_date(ctx.payload.get("deadline_iso"))
    if dl and fmt_day_month(dl).split()[0] not in title:
        lead.append(f"Deadline: {ctx.derive('d.deadline', dl, fmt_day_month(dl) + f' {dl.year}', ('t.deadline_iso',))}.")
    act = str(item.get("actionable") or "").strip()
    if act:
        lead.append(f"What to do: {speak(act)}.")
    cta = yes_cta(ctx, "send a ready audit checklist so your setup is documented well before the deadline",
                  "Deadline se pehle setup document ho jaaye, iske liye ek ready audit checklist bhej doon")
    return Draft(lead, cta, "binary_yes_no", f"compliance change '{title}' with deadline", ctx.used,
                 action_on_yes="send the audit checklist", template_params=[ctx.sal, title, src],
                 yes_artifact="Audit checklist: (1) list every device/film type in use, (2) check each against the new limit, "
                              "(3) record the result in your SOP file, (4) plan replacements before the deadline.",
                 lever="loss aversion (deadline)", optional=opt)


def pb_cde(ctx: Ctx) -> Draft | None:
    item = ctx.item
    if not item:
        return None
    ctx.use("t.item")
    title, src = str(item.get("title") or ""), clean_source(item.get("source"), ctx.ref_date)
    if ":" in title and title.split(":", 1)[0].strip().lower() in src.lower():
        title = title.split(":", 1)[1].strip()
    when = ""
    dt = parse_dt(item.get("date"))
    if dt:
        when = ctx.derive("d.event_when", dt, f"{fmt_dow_day_month(dt)}, {fmt_time(dt)}" if dt.hour else fmt_dow_day_month(dt),
                          ("t.item",))
    lead = [f"{ctx.sal}, {src}: \"{title}\"" + (f" on {when}." if when else ".")]
    credits = num(ctx.payload.get("credits")) or num(item.get("credits"))
    bits = []
    if credits:
        bits.append(f"{fmt_int(credits)} CDE credits")
    act = str(item.get("actionable") or "").strip()
    fee = act if act else humanize(ctx.payload.get("fee")) if ctx.payload.get("fee") else ""
    if fee:
        bits.append(lcfirst(fee.rstrip(".")))
    if bits:
        lead.append("; ".join(bits)[:1].upper() + "; ".join(bits)[1:] + ".")
    speaker = first_sentence(item.get("summary"))
    rest = str(item.get("summary") or "").replace(speaker, "").strip()
    if speaker:
        lead.append(speaker + (" " + first_sentence(rest) if rest else ""))
    cta = yes_cta(ctx, "block the slot and send you the registration details",
                  "Slot block karke registration details bhej doon")
    return Draft(lead, cta, "binary_yes_no", f"CDE event '{title}'", ctx.used, action_on_yes="block the slot and share registration details",
                 template_params=[ctx.sal, title, when or src], lever="curiosity + effort externalisation")


def pb_supply_alert(ctx: Ctx) -> Draft | None:
    item = ctx.item or {}
    mol = str(ctx.payload.get("molecule") or "").strip()
    batches = [str(b) for b in as_list(ctx.payload.get("affected_batches")) if b]
    mfr = str(ctx.payload.get("manufacturer") or "").strip()
    if not (mol or item):
        return None
    ctx.use("t.item") if item else None
    for k in ("t.molecule", "t.manufacturer"):
        ctx.use(k)
    src = clean_source(item.get("source"), ctx.ref_date).strip()
    head = f"{ctx.sal}, urgent: voluntary recall on {mol or 'a molecule you stock'}"
    if batches:
        head += f" batches {join_human(batches)}"
        ctx.derive("d.batches", batches, join_human(batches), ())
    if mfr:
        head += f" ({mfr})"
    head += f", per {src}." if src else "."
    lead = [head]
    safety = sentence_with(item.get("summary"), ("safety", "risk"))
    potency = sentence_with(item.get("summary"), ("potency", "flagged"))
    if potency:
        lead.append(re.sub(r"\s*\(numbers in alert\)", "", potency))
    if safety and safety != potency:
        lead.append(safety)
    if ctx.L.has("m.agg.chronic_rx_count"):
        lead.append(f"Your {ctx.t('m.agg.chronic_rx_count')} are the list to check first.")
    cta = yes_cta(ctx, f"filter your repeat-Rx list for {mol or 'it'} and draft the customer note",
                  f"Repeat-Rx list ko {mol or 'is molecule'} ke liye filter karke customer note draft kar doon")
    return Draft(lead, cta, "binary_yes_no", f"supply recall on {mol}", ctx.used,
                 action_on_yes=f"filter the repeat-Rx list for {mol} and draft the customer note",
                 yes_artifact=(f"Customer note draft: \"Namaste, {ctx.biz} here. A few batches of {mol} have been recalled by the manufacturer "
                               f"for lower strength; it's not a safety issue. Please bring your strip in and we'll replace it free.\""),
                 template_params=[ctx.sal, mol, join_human(batches)], lever="urgency + effort externalisation")


def pb_perf_dip(ctx: Ctx) -> Draft | None:
    a = ctx.a
    metric, d = a.metric, a.metric_delta
    if not metric or d is None:
        return None
    pct = ctx.derive(f"d.dip.{metric}", d, fmt_pct(abs(d)), (f"m.delta.{metric}",), alt=(str(d),))
    lead = [ctx.pick(f"{ctx.sal}, your {metric} are down {pct} this week.",
                     f"{ctx.sal}, heads-up: {metric} dropped {pct} week-on-week.")]
    peer = ctx.L.get(f"d.{metric}_vs_peer")
    if peer and as_dict(peer.value).get("ratio", 1) < 1:
        lead.append(f"That's {ctx.t(f'd.{metric}_vs_peer')}.")
    lever = best_lever(ctx)
    if not lever:
        return None
    obs, en, hi, fids = lever
    for f in fids:
        ctx.use(f)
    lead.append(obs)
    cta = yes_cta(ctx, en, hi)
    explain = f"your {metric} fell {pct} week-on-week" + (f" and you're at {peer.text}" if peer and as_dict(peer.value).get("ratio", 1) < 1 else "") + \
        "; of the gaps on your profile, this one is the quickest to fix."
    return Draft(lead, cta, "binary_yes_no", f"{metric} dip {pct} + one recovery lever", ctx.used, action_on_yes=en,
                 template_params=[ctx.sal, f"{metric} -{pct}", en], lever="loss aversion + single fix", explain=explain)


def pb_perf_check(ctx: Ctx) -> Draft | None:
    """Trigger claimed a dip the data does not show: say so honestly, then point at the one real gap."""
    ups = []
    for m in ("views", "calls"):
        f = ctx.L.get(f"m.delta.{m}")
        if f is not None and f.value is not None and f.value >= 0:
            ctx.use(f"m.delta.{m}")
            ups.append(f"{m} up {fmt_pct(f.value)}")
    lever = best_lever(ctx)
    gap = None
    for m in ("calls", "views"):
        pf = ctx.L.get(f"d.{m}_vs_peer")
        if pf and as_dict(pf.value).get("ratio", 1) < 0.9:
            gap = f"d.{m}_vs_peer"
            break
    if not lever and not gap:
        return None
    lead = [f"{ctx.sal}, quick look at your numbers: " + (join_human(ups) + " week-on-week, so nothing is dipping right now."
                                                          if ups else "no drop this week.")]
    opt = []
    if gap:
        lead.append(f"The gap worth closing is {ctx.t(gap)}.")
    if lever:
        obs, en, hi, fids = lever
        for f in fids:
            ctx.use(f)
        lead.append(obs)
        if gap:
            opt.append(obs)
        cta = yes_cta(ctx, en, hi)
    else:
        cta = yes_cta(ctx, "draft a fresh Google post to push calls", "Calls badhane ke liye ek fresh Google post draft kar doon")
        en = "draft a fresh Google post"
    return Draft(lead, cta, "binary_yes_no", "honest status (no dip in data) + one real gap", ctx.used,
                 action_on_yes=en, template_params=[ctx.sal, join_human(ups), en], lever="reciprocity + single fix", optional=opt)


def pb_seasonal_dip(ctx: Ctx) -> Draft | None:
    a = ctx.a
    if not a.metric or a.metric_delta is None:
        return pb_perf_check(ctx)
    pct = ctx.derive(f"d.dip.{a.metric}", a.metric_delta, fmt_pct(abs(a.metric_delta)), (f"m.delta.{a.metric}",),
                     alt=(str(a.metric_delta),))
    note = str(ctx.payload.get("season_note") or "").lower()
    bid = None
    for f in ctx.L.prefix("c.beat."):
        mr = str(as_dict(f.value).get("month_range", ""))
        months = [x for x in re.findall(r"[a-z]{3}", mr.lower())]
        if months and all(m in note for m in months):
            bid = f.id
            break
    bid = bid or beat_for(ctx, ctx.month())
    lead = [ctx.pick(f"{ctx.sal}, your {a.metric} are down {pct} this week, and that's the season, not you.",
                     f"{ctx.sal}, {a.metric} dipped {pct} this week; this one is seasonal, not something you did.")]
    if bid and (ctx.payload.get("is_expected_seasonal") or note):
        lead.append(beat_sentence(ctx, bid))
    base = ctx.L.get("m.agg.total_active_members") or ctx.L.get("m.agg.total_unique_ytd")
    if base:
        lead.append(f"The best use of a quiet stretch is your {ctx.t(base.id)}.")
        cta = yes_cta(ctx, "draft a consistency challenge to keep them coming",
                      "Unhe regular rakhne ke liye ek consistency challenge draft kar doon")
    else:
        cta = yes_cta(ctx, "draft a retention message for your current regulars", "Regulars ke liye ek retention message draft kar doon")
    return Draft(lead, cta, "binary_yes_no", f"expected seasonal {a.metric} dip reframed toward retention", ctx.used,
                 action_on_yes="draft the retention challenge", template_params=[ctx.sal, pct], lever="anxiety relief + retention")

def _months(mr: str) -> set[int]:
    from .util import months_in_range
    return months_in_range(mr)


def pb_perf_spike(ctx: Ctx) -> Draft | None:
    a = ctx.a
    if not a.metric or a.metric_delta is None:
        return None
    pct = ctx.derive(f"d.spike.{a.metric}", a.metric_delta, fmt_pct(a.metric_delta), (f"m.delta.{a.metric}",),
                     alt=(str(a.metric_delta),))
    strong = abs(a.metric_delta) >= 0.10
    lead = [ctx.pick(f"{ctx.sal}, good week: {a.metric} are up {pct}.",
                     f"{ctx.sal}, your {a.metric} " + ("jumped" if strong else "are up") + f" {pct} this week.")]
    driver = ctx.payload.get("likely_driver")
    if driver:
        ctx.use("t.likely_driver")
        lead.append(f"The likely driver is your {humanize(driver)}.")
    cur = ctx.L.get(f"m.perf.{a.metric}")
    if cur:
        lead.append(f"That's {ctx.t(cur.id)} over the last {ctx.t('m.perf.window')}.")
    if driver:
        cta = yes_cta(ctx, f"put out a follow-up to the {humanize(driver)} while it's working",
                      f"{humanize(driver)} ka follow-up post abhi daal doon")
        act = f"post a follow-up to the {humanize(driver)}"
    else:
        lever = best_lever(ctx)
        if lever:
            obs, act, hi, fids = lever
            for f in fids:
                ctx.use(f)
            lead.append("To keep it going: " + lcfirst(obs))
            cta = yes_cta(ctx, act, hi)
        else:
            act = "draft a Google post to ride the momentum"
            cta = yes_cta(ctx, act, "Momentum ke liye ek Google post draft kar doon")
    return Draft(lead, cta, "binary_yes_no", f"{a.metric} up {pct}: capitalise on momentum", ctx.used, action_on_yes=act,
                 template_params=[ctx.sal, f"{a.metric} +{pct}"], lever="momentum")


def pb_milestone(ctx: Ctx) -> Draft | None:
    p = ctx.payload
    vn, mv = num(p.get("value_now")), num(p.get("milestone_value"))
    metric = humanize(p.get("metric") or "")
    pos = ctx.review("pos")
    if vn is not None and mv is not None and not ctx.a.placeholder:
        ctx.use("t.value_now"), ctx.use("t.milestone_value")
        label = "reviews" if "review" in metric else metric
        if vn < mv:
            gap = ctx.derive("d.milestone_gap", mv - vn, fmt_int(mv - vn), ("t.value_now", "t.milestone_value"))
            lead = [ctx.pick(f"{ctx.sal}, you're {gap} {label} away from {fmt_int(mv)} on Google (at {fmt_int(vn)} now).",
                             f"{ctx.sal}, {fmt_int(vn)} {label} and counting: just {gap} more to cross {fmt_int(mv)}.")]
        else:
            lead = [f"{ctx.sal}, you've crossed {fmt_int(mv)} {label} (now {fmt_int(vn)})."]
        if pos:
            lead.append(f"Your happiest regulars are easy to ask: {ctx.t(pos[0].id)}.")
        cta = yes_cta(ctx, "draft a short review-request message for this week's regulars",
                      "Is hafte ke regulars ke liye ek chhota review-request message draft kar doon")
        return Draft(lead, cta, "binary_yes_no", f"{label} milestone within reach", ctx.used,
                     action_on_yes="draft the review-request message", template_params=[ctx.sal, f"{fmt_int(vn)}/{fmt_int(mv)}"],
                     lever="goal gradient")
    for key in ("total_unique_ytd", "total_active_members"):
        d = ctx.L.get(f"d.milestone.{key}")
        if d:
            mark = as_dict(d.value)["mark"]
            src = ctx.t(f"m.agg.{key}")
            ctx.use(d.id)
            lead = [f"{ctx.sal}, small milestone worth marking: {src}, past the {fmt_int(mark)} mark."]
            cta = yes_cta(ctx, "turn it into a thank-you post for your regulars", "Isse ek thank-you post bana doon regulars ke liye")
            return Draft(lead, cta, "binary_yes_no", "milestone derived from merchant's own customer count", ctx.used,
                         action_on_yes="draft the thank-you post", template_params=[ctx.sal, fmt_int(mark)], lever="pride + social proof")
    return None


def pb_competitor(ctx: Ctx) -> Draft | None:
    p = ctx.payload
    name, dist, their = p.get("competitor_name"), num(p.get("distance_km")), p.get("their_offer")
    pos = ctx.review("pos")
    own = offer_line(ctx)
    if name and not ctx.a.placeholder:
        for k in ("t.competitor_name", "t.distance_km", "t.their_offer", "t.opened_date"):
            ctx.use(k)
        opened = parse_date(p.get("opened_date"))
        when = f" on {ctx.derive('d.opened', opened, fmt_day_month(opened), ('t.opened_date',))}" if opened else ""
        cmp = _price_gap(ctx, own, their)
        lead = [f"{ctx.sal}, {name} opened {dist:g} km from you{when}" + (f" with '{their}'" if their else "") +
                (f", {cmp[0].lower() + cmp[1:-1]}." if cmp else ".")]
    else:
        loc = ctx.t("m.locality")
        lead = [f"{ctx.sal}, heads-up: a new competitor listing has come up" + (f" near {loc}." if loc else " near you.")]
    if pos:
        r = as_dict(pos[0].value)
        quote = r.get("common_quote")
        occ = num(r.get("occurrences_30d"))
        ctx.use(pos[0].id)
        lead.append((ctx.pick("No need to race on price: ", "Don't match the price: ") if their else
                     ctx.pick("Best defence is what's already working: ", "Lean on what already works: ")) +
                    (f"your reviews already say \"{quote}\"" + (f" ({fmt_int(occ)} mentions this month)." if occ else ".") if quote
                     else f"{ctx.t(pos[0].id)}."))
        if quote:
            ctx.derive("d.quote", quote, quote, (pos[0].id,))
        cta = yes_cta(ctx, "draft a Google post that leads with that", "Isi ko lead karte hue ek Google post draft kar doon")
        act = "draft the Google post leading with your review strength"
    elif own:
        ctx.use(own[0])
        lead.append(f"Keep '{own[1]}' front and centre on the profile rather than cutting price.")
        cta = yes_cta(ctx, "refresh your profile post around it this week", "Iske around profile post refresh kar doon")
        act = "refresh the profile post around your offer"
    else:
        lever = best_lever(ctx)
        if not lever:
            return None
        obs, act, hi, fids = lever
        for f in fids:
            ctx.use(f)
        lead.append(obs)
        cta = yes_cta(ctx, act, hi)
    quote_txt = ""
    if pos and as_dict(pos[0].value).get("common_quote"):
        quote_txt = f" (\"{as_dict(pos[0].value)['common_quote']}\")"
    explain = ("A price cut is easy for a new competitor to copy and trains customers to shop on price; "
               f"what they can't copy quickly is the reputation your reviews already show{quote_txt}.")
    return Draft(lead, cta, "binary_yes_no", "competitor opened: defend on strengths, not price", ctx.used, action_on_yes=act,
                 template_params=[ctx.sal, str(name or "new competitor")], lever="loss aversion + social proof", explain=explain)


def _price_gap(ctx: Ctx, own: tuple[str, str] | None, their: Any) -> str:
    if not own or not their:
        return ""
    po, pt = _price(own[1]), _price(str(their))
    so, st = _service(own[1]), _service(str(their))
    if po and pt and so and st and so == st and pt < po:
        ctx.use(own[0])
        gap = ctx.derive("d.price_gap", po - pt, fmt_rupee(po - pt), (own[0], "t.their_offer"))
        return f"That's {gap} under your '{own[1]}'."
    return ""


def _price(title: str) -> float | None:
    m = re.search(r"₹\s?([\d,]+)", title)
    return num(m.group(1)) if m else None


def _service(title: str) -> str:
    return re.sub(r"\s*@.*$", "", title).strip().lower()


def pb_festival(ctx: Ctx) -> Draft | None:
    p = ctx.payload
    fest = p.get("festival")
    fdate = parse_date(p.get("date"))
    if fest and not ctx.a.placeholder:
        ctx.use("t.festival")
        when = ctx.derive("d.fest_date", fdate, fmt_day_month(fdate), ("t.date",)) if fdate else ""
        days = num(p.get("days_until"))
        head = f"{ctx.sal}, {fest} is on {when}" if when else f"{ctx.sal}, {fest} is coming up"
        if days and days_consistent(fdate, days, ctx.ref_date):
            ctx.use("t.days_until")
            head += f", {fmt_int(days)} days away"
        lead = [head + "."]
        bid = beat_for(ctx, fdate.month) if fdate else None
        if bid:
            lead.append(beat_sentence(ctx, bid))
        priced = sorted(((fid, t) for fid, t in ctx.active_offers() if _price(t)), key=lambda x: -_price(x[1]))
        off = priced[0] if priced else offer_line(ctx)
        if off:
            ctx.use(off[0])
            lead.append(f"Your '{off[1]}' is a ready hook for pre-{fest} bookings.")
            cta = yes_cta(ctx, f"draft a pre-{fest} booking post around it", f"Iske saath ek pre-{fest} booking post draft kar doon")
        else:
            cta = yes_cta(ctx, f"draft a pre-{fest} booking post for your profile", f"Ek pre-{fest} booking post draft kar doon")
        return Draft(lead, cta, "binary_yes_no", f"{fest} timing + category season", ctx.used, action_on_yes=f"draft the pre-{fest} post",
                     template_params=[ctx.sal, str(fest), when], lever="timing + effort externalisation")
    # placeholder: only speak if the category's own calendar marks this month as a festive window
    m = ctx.month()
    if not m:
        return None
    for i, b in enumerate(as_list(ctx.category.get("seasonal_beats"))):
        note = str(as_dict(b).get("note", ""))
        if m in _months(str(as_dict(b).get("month_range", ""))) and re.search(r"festiv|wedding|diwali|holiday", note, re.I):
            lead = [f"{ctx.sal}, festive season is coming up. {beat_sentence(ctx, f'c.beat.{i}')}"]
            base = ctx.L.get("m.agg.total_unique_ytd") or ctx.L.get("m.agg.total_active_members")
            if base:
                lead.append(f"You already have {ctx.t(base.id)} to invite back.")
            cta = yes_cta(ctx, "draft a festive comeback offer message for them", "Unke liye ek festive comeback message draft kar doon")
            return Draft(lead, cta, "binary_yes_no", "festive window from category calendar", ctx.used,
                         action_on_yes="draft the festive comeback message", template_params=[ctx.sal], lever="timing")
    return None


def pb_ipl(ctx: Ctx) -> Draft | None:
    p = ctx.payload
    match, venue = p.get("match"), p.get("venue")
    if not match:
        return None
    ctx.use("t.match"), ctx.use("t.venue")
    dt = parse_dt(p.get("match_time_iso"))
    tm = ctx.derive("d.match_time", dt, fmt_time(dt), ("t.match_time_iso",)) if dt else ""
    lead = [f"{ctx.sal}, {match}" + (f" at {venue}" if venue else "") + (f" tonight, {tm}." if tm else " today.")]
    item = None
    for f in ctx.L.prefix("c.digest."):
        if "ipl" in (str(as_dict(f.value).get("id", "")) + str(as_dict(f.value).get("title", ""))).lower():
            item = f
    weeknight = p.get("is_weeknight")
    ipl_opt: list[str] = []
    act = "put up a match-night post for tonight"
    if item:
        ctx.use(item.id)
        summ = str(as_dict(item.value).get("summary", ""))
        if weeknight is False:
            s = sentence_with(summ, ("saturday",)) or sentence_with(summ, ("down",))
            if s:
                lead.append(s)
            lead.append("So tonight is a delivery night, not a dine-in promo night.")
            ipl_opt = [lead[-1]]
            act = "set up a delivery-first match post for tonight"
        elif weeknight is True:
            s = sentence_with(summ, ("weeknight",))
            if s:
                lead.append(s)
    if ctx.L.has("d.delivery_share") and weeknight is False:
        lead.append(f"Fits your mix too: {ctx.t('d.delivery_share')}.")
    off = offer_line(ctx)
    if off:
        ctx.use(off[0])
        if weeknight is False and re.search(r"tue|wed|thu", off[1], re.I):
            lead.append(f"Your '{off[1]}' is weekday-only, so save it for a weeknight match.")
        else:
            lead.append(f"Your '{off[1]}' is the natural hook.")
    cta = yes_cta(ctx, act, "Aaj raat ke liye delivery-first match post set kar doon" if weeknight is False else "Aaj ke match ka post set kar doon")
    why = []
    if item:
        s1 = sentence_with(str(as_dict(item.value).get("summary", "")), ("saturday" if weeknight is False else "weeknight",))
        if s1:
            why.append(s1.rstrip("."))
    if ctx.L.has("d.delivery_share"):
        why.append(ctx.L.text("d.delivery_share"))
    explain = ("; ".join(why) + ".") if why else ""
    return Draft(lead, cta, "binary_yes_no", "IPL match today with category data on match-night behaviour", ctx.used,
                 action_on_yes=act, template_params=[ctx.sal, str(match), tm], lever="counter-intuitive insight", explain=explain,
                 optional=ipl_opt)


def pb_review_theme(ctx: Ctx) -> Draft | None:
    p = ctx.payload
    theme, occ, quote = p.get("theme"), num(p.get("occurrences_30d")), p.get("common_quote")
    if not theme:
        neg = ctx.review("neg")
        if not neg:
            return None
        r = as_dict(neg[0].value)
        theme, occ, quote = r.get("theme"), num(r.get("occurrences_30d")), r.get("common_quote")
        ctx.use(neg[0].id)
    else:
        for k in ("t.theme", "t.occurrences_30d", "t.common_quote"):
            ctx.use(k)
    lead = [f"{ctx.sal}, " + (f"{fmt_int(occ)} reviews in the last 30 days mention {humanize(theme)}" if occ else
                              f"reviews are flagging {humanize(theme)}") +
            (f", latest: \"{quote}\"." if quote else ".")]
    if str(p.get("trend")) == "rising":
        lead.append("It's rising, so it's worth answering now before it shapes your rating.")
    cta = yes_cta(ctx, "draft a short public reply you can post under these reviews",
                  "In reviews ke neeche daalne ke liye ek short reply draft kar doon")
    return Draft(lead, cta, "binary_yes_no", f"review theme '{humanize(theme)}'", ctx.used,
                 action_on_yes="draft the public review reply", template_params=[ctx.sal, humanize(theme)], lever="loss aversion (reputation)")


def pb_curious(ctx: Ctx) -> Draft | None:
    offers = ctx.active_offers()
    guess = None
    matched_trend = None
    own_text = " ".join([t for _, t in offers] + [str(as_dict(f.value).get("common_quote", "")) + " " + f.id
                                                   for f in ctx.review("pos")]).lower()
    for f in ctx.L.prefix("c.trend."):
        q = str(as_dict(f.value).get("query", "")).lower()
        core = [w for w in re.findall(r"[a-z]{4,}", q) if w not in {"near", "price", "cost", "delhi"}]
        if core and all(w in own_text for w in core[:2]):
            matched_trend = f
            break
    if offers:
        guess = offers[-1][1] if len(offers) > 1 else offers[0][1]
        ctx.use(offers[-1][0] if len(offers) > 1 else offers[0][0])
        guess = re.sub(r"\s*@.*$", "", guess)
    beat_txt = ""
    if ctx.month():
        for i, b in enumerate(as_list(ctx.category.get("seasonal_beats"))):
            if ctx.month() in _months(str(as_dict(b).get("month_range", ""))):
                beat_txt = ctx.t(f"c.beat.{i}")
                break
    name = ctx.t("m.name") or "your place"
    lead = [ctx.pick(f"{ctx.sal}, quick one for this week's Google post.",
                     f"{ctx.sal}, I'd like to build this week's Google post from what customers are actually asking for.")]
    trend = matched_trend or _top_trend(ctx)
    if matched_trend:
        lead.append(f"Across metros, {ctx.t(matched_trend.id)}.")
        guess = re.sub(r"\b(price|cost|near me|delhi)\b", "", str(matched_trend.value.get("query", ""))).strip()
    elif trend and not guess:
        lead.append(f"Across metros, {ctx.t(trend.id)}.")
        guess = str(trend.value.get("query", "")).replace(" near me", "").replace(" delhi", "")
        guess = re.sub(r"\b(price|cost)\b", "", guess).strip()
    elif beat_txt:
        lead.append(beat_sentence(ctx, beat_for(ctx, ctx.month())))
    word = {"restaurants": "dish", "pharmacies": "product"}.get(ctx.slug, "service")
    if guess:
        cta = ctx.hl(f"What's been the most asked-for {word} at {name} this week, still {guess} or something new?",
                     f"Is hafte {name} mein sabse zyada kis {word} ki demand rahi, {guess} hi ya kuch naya?")
    else:
        cta = ctx.hl(f"What's been the most asked-for {word} at {name} this week?",
                     f"Is hafte {name} mein sabse zyada kis {word} ki demand rahi?")
    return Draft(lead, cta, "open_ended", "weekly ask-the-merchant with an informed guess", ctx.used,
                 action_on_yes="turn the answer into a Google post and a ready price reply",
                 template_params=[ctx.sal, guess or ""], lever="asking the merchant")


def pb_planning(ctx: Ctx) -> Draft | None:
    p = ctx.payload
    topic = humanize(p.get("intent_topic") or "the plan")
    hist = as_list(ctx.v("m.history"))
    prior = ""
    for h in reversed(hist):
        if as_dict(h).get("from") == "vera" and re.search(r"\d", str(as_dict(h).get("body", ""))):
            prior = str(h.get("body"))
            break
    lead: list[str] = []
    artifact = ""
    if "thali" in topic or "bulk" in topic or "corporate" in topic:
        off = next(((fid, t) for fid, t in ctx.active_offers() if _price(t)), None)
        if off:
            price = _price(off[1])
            t1 = ctx.derive("d.tier1", round(price * 0.9 / 5) * 5, fmt_rupee(round(price * 0.9 / 5) * 5), (off[0],))
            t2 = ctx.derive("d.tier2", round(price * 0.85 / 5) * 5, fmt_rupee(round(price * 0.85 / 5) * 5), (off[0],))
            ctx.derive("d.tier_sizes", (10, 25, 10, 15), "10+ and 25+ at 10% and 15% off", ())
            svc = _service(off[1]).lower()
            lead = [f"{ctx.sal}, here's a first cut of the corporate {svc} package. The bulk prices are only suggestions, "
                    f"10% and 15% below your {fmt_rupee(price)} thali, so set your own:",
                    f"• 10+ per day: {t1} each (suggested)", f"• 25+ per day: {t2} each (suggested)",
                    "• Orders by the previous evening, weekday lunch delivery."]
            pos = next((f for f in ctx.review("pos") if "thali" in f.id), None) or (ctx.review("pos") or [None])[0]
            if pos:
                lead.append(f"Lead the pitch with your reviews: {ctx.t(pos.id)}.")
            artifact = "\n".join(lead[1:4])
    if not lead and prior:
        m = re.search(r"\b(?:suggest(?:ed|ion)?|proposal|plan)\b[:\s]*([^.?]*\d[^.?]*)", prior, re.I)
        spec = (m.group(1) if m else first_sentence(prior, must_have_digit=True)).strip().rstrip(".")
        ctx.L.add("m.history.proposal", "MERCHANT", "conversation_history[vera]", spec, spec)
        ctx.use("m.history.proposal")
        loc = ctx.t("m.locality")
        post = (f"\"{topic.title()} at {ctx.biz}" + (f", {loc}" if loc else "") + f": {spec}.")
        small = next((f for f in ctx.review("pos") if "small" in f.id or "instructor" in f.id), None)
        if small:
            post += " Small batches, experienced instructors."
            ctx.use(small.id)
        post += " Reply to reserve a spot.\""
        lead = [f"{ctx.sal}, done — here's the Google post draft for the {topic}, using the structure we discussed ({spec}):", post]
        artifact = post
    if not lead:
        off = offer_line(ctx)
        lead = [f"{ctx.sal}, here's a starting draft for the {topic}:",
                f"\"{topic.title()} at {ctx.biz}" + (f" — {off[1]}" if off else "") + ". Reply to know more.\""]
        if off:
            ctx.use(off[0])
        artifact = lead[1]
    cta = ctx.hl("Reply CONFIRM and I'll publish it, or send any edits.",
                 "CONFIRM reply kijiye, main publish kar dungi, ya edits bhej dijiye.")
    return Draft(lead, cta, "binary_confirm_cancel", f"merchant asked for '{topic}': delivered the draft, no re-pitch", ctx.used,
                 action_on_yes="publish the drafted post", yes_artifact=artifact, template_params=[ctx.sal, topic],
                 lever="effort externalisation")


def pb_renewal(ctx: Ctx) -> Draft | None:
    p = ctx.payload
    days = num(p.get("days_remaining")) or ctx.v("m.sub.days_remaining")
    if not days or days > 45:
        return None  # nothing is actually due soon: a renewal nudge now would be noise
    ctx.use("t.days_remaining") if "days_remaining" in p else ctx.use("m.sub.days_remaining")
    plan = p.get("plan") or ctx.v("m.sub.plan")
    amt = num(p.get("renewal_amount"))
    if str(plan).lower() == "trial":
        head = f"{ctx.sal}, your free trial ends in {fmt_int(days)} days"
    else:
        head = f"{ctx.sal}, your {plan + ' ' if plan else ''}plan renews in {fmt_int(days)} days"
    if amt:
        ctx.use("t.renewal_amount")
        head += f" ({fmt_rupee(amt)})"
    lead = [head + "."]
    calls, views = ctx.L.get("m.perf.calls"), ctx.L.get("m.perf.views")
    dc = ctx.L.get("m.delta.calls")
    if dc is not None and dc.value is not None and dc.value <= -0.1:
        lead.append(f"With {ctx.t('m.delta.calls')}, a gap in profile upkeep now would cost more than usual.")
    elif calls and views:
        verb = "upgrading" if str(plan).lower() == "trial" else "renewing"
        lead.append(f"The profile brought {ctx.t('m.perf.calls')} and {ctx.t('m.perf.views')} in the last {ctx.t('m.perf.window')}; {verb} keeps that running without a gap.")
    if str(plan).lower() == "trial":
        cta = yes_cta(ctx, "switch you to the full plan so nothing pauses", "Full plan activate kar doon taaki kuch pause na ho")
    else:
        cta = yes_cta(ctx, "process the renewal so nothing pauses", "Renewal process kar doon taaki kuch pause na ho")
    return Draft(lead, cta, "binary_yes_no", "renewal window with merchant's own results at stake", ctx.used,
                 action_on_yes="process the renewal", template_params=[ctx.sal, fmt_int(days)], lever="loss aversion")


def pb_winback(ctx: Ctx) -> Draft | None:
    p = ctx.payload
    days = num(p.get("days_since_expiry")) or ctx.v("m.sub.days_since_expiry")
    lead = []
    if days:
        ctx.use("t.days_since_expiry") if "days_since_expiry" in p else ctx.use("m.sub.days_since_expiry")
        lead.append(f"{ctx.sal}, it's been {fmt_int(days)} days since your plan lapsed.")
    else:
        lead.append(f"{ctx.sal}, your plan is currently inactive.")
    dip = num(p.get("perf_dip_pct"))
    if dip is not None and dip < 0:
        lead.append(f"Since then your profile performance is down {ctx.derive('d.winback_dip', dip, fmt_pct(abs(dip)), ('t.perf_dip_pct',))}")
        lapsed = num(p.get("lapsed_customers_added_since_expiry"))
        if lapsed:
            ctx.use("t.lapsed_customers_added_since_expiry")
            lead[-1] += f", and {fmt_int(lapsed)} more {ctx.aud} have gone quiet."
        else:
            lead[-1] += "."
    elif ctx.L.has("m.delta.calls") and (ctx.v("m.delta.calls") or 0) < 0:
        lead.append(f"Meanwhile {ctx.t('m.delta.calls')}.")
    cta = yes_cta(ctx, "restart your profile upkeep from today", "Aaj se profile upkeep dobara shuru kar doon")
    return Draft(lead, cta, "binary_yes_no", "lapsed subscription with measurable cost", ctx.used,
                 action_on_yes="restart the plan", template_params=[ctx.sal, fmt_int(days or 0)], lever="loss aversion")


def pb_dormant(ctx: Ctx) -> Draft | None:
    days = num(ctx.payload.get("days_since_last_merchant_message"))
    lead = []
    if days and not ctx.a.placeholder:
        ctx.use("t.days_since_last_merchant_message")
        lead.append(ctx.pick(f"{ctx.sal}, it's been {fmt_int(days)} days since we last spoke, so here's one thing from your numbers worth a look.",
                             f"{ctx.sal}, {fmt_int(days)} days since we last spoke; one quick thing from your dashboard."))
    else:
        lead.append(ctx.pick(f"{ctx.sal}, one quick thing from your dashboard.", f"{ctx.sal}, one quick thing on your profile."))
    dc = ctx.L.get("m.delta.calls")
    lapsed = ctx.L.get("m.agg.lapsed_90d_plus") or ctx.L.get("m.agg.lapsed_180d_plus")
    if dc is not None and (dc.value or 0) <= -0.1:
        obs = f"{ctx.t('m.delta.calls').capitalize()}"
        peer = ctx.L.get("d.calls_vs_peer")
        if peer and as_dict(peer.value).get("ratio", 1) < 1:
            obs += f", now {ctx.t('d.calls_vs_peer')}"
        lead.append(obs + ".")
    if lapsed:
        lead.append(f"You also have {ctx.t(lapsed.id)}.")
        cta = yes_cta(ctx, f"draft a win-back message for those {ctx.aud}", f"Un {ctx.aud} ke liye ek win-back message draft kar doon")
        act = "draft the win-back message"
    else:
        lever = best_lever(ctx)
        if not lever:
            return None
        obs, act, hi, fids = lever
        for f in fids:
            ctx.use(f)
        lead.append(obs)
        cta = yes_cta(ctx, act, hi)
    if len(lead) < 2:
        return None
    return Draft(lead, cta, "binary_yes_no", "re-engage with one useful observation (reciprocity)", ctx.used,
                 action_on_yes=act, template_params=[ctx.sal], lever="reciprocity")


def pb_gbp_unverified(ctx: Ctx) -> Draft | None:
    p = ctx.payload
    up = num(p.get("estimated_uplift_pct"))
    lead = [f"{ctx.sal}, your Google profile is still unverified."]
    if up:
        lead.append(f"Verified listings get an estimated {ctx.derive('d.uplift', up, fmt_pct(up), ('t.estimated_uplift_pct',))} more visibility, "
                    f"and you're at {ctx.t('m.perf.views') or 'your current views'} a month without it.")
    path = humanize(p.get("verification_path") or "").replace(" or ", " or a ")
    if path:
        ctx.use("t.verification_path")
        lead.append(f"Verification is by {path}; I'll handle the steps.")
    cta = yes_cta(ctx, "start the verification now", "Verification abhi start kar doon")
    return Draft(lead, cta, "binary_yes_no", "unverified profile with estimated uplift", ctx.used,
                 action_on_yes="start the verification", template_params=[ctx.sal, fmt_pct(up) if up else ""], lever="loss aversion")


def pb_category_seasonal(ctx: Ctx) -> Draft | None:
    trends = []
    for t in as_list(ctx.payload.get("trends")):
        m = re.match(r"^([A-Za-z_ ]+?)_demand_([+-]\d+)$", str(t))
        if m:
            trends.append((m.group(1).replace("_", " "), int(m.group(2))))
    if not trends:
        item = digest_by_kind(ctx, ("seasonal",))
        if not item:
            return None
        ctx.use(f"c.digest.{item['id']}")
        lead = [f"{ctx.sal}, seasonal shift in {ctx.slug}: {item['title']}."]
    else:
        ctx.use("t.trends") if ctx.L.has("t.trends") else None
        up = [f"{n if n.isupper() else n.lower()} up {v}%" for n, v in trends if v > 0]
        dn = [f"{n.lower().replace('cold cough', 'cold & cough')} is down {abs(v)}%" for n, v in trends if v < 0]
        ctx.derive("d.trends", trends, " ".join(up + dn), ())
        lead = [f"{ctx.sal}, the summer shift is showing up across pharmacies: {join_human(up)}" + (f", while {join_human(dn)}." if dn else ".")]
    item = digest_by_kind(ctx, ("seasonal",))
    if item and item.get("actionable"):
        ctx.use(f"c.digest.{item['id']}")
        lead.append(f"The usual shelf move: {speak(item['actionable'])}.")
    off = offer_line(ctx)
    if off:
        ctx.use(off[0])
        lead.append(f"Your '{off[1]}' makes a summer-kit bundle easy to push.")
    cta = yes_cta(ctx, "draft a summer-essentials post for your profile", "Profile ke liye ek summer-essentials post draft kar doon")
    return Draft(lead, cta, "binary_yes_no", "category seasonal demand shift + shelf action", ctx.used,
                 action_on_yes="draft the summer-essentials post", template_params=[ctx.sal], lever="timing")


def pb_generic(ctx: Ctx) -> Draft | None:
    lever = best_lever(ctx)
    if not lever:
        return None
    obs, en, hi, fids = lever
    for f in fids:
        ctx.use(f)
    lead = [f"{ctx.sal}, one thing on your profile worth fixing this week.", obs]
    cta = yes_cta(ctx, en, hi)
    return Draft(lead, cta, "binary_yes_no", "strongest verified gap in merchant data", ctx.used, action_on_yes=en,
                 template_params=[ctx.sal], lever="single fix")


# ---------------------------------------------------------------------------- customer playbooks

def cust_lang(ctx: Ctx) -> str:
    """'hi' (Hindi in Roman script), 'mix' (Hinglish) or 'en'."""
    pref = str(g(ctx.customer, "identity", "language_pref", default="")).lower().strip()
    if pref in ("hi", "hindi"):
        return "hi"
    if "hi" in pref and "mix" in pref:
        return "mix"
    return "en"


def cust_open(ctx: Ctx) -> str:
    name = ctx.t("cu.name")
    parent = ctx.t("cu.parent")
    who = parent or name
    via = "via_" in str(g(ctx.customer, "preferences", "channel", default=""))
    if who.startswith(("Mr. ", "Mrs. ", "Ms. ")) and cust_lang(ctx) != "en":
        who = who.split(" ", 1)[1]
    biz = ctx.biz
    if ctx.slug == "dentists" and ctx.owner:
        biz = f"{ctx.sal}'s clinic" if not ctx.biz.lower().startswith(ctx.sal.lower()) else ctx.biz
    loc = ctx.t("m.locality")
    if loc and loc.lower() not in biz.lower():
        biz = f"{biz}, {loc}"
    lang = cust_lang(ctx)
    emoji = {"dentists": " 🦷", "salons": " ✨", "gyms": " 💪"}.get(ctx.slug, "")
    if via and not parent:
        who = ""
    end = emoji if emoji else "."
    if lang == "hi":
        return f"Namaste {who} ji, {biz} se{end}" if who else f"Namaste, {biz} se{end}"
    if lang == "mix":
        return f"Hi {who}, {biz} se message{end}" if who else f"Namaste, {biz} se message{end}"
    return f"Hi {who}, {biz} here{end}" if who else f"Hello from {biz}{end}"


def _slot_labels(ctx: Ctx) -> list[str]:
    out = []
    for f in ctx.L.prefix("t.slot."):
        if f.text:
            out.append(f.text)
            ctx.use(f.id)
    return out


def cpb_recall(ctx: Ctx) -> Draft | None:
    p, lang = ctx.payload, cust_lang(ctx)
    svc = humanize(p.get("service_due") or ("check-up" if ctx.slug == "dentists" else "next visit"))
    last = parse_date(p.get("last_service_date")) or ctx.v("cu.last_visit")
    due = parse_date(p.get("due_date"))
    lead = [cust_open(ctx)]
    lv = ctx.derive("d.last", last, fmt_day_month(last), ("cu.last_visit",)) if last else ""
    du = ctx.derive("d.due", due, fmt_day_month(due), ("t.due_date",)) if due else ""
    if lang == "en":
        s = (f"Your last visit was on {lv}" if lv else "It's been a while since your last visit") + \
            (f", so your {svc} is due around {du}." if du else f", so your {svc} is due.")
    else:
        s = (f"Aapki last visit {lv} ko thi" if lv else "Aapki last visit ko kaafi time ho gaya") + \
            (f", aur {svc} {du} ke aas-paas due hai." if du else f", aur {svc} due hai.")
    lead.append(s)
    slots = _slot_labels(ctx)
    off = next(((fid, t) for fid, t in ctx.active_offers() if re.search(r"clean|check|consult", t, re.I)), None)
    if off:
        ctx.use(off[0])
        lead.append(ctx.hl(f"{off[1]} applies.", f"{off[1]} wala offer lagu hai.") if lang != "en" else f"{off[1]} applies.")
    if len(slots) >= 2:
        ctx.derive("d.slot_choices", (1, 2), "1 2", ())
        if lang == "en":
            lead.append(f"Two open slots: {slots[0]} or {slots[1]}.")
            cta = "Reply 1 or 2 to book, or send a time that suits you."
        else:
            lead.append(f"Do slots khaali hain: {slots[0]} ya {slots[1]}.")
            cta = "Book karne ke liye 1 ya 2 reply kijiye, ya apna time bata dijiye."
        ctype = "multi_choice_slot"
    elif len(slots) == 1:
        if lang == "en":
            lead.append(f"We've kept {slots[0]} open for you.")
            cta = "Reply YES to book it, or send a time that suits you better."
        else:
            lead.append(f"Aapke liye {slots[0]} ka slot rakha hai.")
            cta = "Book karne ke liye YES reply kijiye, ya apna time bata dijiye."
        ctype = "binary_yes_no"
    else:
        cta = "Reply YES and we'll share open slots." if lang == "en" else "YES reply kijiye, hum slots bhej denge."
        ctype = "binary_yes_no"
    return Draft(lead, cta, ctype, f"{svc} recall for customer with real slots/offer", ctx.used,
                 action_on_yes="book the slot", template_params=[ctx.t("cu.name"), ctx.biz, svc] + slots[:2], lever="convenience")


def cpb_appointment(ctx: Ctx) -> Draft | None:
    p, lang = ctx.payload, cust_lang(ctx)
    lead = [cust_open(ctx)]
    dt = parse_dt(p.get("appointment_iso") or p.get("slot_iso") or p.get("time_iso"))
    svc = humanize(p.get("service") or p.get("service_due") or "")
    if not svc and ctx.L.has("cu.services"):
        svc = ctx.t("cu.services")
    noun = {"restaurants": "table booking", "gyms": "session"}.get(ctx.slug, "appointment")
    when = f"tomorrow at {ctx.derive('d.appt', dt, fmt_time(dt), ())}" if dt else "tomorrow"
    if lang == "en":
        lead.append(f"A quick reminder of your {svc + ' ' if svc else ''}{noun} with us {when}.")
        off = offer_line(ctx)
        if off and len(" ".join(lead)) < 200:
            ctx.use(off[0])
            lead.append(f"'{off[1]}' is running this week if you'd like to add it.")
        cta = "Reply YES to confirm, or tell us if you need a different time."
    else:
        when_hi = f"kal {fmt_time(dt)} baje" if dt else "kal"
        lead.append(f"Yaad dila rahe hain, aapka {svc + ' ' if svc else ''}{noun} {when_hi} hai.")
        cta = "Confirm karne ke liye YES reply kijiye, ya naya time bata dijiye."
    return Draft(lead, cta, "binary_yes_no", "appointment reminder for tomorrow", ctx.used, action_on_yes="confirm the appointment",
                 template_params=[ctx.t("cu.name"), ctx.biz, when], lever="convenience")


def cpb_refill(ctx: Ctx) -> Draft | None:
    p, lang = ctx.payload, cust_lang(ctx)
    mols = [str(m) for m in as_list(p.get("molecule_list")) if m]
    out = parse_date(p.get("stock_runs_out_iso"))
    lead = [cust_open(ctx)]
    name = ctx.t("cu.name")
    who = f"{name.replace('Mr. ', '')} ji" if name else "aap"
    if mols:
        ctx.derive("d.mols", mols, join_human(mols), ())
    od = ctx.derive("d.runs_out", out, fmt_day_month(out), ("t.stock_runs_out_iso",)) if out else ""
    if lang in ("hi", "mix"):
        s = f"{who} ki {fmt_int(len(mols))} regular dawaiyan ({join_human(mols)})" if mols else f"{who} ki regular dawaiyan"
        s += f" {od} tak khatam ho jayengi." if od else " jaldi khatam hone wali hain."
        ctx.derive("d.mol_count", len(mols), fmt_int(len(mols)), ())
        lead.append(s)
        extras = []
        for fid, t in ctx.active_offers():
            if (re.search(r"senior", t, re.I) and ctx.v("cu.senior")) or re.search(r"deliver", t, re.I):
                extras.append(f"'{t}'")
                ctx.use(fid)
        if len(extras) > 1:
            lead.append(f"Same dawaiyan ready rakhenge; {' aur '.join(extras)} offers available hain" +
                        (", delivery saved address par." if p.get("delivery_address_saved") else "."))
        elif extras:
            lead.append(f"Same dawaiyan ready rakhenge; {extras[0]} offer available hai.")
        cta = "Dispatch ke liye CONFIRM reply kijiye, ya dose mein koi badlav ho toh bata dijiye."
    else:
        s = f"Your {join_human(mols)} refill" if mols else "Your regular refill"
        s += f" runs out on {od}." if od else " is due soon."
        lead.append(s)
        extras = [t for fid, t in ctx.active_offers() if re.search(r"deliver|senior", t, re.I)]
        if extras:
            lead.append(f"{join_human(extras)} apply.")
        cta = "Reply CONFIRM and we'll dispatch it, or tell us if the dose has changed."
    return Draft(lead, cta, "binary_confirm_cancel", "chronic refill before stock runs out", ctx.used,
                 action_on_yes="dispatch the refill", template_params=[who, ctx.biz, join_human(mols), od], lever="convenience + care")


def cpb_lapsed(ctx: Ctx) -> Draft | None:
    p, lang = ctx.payload, cust_lang(ctx)
    lead = [cust_open(ctx)]
    days = num(p.get("days_since_last_visit"))
    lv = ctx.t("cu.last_visit")
    focus = humanize(p.get("previous_focus") or "")
    lvd = ctx.v("cu.last_visit")
    if days and lvd and not days_consistent(lvd, days, ctx.ref_date, past=True):
        days = None                       # the payload count disagrees with today's date: say the date instead
    if days:
        ctx.use("t.days_since_last_visit")
        gap_en = f"It's been {fmt_int(days)} days since your last session"
        gap_hi = f"Aapki last visit ko {fmt_int(days)} din ho gaye"
    elif lv:
        gap_en = f"We haven't seen you since {lv}"
        gap_hi = f"{lv} ke baad aapse mulaqat nahi hui"
    else:
        gap_en, gap_hi = "It's been a while", "Kaafi time ho gaya"
    if lang == "en":
        lead.append(gap_en + ctx.pick(", and that's completely fine, it happens.", ", no pressure at all."))
    else:
        lead.append(gap_hi + ", koi baat nahi, aisa hota hai.")
    if focus:
        ctx.use("t.previous_focus")
    off = None
    for rx in (r"trial|free|analysis|check|clean", r"first|month"):
        off = off or next(((fid, t) for fid, t in ctx.active_offers() if re.search(rx, t, re.I)), None)
    off = off or offer_line(ctx)
    if off:
        ctx.use(off[0])
        if lang == "en":
            lead.append((f"If {focus} is still the goal, " if focus else "If you'd like to restart, ") + f"we have {off[1]} for you.")
        else:
            lead.append(f"Wapas shuru karna ho toh {off[1]} available hai.")
    elif lang == "en":
        lead.append("Whenever you're ready, we'd be glad to see you back.")
    else:
        lead.append("Jab aapko theek lage, hum aapka intezaar karenge.")
    pref = ctx.t("cu.pref_slots")
    if ctx.slug == "pharmacies":
        cta = ("Reply YES and we'll keep your regular medicines ready for pickup or delivery." if lang == "en"
               else "YES reply kijiye, hum aapki regular dawaiyan ready rakhenge.")
    elif ctx.slug == "restaurants":
        cta = ("Reply YES and we'll share this week's specials." if lang == "en"
               else "YES reply kijiye, hum is hafte ke specials bhej denge.")
    elif lang == "en":
        cta = f"Reply YES and we'll hold a {pref} slot for you." if pref else "Reply YES and we'll share this week's open slots."
    else:
        cta = "YES reply kijiye, hum aapke liye slot hold kar denge."
    return Draft(lead, cta, "binary_yes_no", "no-guilt return invitation using customer's own history", ctx.used,
                 action_on_yes="hold a slot", template_params=[ctx.t("cu.name"), ctx.biz], lever="warmth + low-commitment")


def cpb_checkin(ctx: Ctx) -> Draft | None:
    lang = cust_lang(ctx)
    content = None
    for f in ctx.L.prefix("c.content."):
        seasonal = re.search(r"summer|monsoon|winter|festive", f.text, re.I)
        in_season = bool(seasonal) and ctx.month() in (4, 5, 6) and "summer" in f.text.lower()
        if in_season or (not seasonal and content is None):
            content = f
            if in_season:
                break
    if not content:
        return None
    ctx.use(content.id)
    lead = [cust_open(ctx)]
    if lang == "en":
        lead.append(f"A quick, useful read from us: \"{content.text}\".")
        cta = "Reply YES and we'll send it over."
    else:
        lead.append(f"Aapke liye ek chhota useful note: \"{content.text}\".")
        cta = "YES reply kijiye, hum bhej denge."
    return Draft(lead, cta, "binary_yes_no", "neutral value-first check-in (customer is active, no lapse language)", ctx.used,
                 action_on_yes="send the content note", template_params=[ctx.t("cu.name"), ctx.biz], lever="reciprocity")


def cpb_trial(ctx: Ctx) -> Draft | None:
    p, lang = ctx.payload, cust_lang(ctx)
    td = parse_date(p.get("trial_date"))
    lead = [cust_open(ctx)]
    child = ctx.t("cu.name") if ctx.L.has("cu.parent") else ""
    tds = ctx.derive("d.trial", td, fmt_day_month(td), ("t.trial_date",)) if td else ""
    svc = humanize(ctx.v("cu.services") and ctx.v("cu.services")[-1] or "trial")
    if child:
        lead.append(f"Hope {child} enjoyed the {svc}" + (f" on {tds}." if tds else "."))
    elif lang == "en":
        lead.append(f"Thanks for coming in for the {svc}" + (f" on {tds}." if tds else "."))
    else:
        lead.append(f"{svc.capitalize()} ke liye aane ka shukriya" + (f" ({tds})." if tds else "."))
    slots = _slot_labels(ctx)
    if slots:
        lead.append(f"Next session: {slots[0]}.")
        cta = "Reply YES to book " + ("the spot." if not child else f"{child}'s spot.")
    elif lang == "en":
        cta = "Reply YES and we'll share the next session times."
    else:
        cta = "YES reply kijiye, hum agle session ka time bhej denge."
    return Draft(lead, cta, "binary_yes_no", "post-trial follow-up with the real next session", ctx.used,
                 action_on_yes="book the next session", template_params=[ctx.t("cu.name"), ctx.biz] + slots[:1], lever="momentum")


def cpb_wedding(ctx: Ctx) -> Draft | None:
    p = ctx.payload
    wd = parse_date(p.get("wedding_date"))
    days = num(p.get("days_to_wedding"))
    tc = parse_date(p.get("trial_completed"))
    step = humanize(p.get("next_step_window_open") or "")
    m30 = re.search(r"\s*(\d+)\s*-?\s*day\b", step)
    if m30:
        step = f"{m30.group(1)}-day " + (step[:m30.start()] + step[m30.end():]).strip()
    lead = [cust_open(ctx).replace(" ✨", " 💍")]
    s = ""
    if wd:
        wds = ctx.derive('d.wedding', wd, fmt_day_month(wd), ('t.wedding_date',))
        if days and days_consistent(wd, days, ctx.ref_date):
            ctx.use("t.days_to_wedding")
            lead.append(f"{fmt_int(days)} days to your wedding on {wds}!")
        else:
            lead.append(f"Your wedding on {wds} is getting closer!")
    elif days:
        ctx.use("t.days_to_wedding")
        lead.append(f"{fmt_int(days)} days to your wedding!")
    if step:
        ctx.use("t.next_step_window_open")
        lead.append(f"After your bridal trial" + (f" on {ctx.derive('d.trial_done', tc, fmt_day_month(tc), ('t.trial_completed',))}" if tc else "") +
                    f", the {step} is the next step, and this is the right window to begin.")
    pref = ctx.t("cu.pref_slots")
    cta = f"Reply YES and we'll hold a {pref} slot for your first session." if pref else "Reply YES and we'll hold a slot for your first session."
    return Draft(lead, cta, "binary_yes_no", "bridal follow-up timed to the wedding countdown", ctx.used,
                 action_on_yes="hold the first skin-prep session", template_params=[ctx.t("cu.name"), ctx.biz, str(days or "")],
                 lever="timing + personal milestone")


def _offer_tie(ctx: Ctx, item: dict) -> str:
    """Tie a category item to this merchant through a matching live offer (or its absence)."""
    words = [w for w in re.findall(r"[a-z]{5,}", str(item.get("title", "")).lower())
             if w not in {"searches", "metros", "india", "launches", "consultations", "gaining", "share", "alternatives"}]
    for fid, t in ctx.active_offers():
        if any(w.rstrip("s") in t.lower() for w in words):
            ctx.use(fid)
            return f"Useful context for your '{t}' offer."
    return ""


def _top_trend(ctx: Ctx):
    best = None
    for f in ctx.L.prefix("c.trend."):
        d = num(as_dict(f.value).get("delta_yoy")) or 0
        if best is None or d > (num(as_dict(best.value).get("delta_yoy")) or 0):
            best = f
    return best


# ---------------------------------------------------------------------------- registry

MERCHANT_PLAYBOOKS: dict[str, Callable[[Ctx], Draft | None]] = {
    "research_digest": pb_research,
    "category_research_digest_release": pb_research,
    "category_trend_movement": pb_research,
    "regulation_change": pb_regulation,
    "cde_opportunity": pb_cde,
    "supply_alert": pb_supply_alert,
    "perf_dip": pb_perf_dip,
    "perf_check": pb_perf_check,
    "seasonal_perf_dip": pb_seasonal_dip,
    "perf_spike": pb_perf_spike,
    "milestone_reached": pb_milestone,
    "competitor_opened": pb_competitor,
    "festival_upcoming": pb_festival,
    "ipl_match_today": pb_ipl,
    "review_theme_emerged": pb_review_theme,
    "curious_ask_due": pb_curious,
    "scheduled_recurring": pb_curious,
    "active_planning_intent": pb_planning,
    "renewal_due": pb_renewal,
    "winback_eligible": pb_winback,
    "dormant_with_vera": pb_dormant,
    "gbp_unverified": pb_gbp_unverified,
    "category_seasonal": pb_category_seasonal,
}

CUSTOMER_PLAYBOOKS: dict[str, Callable[[Ctx], Draft | None]] = {
    "recall_due": cpb_recall,
    "appointment_tomorrow": cpb_appointment,
    "chronic_refill_due": cpb_refill,
    "customer_lapsed_soft": cpb_lapsed,
    "customer_lapsed_hard": cpb_lapsed,
    "customer_checkin": cpb_checkin,
    "trial_followup": cpb_trial,
    "wedding_package_followup": cpb_wedding,
}


FINALS = [  # (keyword in action, final_en, final_hi)
    ("checklist", "send the checklist over", "checklist bhej dungi"),
    ("customer note", "send it to the affected customers", "affected customers ko bhej dungi"),
    ("patient", "send it to your patient list", "patient list ko bhej dungi"),
    ("win-back", "send it to them", "unhe bhej dungi"),
    ("retention", "send it to your members", "members ko bhej dungi"),
    ("review-request", "send it to this week's regulars", "is hafte ke regulars ko bhej dungi"),
    ("review reply", "post it under the reviews", "reviews ke neeche post kar dungi"),
    ("renewal", "process the renewal", "renewal process kar dungi"),
    ("full plan", "switch the plan", "plan switch kar dungi"),
    ("verification", "start the verification", "verification start kar dungi"),
    ("restart", "restart the profile upkeep", "profile upkeep restart kar dungi"),
    ("registration", "block the slot", "slot block kar dungi"),
    ("live", "put it live", "live kar dungi"),
    ("post", "publish the post", "post publish kar dungi"),
]


def _finals(action: str) -> tuple[str, str]:
    low = action.lower()
    for kw, en, hi in FINALS:
        if kw in low:
            return en, hi
    return "go ahead with it", "aage badha dungi"


def apply_history(ctx: Ctx, d: Draft) -> Draft:
    """Continue the thread instead of re-pitching.
    - The merchant already said yes to this exact thing -> deliver it now, ask only for CONFIRM.
    - The merchant has an open, related ask (e.g. 'focus on whitening and aligners') -> fold it into the action."""
    h = ctx.hist
    if not h.get("accepted"):
        return d
    overlap = content_words(h["accepted_vera"]) & (content_words(" ".join(d.lead)) | content_words(d.action_on_yes))
    if len(overlap) >= 2 and d.yes_artifact and d.cta_type != "binary_confirm_cancel":
        ctx.L.add("m.history.accepted", "MERCHANT", "conversation_history", h["merchant_reply"], h["merchant_reply"])
        ctx.use("m.history.accepted")
        first = d.lead[0]
        d.lead = [first, ctx.hl("You already said yes to this, so here it is:", "Aapne haan kaha tha, toh yeh raha:"), d.yes_artifact]
        d.optional = []
        d.cta = ctx.hl("Reply CONFIRM and I'll send it out, or send edits.", "CONFIRM reply kijiye, main bhej dungi, ya edits bhej dijiye.")
        d.cta_type = "binary_confirm_cancel"
        d.action_on_yes = "send the drafted note once confirmed"
        d.angle += "; merchant had already accepted, so delivered instead of re-asking"
        return d
    focus = pending_focus(ctx)
    if focus and "post" in h["accepted_vera"].lower() and "post" in d.action_on_yes.lower():
        ctx.L.add("m.history.pending", "MERCHANT", "conversation_history", focus, focus)
        ctx.use("m.history.pending")
        line = ctx.hl(f"I'll draft it together with the {focus} posts you asked for.",
                      f"Aapke maange hue {focus.replace(' and ', ' aur ')} posts bhi saath mein ready kar dungi.")
        d.lead.append(line)
        d.angle += f"; folded in the merchant's pending ask ({focus})"
    return d


def run_playbook(ctx: Ctx) -> tuple[Draft | None, str]:
    kind = ctx.a.effective_kind
    if ctx.a.audience == "customer":
        fn = CUSTOMER_PLAYBOOKS.get(kind, cpb_lapsed)
        name = f"merchant_{kind}_v1"
    else:
        fn = MERCHANT_PLAYBOOKS.get(kind, pb_generic)
        name = f"vera_{kind}_v1"
    d = fn(ctx)
    if d is not None and ctx.a.audience == "merchant":
        d = apply_history(ctx, d)
    if d is not None:
        d.action_hi = d.action_hi or ctx.last_hi_action
        if not (d.final_en and d.final_hi):
            en, hi = _finals(d.action_on_yes)
            d.final_en, d.final_hi = d.final_en or en, d.final_hi or hi
        if not d.explain:
            facts = [ctx.L.get(f) for f in d.used if ctx.L.get(f) and ctx.L.get(f).text]
            facts.sort(key=lambda f: (f.role == "CATEGORY", not re.search(r"\d", f.text)))
            ev = [f.text for f in facts if re.search(r"\d", f.text)][:2]
            if ev:
                d.explain = "It's based on your own numbers: " + "; ".join(ev) + "."
    return d, name
