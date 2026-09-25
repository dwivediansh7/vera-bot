"""Brief §7.4 optional interface: respond(state, merchant_message) -> next action.

`state` is a dict: {"conversation_id", "merchant_id", "customer_id"?, "from_role"?}. It reuses the live
reply engine (app/replies.py) and the process-wide store, so multi-turn memory works across calls.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.replies import handle_reply
from app.store import store


def respond(state: dict, merchant_message: str) -> dict:
    req = {
        "conversation_id": state.get("conversation_id") or "conv_offline",
        "merchant_id": state.get("merchant_id"),
        "customer_id": state.get("customer_id"),
        "from_role": state.get("from_role", "merchant"),
        "message": merchant_message,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    return handle_reply(store, req, datetime.now(timezone.utc))
