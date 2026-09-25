"""Write submission.jsonl (brief §7.2): one line per canonical test pair, via bot.compose().

    python scripts/make_submission.py [--now 2026-04-26T10:30:00Z]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import compose  # noqa: E402
from tests.dataset import load_all  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", default="2026-04-26T10:30:00Z", help="reference time (the dataset's week by default)")
    ap.add_argument("--out", default=str(ROOT / "submission.jsonl"))
    args = ap.parse_args()
    now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
    d = load_all()
    n = 0
    with open(args.out, "w", encoding="utf-8") as f:
        for p in d["pairs"]:
            trig = d["trigger"][p["trigger_id"]]
            mer = d["merchant"][p["merchant_id"]]
            cat = d["category"][mer["category_slug"]]
            cus = d["customer"].get(p["customer_id"]) if p.get("customer_id") else None
            out = compose(cat, mer, trig, cus, now=now)
            f.write(json.dumps({"test_id": p["test_id"], **out}, ensure_ascii=False) + "\n")
            n += 1
    print(f"wrote {n} lines -> {args.out}")


if __name__ == "__main__":
    main()
