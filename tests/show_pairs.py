"""Print the composed message (or abstention) for every canonical test pair. Dev aid."""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from app.compose import build_candidate
from app.store import Store
from tests.dataset import load_all, load_into


def main(only: set[str] | None = None) -> None:
    d = load_all()
    s = Store()
    load_into(s, d)
    now = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)
    for p in d["pairs"]:
        if only and p["test_id"] not in only:
            continue
        t = d["trigger"][p["trigger_id"]]
        c = build_candidate(s, t, now)
        if c.ok:
            print(f"{p['test_id']} [{c.assessment.effective_kind}] ({len(c.body)}c)\n{c.body}\n   cta={c.draft.cta_type} "
                  f"soft={[str(x) for x in c.problems]}\n")
        else:
            print(f"{p['test_id']} [{t['kind']}] ABSTAIN {c.abstain_reason} {[str(x) for x in c.problems]}\n")


if __name__ == "__main__":
    main(set(sys.argv[1:]) or None)
