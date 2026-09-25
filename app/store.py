"""In-memory, versioned state. Single process by design (the judge expects state to persist across calls).

All mutations happen synchronously between awaits on one event loop, so they are atomic without locks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .util import iso_now, norm_text

SCOPES = ("category", "merchant", "customer", "trigger")


@dataclass
class Conversation:
    conversation_id: str
    merchant_id: str | None
    customer_id: str | None
    audience: str                      # merchant | customer
    trigger_id: str | None = None
    kind: str = ""
    topic: str = ""                    # short description of what the thread is about
    action_on_yes: str = ""
    action_hi: str = ""
    final_en: str = ""
    final_hi: str = ""
    explain: str = ""
    yes_artifact: str = ""
    lang: str = "en"                   # en | mix | hi
    status: str = "open"               # open | waiting | ended | done
    stage: str = "pitch"               # pitch | action | confirmed
    turns: list[dict] = field(default_factory=list)
    sent_bodies: set[str] = field(default_factory=set)
    inbound_norm: list[str] = field(default_factory=list)
    auto_replies: int = 0
    bot_sends: int = 0
    wait_until: str | None = None
    card: dict = field(default_factory=dict)


class Store:
    def __init__(self) -> None:
        self.started = time.time()
        self.reset()

    def reset(self) -> None:
        self.contexts: dict[tuple[str, str], dict[str, Any]] = {}
        self.conversations: dict[str, Conversation] = {}
        self.used_suppression: set[str] = set()
        self.blocked_merchants: set[str] = set()
        self.blocked_customers: set[str] = set()
        self.merchant_bodies: dict[str, set[str]] = {}     # novelty control across conversations
        self.merchant_kinds: dict[str, set[str]] = {}
        self.merchant_autoreply: dict[str, int] = {}
        self.merchant_last_autoreply: dict[str, str] = {}
        self.merchant_autoreply_at: dict[str, datetime] = {}
        self.merchant_wait_until: dict[str, datetime] = {}
        self.decision_log: list[dict] = []
        self.llm_fallbacks: list[dict] = []
        self.counters: dict[str, int] = {"context": 0, "tick": 0, "reply": 0, "errors": 0, "actions": 0,
                                         "abstentions": 0, "llm_used": 0, "llm_rejected": 0, "fallbacks": 0}
        self.last_ok: dict[str, str] = {}

    # ------------------------------------------------------------- contexts
    def put_context(self, scope: str, cid: str, version: int, payload: dict) -> tuple[bool, int | None]:
        key = (scope, cid)
        cur = self.contexts.get(key)
        if cur is not None and cur["version"] >= version:
            return False, cur["version"]
        self.contexts[key] = {"version": version, "payload": payload, "stored_at": iso_now()}
        return True, None

    def get(self, scope: str, cid: str | None) -> dict | None:
        if not cid:
            return None
        rec = self.contexts.get((scope, str(cid)))
        return rec["payload"] if rec else None

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in SCOPES}
        for (s, _) in self.contexts:
            out[s] = out.get(s, 0) + 1
        return out

    def category_for(self, merchant: dict | None, trigger: dict | None = None) -> dict | None:
        m = merchant if isinstance(merchant, dict) else {}
        p = (trigger or {}).get("payload") if isinstance(trigger, dict) else None
        slug = m.get("category_slug") or (p.get("category") if isinstance(p, dict) else None)
        if not isinstance(slug, str) or not slug:
            return None
        return self.get("category", slug)

    # ------------------------------------------------------------- outbound bookkeeping
    def remember_send(self, conv: Conversation, body: str) -> None:
        n = norm_text(body)
        conv.sent_bodies.add(n)
        conv.bot_sends += 1
        if conv.merchant_id:
            self.merchant_bodies.setdefault(conv.merchant_id, set()).add(n)

    def prior_bodies(self, merchant_id: str | None, conv: Conversation | None = None) -> set[str]:
        out = set(self.merchant_bodies.get(merchant_id or "", set()))
        if conv:
            out |= conv.sent_bodies
        return out


store = Store()
