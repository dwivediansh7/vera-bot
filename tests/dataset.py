"""Load the expanded dataset (UTF-8) for tests and harnesses."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPANDED = ROOT / "expanded"


def ensure_expanded() -> Path:
    if not (EXPANDED / "test_pairs.json").exists():
        subprocess.run([sys.executable, str(ROOT / "dataset" / "generate_dataset.py"), "--seed-dir",
                        str(ROOT / "dataset"), "--out", str(EXPANDED)], check=True,
                       env={**__import__("os").environ, "PYTHONUTF8": "1"})
    return EXPANDED


def _load_dir(d: Path) -> dict[str, dict]:
    out = {}
    for f in sorted(d.glob("*.json")):
        out[f.stem] = json.loads(f.read_text(encoding="utf-8"))
    return out


def load_all() -> dict:
    base = ensure_expanded()
    cats = {v["slug"]: v for v in _load_dir(base / "categories").values()}
    merchants = {v["merchant_id"]: v for v in _load_dir(base / "merchants").values()}
    customers = {v["customer_id"]: v for v in _load_dir(base / "customers").values()}
    triggers = {v["id"]: v for v in _load_dir(base / "triggers").values()}
    pairs = json.loads((base / "test_pairs.json").read_text(encoding="utf-8"))["pairs"]
    return {"category": cats, "merchant": merchants, "customer": customers, "trigger": triggers, "pairs": pairs}


def load_into(store, data: dict, include_triggers: bool = True) -> None:
    for scope in ("category", "merchant", "customer") + (("trigger",) if include_triggers else ()):
        for cid, payload in data[scope].items():
            store.put_context(scope, cid, 1, payload)
