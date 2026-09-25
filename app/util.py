"""Small, dependency-free helpers: safe access, formatting, dates, hashing."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from typing import Any, Iterable

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH_IDX = {m.lower(): i + 1 for i, m in enumerate(MONTHS)}
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def g(obj: Any, *path: Any, default: Any = None) -> Any:
    """Safe nested get over dicts/lists. Never raises."""
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and isinstance(key, int) and -len(cur) <= key < len(cur):
            cur = cur[key]
        else:
            return default
        if cur is None:
            return default
    return cur


def as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, (tuple, set)):
        return list(v)
    return [v]


def as_dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def num(v: Any) -> float | None:
    """Coerce to a finite float if it is a real number (bool, NaN and inf excluded)."""
    if isinstance(v, bool) or v is None:
        return None
    out: float | None = None
    if isinstance(v, (int, float)):
        out = float(v)
    elif isinstance(v, str):
        s = v.strip().replace(",", "").replace("₹", "")
        try:
            out = float(s)
        except ValueError:
            return None
    if out is None or out != out or out in (float("inf"), float("-inf")):
        return None
    return out


# ---------------------------------------------------------------- formatting

def fmt_int(n: float | int) -> str:
    """Indian digit grouping: 2100 -> 2,100 ; 123456 -> 1,23,456."""
    n = int(round(float(n)))
    sign = "-" if n < 0 else ""
    s = str(abs(n))
    if len(s) <= 3:
        return sign + s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return sign + ",".join(parts + [tail])


def fmt_rupee(n: float | int) -> str:
    return "₹" + fmt_int(n)


def fmt_pct(frac: float, signed: bool = False) -> str:
    """0.18 -> '18%'; 0.005 -> '0.5%'. signed adds +/-."""
    p = float(frac) * 100.0
    mag = abs(p)
    txt = f"{mag:.1f}".rstrip("0").rstrip(".") if mag < 1 else str(int(round(mag)))
    if signed:
        return ("+" if p >= 0 else "-") + txt + "%"
    return ("-" if p < 0 else "") + txt + "%"


def fmt_ctr(ctr: float) -> str:
    """CTR fraction -> '2.1%'."""
    p = float(ctr) * 100.0
    return f"{p:.1f}".rstrip("0").rstrip(".") + "%"


def humanize(slug: Any) -> str:
    """'6_month_cleaning' -> '6-month cleaning'; 'kids_yoga_summer_camp' -> 'kids yoga summer camp'."""
    s = str(slug or "").strip()
    s = re.sub(r"(\d)_(month|week|day|year|mo|min)", r"\1-\2", s)
    s = s.replace("_", " ").strip()
    return re.sub(r"\s+", " ", s)


# ---------------------------------------------------------------- dates

def parse_dt(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v.strip():
        return None
    s = v.strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt
    except ValueError:
        pass
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return None


def parse_date(v: Any) -> date | None:
    dt = parse_dt(v)
    return dt.date() if dt else None


def fmt_day_month(d: date | datetime) -> str:
    """'12 Nov'."""
    return f"{d.day} {MONTHS[d.month - 1]}"


def fmt_dow_day_month(d: date | datetime) -> str:
    """'Sat, 2 May'."""
    return f"{DAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


def fmt_time(dt: datetime) -> str:
    """'7:30pm' / '7pm'."""
    h, m = dt.hour, dt.minute
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d}{suffix}" if m else f"{h12}{suffix}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def months_in_range(spec: Any) -> set[int]:
    """'Nov-Feb' -> {11,12,1,2}; 'Jan' -> {1}; 'Feb 14' -> {2}. Unknown -> empty."""
    s = str(spec or "").strip().lower()
    found = re.findall(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", s)
    if not found:
        return set()
    if len(found) == 1:
        return {_MONTH_IDX[found[0]]}
    a, b = _MONTH_IDX[found[0]], _MONTH_IDX[found[1]]
    out, m = set(), a
    for _ in range(12):
        out.add(m)
        if m == b:
            break
        m = m % 12 + 1
    return out


# ---------------------------------------------------------------- text

def stable_hash(obj: Any, n: int = 12) -> str:
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:n]


def pick(options: list, *seed_parts: Any) -> Any:
    """Deterministic choice among style variants."""
    if not options:
        return None
    h = int(stable_hash(list(seed_parts), 8), 16)
    return options[h % len(options)]


def norm_text(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^\w\s₹%]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def join_human(items: Iterable[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def short_id(mid: str) -> str:
    """'m_001_drmeera_dentist_delhi' -> 'm001_drmeera'."""
    parts = str(mid or "x").split("_")
    if len(parts) >= 3 and parts[0] == "m":
        return f"m{parts[1]}_{parts[2]}"[:24]
    return re.sub(r"[^a-zA-Z0-9]", "", str(mid))[:24] or "x"


def clean_space(s: str) -> str:
    s = re.sub(r"[ \t]+", " ", s or "")
    s = re.sub(r" +([,.;:?!])", r"\1", s)
    return s.strip()
