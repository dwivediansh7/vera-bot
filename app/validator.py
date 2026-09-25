"""VALIDATE: claim-level checks on every outbound body (template or LLM).

Hard problems block the message; soft problems are logged. The validator never loosens itself to
let a message through — the composer must produce a compliant body, fall back, or abstain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .evidence import Ledger, numeric_tokens
from .util import norm_text

URL_RE = re.compile(r"(https?://|www\.|\b[\w-]+\.(com|in|org|net|io|ly|co|app|me|info)\b(/\S*)?)", re.I)
SNAKE_RE = re.compile(r"\b[a-z]+_[a-z0-9_]+\b")
JARGON = [
    "trigger", "payload", "suppression", "placeholder", "merchant_id", "customer_id", "context id",
    "urgency", "category_slug", "json", "llm", "as an ai", "language model", "template", "digest item",
    "customer_aggregate", "peer_stats", "delta_7d", "signal:", "signals", "rationale", "fact sheet",
    "evidence ledger", "decision card",
]
GLOBAL_TABOO = ["guaranteed", "100% safe", "miracle", "best in city", "completely cure", "instant results"]
PREAMBLE_RE = re.compile(
    r"(hope (you|this)( a|')re|hope this (message )?finds|i am reaching out|i'm reaching out|this is vera|"
    r"i am vera|i'm vera|my name is vera|vera here|trust you are)", re.I)
CAPS_OK = {
    "YES", "STOP", "CONFIRM", "CTR", "GBP", "IDA", "JIDA", "DCI", "ORS", "CDE", "RVG", "IOPA", "OPG", "CDSCO",
    "DGCI", "FDA", "ICMR", "HIIT", "BOGO", "IPL", "AOV", "SOP", "SOPS", "OTC", "YOY", "RCT", "CAD", "CAM", "PFM",
    "EMOM", "AMRAP", "BMR", "GST", "FSSAI", "NO", "PT", "DC", "MI", "SR", "LDL", "PCR", "MRP", "RX", "UPI",
}
HYPE = ["!!!", "amazing deal", "hurry", "limited time only", "act now", "don't miss out", "once in a lifetime"]
QUALIFYING = ["would you", "do you", "can you tell", "what if", "how about"]

# the published case-study bodies: we must never be near-copies (similarity check penalises)
CASE_STUDY_BODIES = [
    "JIDA's Oct issue landed. One item relevant to your high-risk adult patients — 2,100-patient trial showed 3-month fluoride recall cuts caries recurrence 38% better than 6-month. Worth a look (2-min abstract). Want me to pull it + draft a patient-ed WhatsApp you can share?",
    "It's been 5 months since your last visit — your 6-month cleaning recall is due. Apke liye 2 slots ready hain: Wed 5 Nov, 6pm ya Thu 6 Nov, 5pm. ₹299 cleaning + complimentary fluoride. Reply 1 for Wed, 2 for Thu, or tell us a time that works.",
    "196 days to your wedding — perfect window to start the 30-day skin-prep program before serious bridal bookings roll in. ₹2,499 covers 4 sessions + a take-home kit. Want me to block your preferred Saturday 4pm slot for the first session next week?",
    "Quick check — what service has been most asked-for this week at Studio11? I'll turn the answer into a Google post + a 4-line WhatsApp reply you can use when customers ask about pricing. Takes 5 min.",
    "DC vs MI at Arun Jaitley tonight, 7:30pm. Important: Saturday IPL matches usually shift -12% restaurant covers (people watch at home). Skip the match-night promo today; instead push your BOGO pizza (already active) as a delivery-only Saturday special. Want me to draft the Swiggy banner + an Insta story? Live in 10 min.",
    "here's a starter version — you can edit: Mylari Corporate Thali — for offices in Indiranagar - 10 thalis @ ₹125 each (₹25 off retail) + free delivery - 25 thalis @ ₹115 each + 2 free filter coffees",
    "your views are down 30% this week — but I want to flag this is the normal April-June acquisition lull (every metro gym sees -25 to -35% in this window). Action: skip ad spend now, save it for Sept-Oct when conversion is 2x.",
    "It's been about 8 weeks — happens to most members at some point, no judgment. We've added a Tue/Thu evening HIIT class that fits weight-loss goals well (45 min, 6:30pm). Want me to hold a free trial spot for you next Tue, 30 Apr? Reply YES — no commitment, no auto-charge.",
    "urgent: voluntary recall on 2 atorvastatin batches (AT2024-1102, AT2024-1108) by Mfr Z — sub-potency, no safety risk, but customers should be informed for replacement. Pulled your repeat-Rx list: 22 of your chronic-Rx customers were dispensed these batches in last 90 days.",
    "Sharma ji ki 3 monthly medicines (metformin, atorvastatin, telmisartan) 28 April ko khatam hongi. Same dose, same brand pack ready hai. Senior discount 15% applied — total ₹1,420 (₹240 saved). Free home delivery to saved address by 5pm tomorrow.",
]


@dataclass
class Problem:
    code: str
    hard: bool
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.code}{'!' if self.hard else ''}:{self.detail}" if self.detail else self.code


def _shingles(text: str, n: int = 5) -> set[tuple]:
    toks = norm_text(text).split()
    return {tuple(toks[i:i + n]) for i in range(max(0, len(toks) - n + 1))}


_CASE_SH = [_shingles(b) for b in CASE_STUDY_BODIES]


def case_similarity(body: str) -> float:
    sh = _shingles(body)
    if not sh:
        return 0.0
    return max((len(sh & c) / max(1, min(len(sh), len(c))) for c in _CASE_SH), default=0.0)


def _pct_tokens(text: str) -> set[str]:
    t = re.sub(r"(?<=\d),(?=\d)", "", text)
    return {m.group(1).rstrip("0").rstrip(".") if "." in m.group(1) else m.group(1)
            for m in re.finditer(r"(\d+(?:\.\d+)?)\s?%", t)}


def _price_tokens(text: str) -> set[str]:
    t = re.sub(r"(?<=\d),(?=\d)", "", text)
    return set(re.findall(r"₹\s?(\d+(?:\.\d+)?)", t))


def ledger_texts(L: Ledger, ids: list[str] | None = None) -> list[str]:
    out = []
    for k, f in L.facts.items():
        if ids is None or k in ids:
            out.append(f.text)
            out.extend(str(a) for a in f.alt)
    return out


def last_sentence(body: str) -> str:
    parts = [p for p in re.split(r"(?<=[.!?])\s+|\n+", body.strip()) if p.strip()]
    return parts[-1] if parts else ""


def validate(body: str, *, L: Ledger, audience: str, send_as: str, cta_type: str, taboo: list[str],
             expected_names: list[str], prior_bodies: set[str], allowed_ids: list[str] | None = None,
             required_ending: str | None = None, customer_lang: str | None = None,
             forbid_qualifying: bool = False) -> list[Problem]:
    P: list[Problem] = []
    b = (body or "").strip()
    if not b:
        return [Problem("empty_body", True)]
    low = b.lower()

    # --- length
    if len(b) > 1100:
        P.append(Problem("too_long", True, str(len(b))))
    elif len(b) > 750:
        P.append(Problem("long", False, str(len(b))))

    # --- URLs (penalty -3 each)
    if URL_RE.search(b):
        P.append(Problem("url_in_body", True, URL_RE.search(b).group(0)))

    # --- numbers / prices / percentages must be provable
    texts = ledger_texts(L, allowed_ids)
    allowed_nums: set[str] = set()
    for t in texts:
        allowed_nums |= numeric_tokens(t)
    bad = sorted(numeric_tokens(b) - allowed_nums)
    if bad:
        P.append(Problem("unsupported_number", True, ",".join(bad)))
    allowed_pct = set().union(*(_pct_tokens(t) for t in texts)) if texts else set()
    badp = sorted(_pct_tokens(b) - allowed_pct)
    if badp:
        P.append(Problem("unsupported_percentage", True, ",".join(badp)))
    allowed_price = set().union(*(_price_tokens(t) for t in texts)) if texts else set()
    badr = sorted(_price_tokens(b) - allowed_price)
    if badr:
        P.append(Problem("unsupported_price", True, ",".join(badr)))

    # --- jargon / taboo / hype
    for j in JARGON:
        if j in low:
            P.append(Problem("internal_jargon", True, j))
            break
    snake = [s for s in SNAKE_RE.findall(b)]
    if snake:
        P.append(Problem("internal_jargon", True, snake[0]))
    for t in list(taboo) + GLOBAL_TABOO:
        t0 = re.sub(r"\(.*?\)", "", str(t)).strip().lower()
        if t0 and re.search(r"(?<![a-z])" + re.escape(t0) + r"(?![a-z])", low):
            P.append(Problem("taboo_word", True, t0))
            break
    for h in HYPE:
        if h in low:
            P.append(Problem("hype", True, h))
            break
    fact_caps = set(re.findall(r"\b[A-Z]{2,}\b", " ".join(texts)))
    caps = [w for w in re.findall(r"\b[A-Z]{4,}\b", b) if w not in CAPS_OK and w not in fact_caps]
    if caps:
        P.append(Problem("shouting", True, caps[0]))

    # --- misattribution: category / metro facts must never be phrased as the merchant's own ("your ...")
    if re.search(r"\byour\b[^.?!]{0,40}\bsearches\b|\byour\b[^.?!]{0,25}\b(year-on-year|yoy|across metros)\b", low):
        P.append(Problem("misattribution", True, "category trend phrased as the merchant's own"))
    for f in L.facts.values():
        if f.role == "CATEGORY" and f.text and len(f.text) > 12:
            i = low.find(f.text.lower())
            if i > 0 and re.search(r"\byour\s*$", low[max(0, i - 12):i]):
                P.append(Problem("misattribution", True, f.text[:40]))
                break

    # --- preamble / self-introduction
    if PREAMBLE_RE.search(b):
        P.append(Problem("preamble_or_reintroduction", True, PREAMBLE_RE.search(b).group(0)))

    # --- personalisation
    names = [n for n in expected_names if n]
    if names and not any(n.lower() in low[:140] for n in names):
        P.append(Problem("missing_name", True, names[0]))

    # --- CTA: exactly one, in the final sentence (quoted drafts for the merchant's customers don't count)
    unq = re.sub(r'"[^"]*"', " ", b)
    ls = last_sentence(b)
    if cta_type != "none":
        cta_like = re.search(r"\?|\breply\b|\bconfirm\b|\byes\b|kijiye|bata", ls, re.I)
        if not cta_like:
            P.append(Problem("cta_not_last", True, ls[:60]))
    directives = re.findall(r"\breply\s+(?:with\s+)?(?:yes|no|stop|confirm|cancel|\d)\b|"
                            r"\b(?:yes|confirm|\d(?: ya \d)?)\s+reply\b", unq, re.I)
    if len(directives) > 1:
        P.append(Problem("multiple_ctas", True, str(len(directives))))
    if unq.count("?") > 2:
        P.append(Problem("too_many_questions", True, str(unq.count("?"))))
    if required_ending and not b.rstrip().endswith(required_ending.strip()):
        P.append(Problem("cta_changed", True))

    # --- audience consistency
    if audience == "customer" and send_as != "merchant_on_behalf":
        P.append(Problem("send_as_mismatch", True, send_as))
    if audience == "merchant" and send_as != "vera":
        P.append(Problem("send_as_mismatch", True, send_as))

    # --- repetition / plagiarism
    if norm_text(b) in prior_bodies:
        P.append(Problem("duplicate_body", True))
    sim = case_similarity(b)
    if sim >= 0.30:
        P.append(Problem("case_study_similarity", True, f"{sim:.2f}"))

    # --- language (customer preference must be honoured)
    if customer_lang == "hi" and not re.search(r"\b(hai|hain|kijiye|aap|ji|ko|ki|ke|se)\b", low):
        P.append(Problem("language_mismatch", True, "expected Hindi"))
    if forbid_qualifying and any(q in low for q in QUALIFYING):
        P.append(Problem("qualifying_after_commitment", True))
    return P


def hard(problems: list[Problem]) -> list[Problem]:
    return [p for p in problems if p.hard]
