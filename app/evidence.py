"""Evidence ledger: raw context -> verified, provenance-tagged facts.

Nothing reaches a message unless it exists here. Every fact carries:
  role     MERCHANT | CATEGORY | DERIVED | CUSTOMER | TRIGGER
  path     where it came from in the pushed context
  text     the exact human rendering messages may use
  numbers  numeric tokens this fact licenses (validator whitelist)
Derived values (ratios, deltas, proposals) are computed here in code, never by the LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .util import (
    as_dict, as_list, fmt_ctr, fmt_day_month, fmt_dow_day_month, fmt_int, fmt_pct, fmt_rupee,
    fmt_time, g, humanize, months_in_range, num, parse_date, parse_dt,
)

MERCHANT, CATEGORY, DERIVED, CUSTOMER, TRIGGER = "MERCHANT", "CATEGORY", "DERIVED", "CUSTOMER", "TRIGGER"

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def numeric_tokens(text: str) -> set[str]:
    """'₹1,499 and 2.1% on 12 Nov' -> {'1499','2.1','12'} (thousand separators removed)."""
    t = re.sub(r"(?<=\d),(?=\d)", "", str(text or ""))
    out = set()
    for tok in _NUM_RE.findall(t):
        out.add(tok)
        if "." in tok:
            out.add(tok.rstrip("0").rstrip("."))
    return out


@dataclass
class Fact:
    id: str
    role: str
    path: str
    value: Any
    text: str
    confidence: float = 1.0
    merchant_specific: bool = False
    derived_from: tuple = ()
    alt: tuple = ()           # other licensed renderings (e.g. raw numbers)
    numbers: set = field(default_factory=set)

    def __post_init__(self) -> None:
        self.numbers = numeric_tokens(self.text)
        for a in self.alt:
            self.numbers |= numeric_tokens(a)

    def brief(self) -> dict:
        return {"id": self.id, "role": self.role, "path": self.path, "text": self.text}


class Ledger:
    def __init__(self) -> None:
        self.facts: dict[str, Fact] = {}

    def add(self, fid: str, role: str, path: str, value: Any, text: str, **kw: Any) -> Fact:
        f = Fact(fid, role, path, value, str(text), merchant_specific=role in (MERCHANT, CUSTOMER), **kw)
        self.facts[fid] = f
        return f

    def get(self, fid: str) -> Fact | None:
        return self.facts.get(fid)

    def has(self, fid: str) -> bool:
        return fid in self.facts

    def text(self, fid: str, default: str = "") -> str:
        f = self.facts.get(fid)
        return f.text if f else default

    def val(self, fid: str, default: Any = None) -> Any:
        f = self.facts.get(fid)
        return f.value if f else default

    def prefix(self, pre: str) -> list[Fact]:
        return [f for k, f in self.facts.items() if k.startswith(pre)]

    def allowed_numbers(self, ids: list[str] | None = None) -> set[str]:
        out: set[str] = set()
        for k, f in self.facts.items():
            if ids is None or k in ids:
                out |= f.numbers
        return out

    def derive(self, fid: str, value: Any, text: str, sources: tuple = (), alt: tuple = ()) -> Fact:
        return self.add(fid, DERIVED, "computed", value, text, derived_from=sources, alt=alt)


# ------------------------------------------------------------------ labels

AUDIENCE_WORD = {
    "dentists": "patients", "pharmacies": "customers", "gyms": "members",
    "salons": "clients", "restaurants": "customers",
}

AGG_LABELS = {
    "total_unique_ytd": ("count", "{n} unique {aud} this year"),
    "lapsed_180d_plus": ("count", "{n} {aud} who haven't visited in 180+ days"),
    "lapsed_90d_plus": ("count", "{n} {aud} who haven't visited in 90+ days"),
    "retention_6mo_pct": ("pct", "{p} 6-month retention"),
    "retention_3mo_pct": ("pct", "{p} 3-month retention"),
    "high_risk_adult_count": ("count", "{n} high-risk adult patients"),
    "repeat_customer_pct": ("pct", "{p} repeat customers"),
    "delivery_share_pct": ("pct", "{p} of orders via delivery"),
    "chronic_rx_count": ("count", "{n} chronic-Rx customers"),
    "total_active_members": ("count", "{n} active members"),
    "monthly_churn_pct": ("pct", "{p} monthly churn"),
    "trial_to_paid_pct": ("pct", "{p} trial-to-paid conversion"),
    "delivery_orders_30d": ("count", "{n} delivery orders in the last 30 days"),
    "dine_in_orders_30d": ("count", "{n} dine-in orders in the last 30 days"),
}

PEER_LABELS = {
    "avg_rating": "{v}★ average rating",
    "avg_review_count": "{n} reviews on average",
    "avg_reviews": "{n} reviews on average",
    "avg_views_30d": "{n} profile views a month",
    "avg_calls_30d": "{n} calls a month",
    "avg_directions_30d": "{n} direction requests a month",
    "avg_ctr": "{c} CTR",
    "avg_photos": "{n} photos",
    "avg_post_freq_days": "a Google post every {n} days",
}

SIGNAL_TEXT = [
    (re.compile(r"^stale_posts:(\d+)d$"), "last Google post was {0} days ago"),
    (re.compile(r"^renewal_due_soon:(\d+)d$"), "subscription renews in {0} days"),
    (re.compile(r"^dormant_with_vera_(\d+)d$"), "no conversation with Vera in {0} days"),
    (re.compile(r"^ctr_below_peer_median$"), "profile CTR is below the local peer median"),
    (re.compile(r"^unverified_gbp$"), "Google Business Profile is not verified yet"),
    (re.compile(r"^no_active_offers$"), "no active offer on the profile"),
    (re.compile(r"^delivery_not_set_up$"), "home delivery is not set up on the profile"),
    (re.compile(r"^no_recent_post$"), "no recent Google post"),
    (re.compile(r"^high_risk_adult_cohort$"), "a sizeable high-risk adult patient cohort"),
]


def display_name(merchant: dict) -> str:
    v = g(merchant, "identity", "name", default="")
    return v.strip() if isinstance(v, str) else ""


def owner_first(merchant: dict) -> str:
    v = g(merchant, "identity", "owner_first_name", default="")
    return v.strip() if isinstance(v, str) else ""


def salutation(merchant: dict, category_slug: str) -> str:
    """'Dr. Meera' for dentists, 'Lakshmi' otherwise. Never doubles the 'Dr.'."""
    first = owner_first(merchant)
    if not first:
        name = display_name(merchant)
        m = re.match(r"^(Dr\.?\s+\w+)", name)
        if m:
            return m.group(1).replace("Dr ", "Dr. ")
        return f"{name} team" if name else ""   # category voice pattern "{business_name} team"
    if re.match(r"^dr\.?\s", first, re.I):
        rest = re.sub(r"^dr\.?\s*", "", first, flags=re.I)
        return f"Dr. {rest}"
    if category_slug == "dentists":
        return f"Dr. {first}"
    return first


# ------------------------------------------------------------------ digest resolution

def resolve_digest_item(category: dict, trigger: dict) -> dict | None:
    """Follow payload pointers (top_item_id / digest_item_id / alert_id / item_id) into category.digest.
    Falls back to an inline item dict in the payload (brief §4.3 shape)."""
    payload = as_dict(trigger.get("payload"))
    digest = [d for d in as_list(category.get("digest")) if isinstance(d, dict)]
    for key in ("top_item_id", "digest_item_id", "alert_id", "item_id"):
        ref = payload.get(key)
        if ref:
            for d in digest:
                if d.get("id") == ref:
                    return d
    inline = payload.get("top_item") or payload.get("item")
    if isinstance(inline, dict) and inline.get("title"):
        return inline
    return None


# ------------------------------------------------------------------ builder

def build_ledger(category: dict, merchant: dict, trigger: dict, customer: dict | None,
                 digest_item: dict | None, ref_date: date | None) -> Ledger:
    L = Ledger()
    slug = str(category.get("slug") or merchant.get("category_slug") or "")
    aud = AUDIENCE_WORD.get(slug, "customers")

    # ---- merchant identity
    ident = as_dict(merchant.get("identity"))
    if display_name(merchant):
        L.add("m.name", MERCHANT, "identity.name", display_name(merchant), display_name(merchant))
    sal = salutation(merchant, slug)
    if sal:
        L.add("m.salutation", MERCHANT, "identity.owner_first_name", sal, sal)
    if ident.get("locality"):
        L.add("m.locality", MERCHANT, "identity.locality", ident["locality"], str(ident["locality"]))
    if ident.get("city"):
        L.add("m.city", MERCHANT, "identity.city", ident["city"], str(ident["city"]))
    if ident.get("verified") is False:
        L.add("m.unverified", MERCHANT, "identity.verified", False, "Google profile not verified")

    # ---- subscription
    sub = as_dict(merchant.get("subscription"))
    if sub.get("status"):
        L.add("m.sub.status", MERCHANT, "subscription.status", sub["status"], str(sub["status"]))
    if sub.get("plan"):
        L.add("m.sub.plan", MERCHANT, "subscription.plan", sub["plan"], f"{sub['plan']} plan")
    dr = num(sub.get("days_remaining"))
    if dr is not None and dr > 0:
        L.add("m.sub.days_remaining", MERCHANT, "subscription.days_remaining", dr, f"{fmt_int(dr)} days")
    dse = num(sub.get("days_since_expiry"))
    if dse is not None and dse > 0:
        L.add("m.sub.days_since_expiry", MERCHANT, "subscription.days_since_expiry", dse, f"{fmt_int(dse)} days")

    # ---- performance
    perf = as_dict(merchant.get("performance"))
    window = num(perf.get("window_days")) or 30
    L.add("m.perf.window", MERCHANT, "performance.window_days", window, f"{fmt_int(window)} days")
    for key, label in (("views", "profile views"), ("calls", "calls"), ("directions", "direction requests"), ("leads", "leads")):
        v = num(perf.get(key))
        if v is not None:
            L.add(f"m.perf.{key}", MERCHANT, f"performance.{key}", v, f"{fmt_int(v)} {label}")
    ctr = num(perf.get("ctr"))
    if ctr is not None:
        L.add("m.perf.ctr", MERCHANT, "performance.ctr", ctr, fmt_ctr(ctr), alt=(str(ctr),))
    delta = as_dict(perf.get("delta_7d"))
    for key, label in (("views_pct", "views"), ("calls_pct", "calls"), ("ctr_pct", "CTR")):
        v = num(delta.get(key))
        if v is not None:
            L.add(f"m.delta.{label.lower()}", MERCHANT, f"performance.delta_7d.{key}", v,
                  f"{label} {'up' if v >= 0 else 'down'} {fmt_pct(abs(v))} week-on-week")

    # ---- offers (verbatim titles only)
    for i, o in enumerate(as_list(merchant.get("offers"))):
        if not isinstance(o, dict) or not o.get("title"):
            continue
        status = str(o.get("status") or "").lower()
        fid = f"m.offer.{status or 'unknown'}.{i}"
        L.add(fid, MERCHANT, f"offers[{i}]", o, str(o["title"]).strip())

    # ---- customer aggregate (keys differ by category; read what exists)
    agg = as_dict(merchant.get("customer_aggregate"))
    for k, v in agg.items():
        n = num(v)
        if n is None:
            continue
        kind, tmpl = AGG_LABELS.get(k, ("pct" if k.endswith("_pct") else "count",
                                          humanize(k) + (" {p}" if k.endswith("_pct") else ": {n}")))
        if kind == "count" and n <= 0:
            continue
        text = tmpl.format(n=fmt_int(n), p=fmt_pct(n), aud=aud)
        L.add(f"m.agg.{k}", MERCHANT, f"customer_aggregate.{k}", n, text)

    # ---- signals (only known ones get a merchant-readable rendering; raw names never leak)
    for s in as_list(merchant.get("signals")):
        s = str(s)
        for rx, tmpl in SIGNAL_TEXT:
            mm = rx.match(s)
            if mm:
                L.add(f"m.signal.{s.split(':')[0]}", MERCHANT, "signals", s, tmpl.format(*mm.groups()))
                break
        else:
            L.add(f"m.signal_raw.{s}", MERCHANT, "signals", s, "")  # usable for logic, never rendered

    # ---- review themes
    for i, r in enumerate(as_list(merchant.get("review_themes"))):
        if not isinstance(r, dict) or not r.get("theme"):
            continue
        occ = num(r.get("occurrences_30d"))
        txt = humanize(r["theme"])
        if occ:
            txt = f"{fmt_int(occ)} reviews this month mention {txt}"
        L.add(f"m.review.{r['theme']}", MERCHANT, f"review_themes[{i}]", r, txt)

    # ---- conversation history (for continuity; bodies used only when quoting our own past proposal)
    hist = [h for h in as_list(merchant.get("conversation_history")) if isinstance(h, dict)]
    if hist:
        L.add("m.history", MERCHANT, "conversation_history", hist, "")

    # ---- category peers
    peers = as_dict(category.get("peer_stats"))
    for k, v in peers.items():
        n = num(v)
        if n is None or k not in PEER_LABELS:
            continue
        text = PEER_LABELS[k].format(n=fmt_int(n), v=f"{n:g}", c=fmt_ctr(n))
        L.add(f"c.peer.{k}", CATEGORY, f"peer_stats.{k}", n, text, alt=(str(v),))

    for o in as_list(category.get("offer_catalog")):
        if isinstance(o, dict) and o.get("title"):
            L.add(f"c.catalog.{o.get('id', o['title'])}", CATEGORY, "offer_catalog", o, str(o["title"]))

    for i, b in enumerate(as_list(category.get("seasonal_beats"))):
        if isinstance(b, dict) and b.get("note"):
            L.add(f"c.beat.{i}", CATEGORY, f"seasonal_beats[{i}]", b, str(b["note"]),
                  alt=(str(b.get("month_range", "")),))

    for i, t in enumerate(as_list(category.get("trend_signals"))):
        if isinstance(t, dict) and t.get("query") and num(t.get("delta_yoy")) is not None:
            dv = num(t["delta_yoy"])
            L.add(f"c.trend.{i}", CATEGORY, f"trend_signals[{i}]", t,
                  f"searches for '{t['query']}' are {'up' if dv >= 0 else 'down'} {fmt_pct(abs(dv))} year-on-year")

    for c in as_list(category.get("patient_content_library")):
        if isinstance(c, dict) and c.get("title"):
            L.add(f"c.content.{c.get('id', c['title'])}", CATEGORY, "patient_content_library", c, str(c["title"]))

    for d in as_list(category.get("digest")):
        if isinstance(d, dict) and d.get("id") and d.get("title"):
            _add_digest(L, f"c.digest.{d['id']}", d, CATEGORY)

    # ---- trigger payload
    payload = as_dict(trigger.get("payload"))
    for k, v in payload.items():
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float, str)):
            L.add(f"t.{k}", TRIGGER, f"payload.{k}", v, str(v), alt=(str(v),))
    for i, s in enumerate(as_list(payload.get("available_slots")) + as_list(payload.get("next_session_options"))):
        if isinstance(s, dict) and (s.get("label") or s.get("iso")):
            label = s.get("label") or ""
            dt = parse_dt(s.get("iso"))
            if not label and dt:
                label = f"{fmt_dow_day_month(dt)}, {fmt_time(dt)}"
            L.add(f"t.slot.{i}", TRIGGER, "payload.slots", s, str(label), alt=(str(s.get("iso", "")),))
    if digest_item:
        _add_digest(L, "t.item", digest_item, TRIGGER)

    # ---- customer
    if customer:
        cid = as_dict(customer.get("identity"))
        name = str(cid.get("name") or "").strip()
        if name and not name.startswith("("):
            first = re.split(r"\s*\(", name)[0].strip()
            L.add("cu.name", CUSTOMER, "identity.name", first, first)
            parent = re.search(r"parent:\s*([^)]+)\)", name)
            if parent:
                L.add("cu.parent", CUSTOMER, "identity.name", parent.group(1).strip(), parent.group(1).strip())
        rel = as_dict(customer.get("relationship"))
        lv = parse_date(rel.get("last_visit"))
        if lv:
            L.add("cu.last_visit", CUSTOMER, "relationship.last_visit", lv, fmt_day_month(lv),
                  alt=(str(rel.get("last_visit")),))
        vt = num(rel.get("visits_total"))
        if vt:
            L.add("cu.visits", CUSTOMER, "relationship.visits_total", vt, f"{fmt_int(vt)} visits")
        services = [humanize(s) for s in as_list(rel.get("services_received")) if s and s != "..."]
        if services:
            L.add("cu.services", CUSTOMER, "relationship.services_received", services, services[-1])
        for k in ("favourite_dish",):
            if rel.get(k):
                L.add(f"cu.{k}", CUSTOMER, f"relationship.{k}", rel[k], str(rel[k]))
        prefs = as_dict(customer.get("preferences"))
        if prefs.get("preferred_slots"):
            L.add("cu.pref_slots", CUSTOMER, "preferences.preferred_slots", prefs["preferred_slots"],
                  re.sub(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
                         lambda m: m.group(1).capitalize(), humanize(prefs["preferred_slots"])))
        if customer.get("state"):
            L.add("cu.state", CUSTOMER, "state", customer["state"], humanize(customer["state"]))
        if cid.get("senior_citizen"):
            L.add("cu.senior", CUSTOMER, "identity.senior_citizen", True, "senior citizen")

    # ---- derived comparisons (code, not LLM)
    _derive(L, slug, merchant, peers, ref_date)
    return L


def _add_digest(L: Ledger, fid: str, d: dict, role: str) -> None:
    L.add(fid, role, f"digest[{d.get('id', '?')}]", d, str(d.get("title", "")),
          alt=(str(d.get("summary", "")), str(d.get("source", "")), str(d.get("actionable", "")),
               str(d.get("trial_n", "")), str(d.get("credits", "")), str(d.get("date", ""))))


def _derive(L: Ledger, slug: str, merchant: dict, peers: dict, ref_date: date | None) -> None:
    ctr, pctr = L.val("m.perf.ctr"), num(peers.get("avg_ctr"))
    if ctr is not None and pctr:
        rel = "below" if ctr < pctr else "above" if ctr > pctr else "level with"
        L.derive("d.ctr_vs_peer", {"ctr": ctr, "peer": pctr, "rel": rel},
                 f"CTR {fmt_ctr(ctr)} vs {fmt_ctr(pctr)} peer average", ("m.perf.ctr", "c.peer.avg_ctr"))
    for key, pkey, label in (("calls", "avg_calls_30d", "calls"), ("views", "avg_views_30d", "profile views")):
        mv, pv = L.val(f"m.perf.{key}"), num(peers.get(pkey))
        if mv is not None and pv:
            L.derive(f"d.{key}_vs_peer", {"m": mv, "peer": pv, "ratio": mv / pv},
                     f"{fmt_int(mv)} {label} in 30 days vs ~{fmt_int(pv)} for {_peer_phrase(slug)}",
                     (f"m.perf.{key}", f"c.peer.{pkey}"))
    dly, dine = L.val("m.agg.delivery_orders_30d"), L.val("m.agg.dine_in_orders_30d")
    if dly and dine is not None and (dly + dine) > 0:
        share = dly / (dly + dine)
        L.derive("d.delivery_share", share, f"{fmt_pct(share)} of your last-30-day orders were delivery",
                 ("m.agg.delivery_orders_30d", "m.agg.dine_in_orders_30d"))
    # the largest "round" threshold a merchant has crossed (truthful milestone for sparse data)
    for key in ("m.agg.total_unique_ytd", "m.agg.total_active_members", "m.perf.views"):
        v = L.val(key)
        if v and v >= 100:
            step = 100 if v < 1000 else 500 if v < 5000 else 1000
            mark = int(v // step * step)
            if mark >= 100:
                L.derive(f"d.milestone.{key.split('.')[-1]}", {"value": v, "mark": mark},
                         f"past {fmt_int(mark)}", (key,))
    if ref_date:
        L.derive("d.ref_month", ref_date.month, "", ())


def _peer_phrase(slug: str) -> str:
    return {
        "dentists": "similar clinics", "salons": "similar salons", "restaurants": "similar restaurants",
        "gyms": "similar gyms", "pharmacies": "similar pharmacies",
    }.get(slug, "similar businesses")


def beats_for_month(category: dict, month: int) -> list[dict]:
    out = []
    for b in as_list(category.get("seasonal_beats")):
        if isinstance(b, dict) and month in months_in_range(b.get("month_range")):
            out.append(b)
    return out


def beat_index(category: dict, beat: dict) -> int:
    for i, b in enumerate(as_list(category.get("seasonal_beats"))):
        if b is beat or b == beat:
            return i
    return -1
