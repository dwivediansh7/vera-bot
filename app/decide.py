"""SELECT: validate a trigger against the evidence, detect contradictions, check consent, score it.

Deterministic. The LLM never decides whether to send, whom to send to, or whether a trigger is true.
Output is an Assessment; abstention is a first-class outcome with an explicit reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .evidence import resolve_digest_item
from .util import as_dict, as_list, g, num, parse_dt

CUSTOMER_KINDS = {
    "recall_due", "appointment_tomorrow", "chronic_refill_due", "customer_lapsed_soft",
    "customer_lapsed_hard", "trial_followup", "wedding_package_followup",
}

# kinds that only make sense for some verticals
KIND_CATEGORY_FIT = {
    "chronic_refill_due": {"pharmacies"},
    "wedding_package_followup": {"salons"},
    "ipl_match_today": {"restaurants"},
    "supply_alert": {"pharmacies", "dentists"},
    "appointment_tomorrow": {"dentists", "salons", "gyms", "restaurants"},
    "trial_followup": {"gyms", "salons", "dentists"},
}

# consent scopes that cover each customer-facing purpose
PURPOSE_SCOPES = {
    "recall_due": {"recall_reminders", "appointment_reminders", "treatment_followup"},
    "appointment_tomorrow": {"appointment_reminders", "recall_reminders", "program_updates", "kids_program_updates"},
    "chronic_refill_due": {"refill_reminders", "delivery_notifications"},
    "trial_followup": {"program_updates", "kids_program_updates", "appointment_reminders", "promotional_offers"},
    "wedding_package_followup": {"bridal_package_followup", "appointment_reminders", "promotional_offers"},
    "customer_lapsed_soft": {"winback_offers", "promotional_offers", "recall_reminders", "renewal_reminders"},
    "customer_lapsed_hard": {"winback_offers", "promotional_offers", "recall_reminders", "renewal_reminders"},
}
REMINDER_PURPOSES = {"recall_due", "appointment_tomorrow", "chronic_refill_due", "trial_followup"}

ACTION_KINDS = {"supply_alert", "regulation_change", "active_planning_intent"}
DIGEST_KINDS = {"research_digest", "regulation_change", "cde_opportunity", "supply_alert"}


@dataclass
class Assessment:
    trigger_id: str
    kind: str                      # declared kind
    effective_kind: str            # kind we will actually write for (after honest reframes)
    audience: str                  # merchant | customer
    ok: bool = True
    abstain_reason: str = ""
    retry_later: bool = False      # e.g. customer context not pushed yet -> do not burn suppression
    placeholder: bool = False
    contradiction: str = ""        # description when trigger wording disagrees with data
    reframed: bool = False
    digest_item: dict | None = None
    notes: list[str] = field(default_factory=list)
    score: float = 0.0
    score_parts: dict = field(default_factory=dict)
    expired: bool = False
    metric: str = ""
    metric_delta: float | None = None

    def abstain(self, reason: str, retry_later: bool = False) -> "Assessment":
        self.ok, self.abstain_reason, self.retry_later = False, reason, retry_later
        return self


def is_placeholder(payload: Any) -> bool:
    p = as_dict(payload)
    if not p:
        return True
    return bool(p.get("placeholder")) or set(p.keys()) <= {"placeholder", "metric_or_topic"}


def _delta(merchant: dict, metric: str) -> float | None:
    return num(g(merchant, "performance", "delta_7d", f"{metric}_pct"))


def assess(trigger: dict, merchant: dict | None, category: dict | None, customer: dict | None,
           now: datetime | None, sent_kinds_for_merchant: set[str], suppressed_keys: set[str],
           merchant_blocked: bool, customer_blocked: bool) -> Assessment:
    kind = str(trigger.get("kind") or "unknown").strip() or "unknown"
    tid = str(trigger.get("id") or "")
    payload = as_dict(trigger.get("payload"))
    audience = "customer" if (trigger.get("scope") == "customer" or trigger.get("customer_id")
                              or kind in CUSTOMER_KINDS) else "merchant"
    a = Assessment(tid, kind, kind, audience, placeholder=is_placeholder(payload))

    # ---------- resolvability (never guess missing context)
    if not merchant:
        return a.abstain("merchant_context_missing", retry_later=True)
    if not category:
        return a.abstain("category_context_missing", retry_later=True)
    if merchant_blocked:
        return a.abstain("merchant_opted_out")
    key = str(trigger.get("suppression_key") or "")
    if key and key in suppressed_keys:
        return a.abstain("suppression_key_already_used")
    slug = str(category.get("slug") or merchant.get("category_slug") or "")

    if audience == "customer":
        if not customer:
            return a.abstain("customer_context_missing", retry_later=True)
        if customer_blocked:
            return a.abstain("customer_opted_out")
        if customer.get("merchant_id") and merchant.get("merchant_id") and customer["merchant_id"] != merchant["merchant_id"]:
            return a.abstain("customer_belongs_to_other_merchant")

    # ---------- category fit + honest reframes
    fit = KIND_CATEGORY_FIT.get(kind)
    if fit is not None and slug not in fit:
        if kind in ("recall_due", "appointment_tomorrow", "trial_followup") and audience == "customer" \
                and str(customer.get("state")) in ("lapsed_soft", "lapsed_hard", "churned"):
            a.effective_kind, a.reframed = "customer_lapsed_soft", True
            a.contradiction = f"{kind} does not fit a {slug} business; reframed as a return-visit note for a lapsed customer"
        else:
            return a.abstain(f"kind_category_mismatch:{kind}->{slug}")
    has_slots = bool(as_list(payload.get("available_slots")) or payload.get("service_due"))
    if kind == "recall_due" and slug != "dentists" and not a.reframed and not has_slots:
        if audience == "customer" and str(customer.get("state")) in ("lapsed_soft", "lapsed_hard", "churned"):
            a.effective_kind, a.reframed = "customer_lapsed_soft", True
            a.contradiction = f"'recall' is a clinical term; for a {slug} business this is a lapsed-customer return note"
        else:
            return a.abstain(f"kind_category_mismatch:recall_due->{slug}")
    rel = as_list(payload.get("category_relevance"))
    if rel and slug not in rel:
        return a.abstain(f"trigger_not_relevant_to_category:{slug}")

    # ---------- consent (customer-facing)
    if audience == "customer":
        reason = consent_problem(customer, a.effective_kind)
        if reason:
            return a.abstain(reason)
        if a.effective_kind in ("customer_lapsed_soft", "customer_lapsed_hard") and str(customer.get("state")) == "active" \
                and not a.reframed:
            a.contradiction = "trigger says lapsed but customer state is active; writing a neutral check-in, no lapse language"
            a.effective_kind, a.reframed = "customer_checkin", True

    # ---------- digest pointer resolution
    if kind in DIGEST_KINDS or payload.get("top_item_id") or payload.get("digest_item_id") or payload.get("alert_id"):
        a.digest_item = resolve_digest_item(category, trigger)
        if kind in ("regulation_change", "cde_opportunity") and not a.digest_item and a.placeholder:
            return a.abstain("digest_item_unresolved")
        if kind == "supply_alert" and not a.digest_item and not payload.get("molecule"):
            return a.abstain("alert_details_missing")

    # ---------- merchant-data contradictions
    if a.effective_kind in ("perf_dip", "seasonal_perf_dip"):
        _check_dip(a, merchant, payload)
    elif a.effective_kind == "perf_spike":
        _check_spike(a, merchant, payload)
    elif kind == "gbp_unverified" and g(merchant, "identity", "verified") is True:
        return a.abstain("contradiction:gbp_already_verified")
    elif kind == "winback_eligible" and str(g(merchant, "subscription", "status")) == "active":
        return a.abstain("contradiction:subscription_is_active")
    elif kind == "renewal_due" and str(g(merchant, "subscription", "status")) == "expired":
        a.effective_kind, a.reframed = "winback_eligible", True
        a.contradiction = "renewal_due but subscription already expired; reframed as reactivation"
    elif kind == "milestone_reached" and not a.placeholder:
        vn, mv = num(payload.get("value_now")), num(payload.get("milestone_value"))
        if vn is None or mv is None:
            a.placeholder = True
    if not a.ok:
        return a

    # ---------- expiry: tie-break only (simulator clock can run ahead of the dataset's dates)
    exp = parse_dt(trigger.get("expires_at"))
    if exp and now and exp.tzinfo and now.tzinfo and exp < now:
        a.expired = True

    _score(a, trigger, sent_kinds_for_merchant)
    return a


def consent_problem(customer: dict, purpose: str) -> str:
    ident = as_dict(customer.get("identity"))
    prefs = as_dict(customer.get("preferences"))
    consent = as_dict(customer.get("consent"))
    if "phone_redacted" in ident and ident.get("phone_redacted") in (None, ""):
        return "no_reachable_phone"
    if str(prefs.get("channel") or "").startswith("none"):
        return "no_reachable_channel"
    scopes = {str(s) for s in as_list(consent.get("scope"))}
    if not consent.get("opted_in_at") or not scopes:
        return "no_customer_consent"
    allowed = PURPOSE_SCOPES.get(purpose, {"promotional_offers"})
    if scopes & allowed:
        return ""
    # generic reminder opt-in covers reminder-type purposes only
    if purpose in REMINDER_PURPOSES and prefs.get("reminder_opt_in") is True:
        return ""
    if purpose == "customer_checkin" and ("promotional_offers" in scopes or "seasonal_health_content" in scopes):
        return ""
    return f"consent_scope_mismatch:{purpose}"


def _check_dip(a: Assessment, merchant: dict, payload: dict) -> None:
    metric = str(payload.get("metric") or "").lower()
    pd = num(payload.get("delta_pct"))
    if metric in ("views", "calls", "ctr") and not a.placeholder:
        md = _delta(merchant, metric)
        if pd is not None and pd < 0 and (md is None or md < 0):
            a.metric, a.metric_delta = metric, pd
            return
        if pd is not None and pd < 0 and md is not None and md >= 0:
            a.contradiction = f"trigger reports {metric} {pd:+.0%} but current snapshot shows {md:+.0%}"
    # placeholder or contradicted: use what the snapshot actually shows
    deltas = {m: _delta(merchant, m) for m in ("calls", "views")}
    neg = {m: d for m, d in deltas.items() if d is not None and d <= -0.05}
    if neg:
        m = min(neg, key=neg.get)
        a.metric, a.metric_delta = m, neg[m]
        if a.contradiction:
            a.reframed = True
        return
    a.contradiction = a.contradiction or "dip trigger but no metric is down in the current snapshot"
    a.effective_kind, a.reframed = "perf_check", True


def _check_spike(a: Assessment, merchant: dict, payload: dict) -> None:
    metric = str(payload.get("metric") or "").lower()
    pd = num(payload.get("delta_pct"))
    if metric in ("views", "calls", "ctr") and not a.placeholder:
        md = _delta(merchant, metric)
        if pd is not None and pd > 0 and (md is None or md > 0):
            a.metric, a.metric_delta = metric, pd
            return
        if md is not None and md <= 0:
            a.contradiction = f"trigger reports {metric} up but current snapshot shows {md:+.0%}"
    deltas = {m: _delta(merchant, m) for m in ("calls", "views")}
    pos = {m: d for m, d in deltas.items() if d is not None and d >= 0.03}
    if pos:
        m = max(pos, key=pos.get)
        a.metric, a.metric_delta = m, pos[m]
        if a.contradiction:
            a.reframed = True
        return
    a.abstain("contradiction:no_metric_is_up")


def _score(a: Assessment, trigger: dict, sent_kinds: set[str]) -> None:
    parts: dict[str, float] = {}
    urg = num(trigger.get("urgency"))
    parts["urgency"] = max(1.0, min(5.0, urg if urg is not None else 2.0))
    parts["evidence"] = 0.75 if a.placeholder else 1.5
    if a.digest_item:
        parts["digest"] = 0.5
    if a.kind in ACTION_KINDS:
        parts["action_kind"] = 1.0
    if a.reframed:
        parts["reframe_penalty"] = -1.0
    if a.effective_kind in sent_kinds:
        parts["novelty_penalty"] = -1.5
    if a.expired:
        parts["expired_penalty"] = -0.5
    a.score_parts = parts
    a.score = round(sum(parts.values()), 3)
