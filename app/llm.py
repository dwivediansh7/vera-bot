"""Minimal async client for any OpenAI-compatible chat endpoint (Groq, Gemini, OpenRouter, OpenAI).

Guards: hard timeout, per-minute rate budget (free tiers), circuit breaker, response cache.
Returns None on any failure — callers always have a deterministic fallback.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque

import httpx

from .config import settings
from .util import stable_hash


class LLMClient:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._calls: deque[float] = deque()
        self._sem = asyncio.Semaphore(max(1, settings.llm_concurrency))
        self._fail_streak = 0
        self._open_until = 0.0
        self._cache: dict[str, dict] = self._load_cache()
        self.stats = {"calls": 0, "ok": 0, "errors": 0, "timeouts": 0, "rate_limited": 0, "cache_hits": 0,
                      "last_ok": None, "last_error": None, "cache_entries": len(self._cache)}

    # ---- persisted cache: same prompt -> same wording, even after a restart
    @staticmethod
    def _load_cache() -> dict[str, dict]:
        path = settings.llm_cache_path
        if not path:
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_cache(self) -> None:
        path = settings.llm_cache_path
        if not path:
            return
        try:
            if len(self._cache) > 5000:  # keep the file small
                for k in list(self._cache)[: len(self._cache) - 5000]:
                    self._cache.pop(k, None)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False)
            import os
            os.replace(tmp, path)
            self.stats["cache_entries"] = len(self._cache)
        except Exception:
            pass  # a read-only disk must never break replies

    @property
    def enabled(self) -> bool:
        return settings.llm_enabled

    def status(self) -> dict:
        return {"enabled": self.enabled, "provider": settings.llm_provider if self.enabled else None,
                "model": settings.llm_model if self.enabled else None,
                "circuit_open": time.time() < self._open_until, **self.stats}

    def _budget_ok(self) -> bool:
        now = time.time()
        while self._calls and now - self._calls[0] > 60:
            self._calls.popleft()
        return len(self._calls) < settings.llm_rpm

    async def complete_json(self, system: str, user: str, timeout: float | None = None, max_tokens: int = 700) -> dict | None:
        if not self.enabled:
            return None
        key = stable_hash([settings.llm_model, system, user], 24)
        if key in self._cache:
            self.stats["cache_hits"] += 1
            return self._cache[key]
        if time.time() < self._open_until:
            return None
        if not self._budget_ok():
            self.stats["rate_limited"] += 1
            return None
        self._calls.append(time.time())
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=settings.llm_base_url.rstrip("/"))
        body = {"model": settings.llm_model, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_object"}}
        headers = {"Authorization": f"Bearer {settings.llm_api_key}", "Content-Type": "application/json"}
        t = timeout or settings.llm_timeout_s
        self.stats["calls"] += 1
        try:
            async with self._sem:
                r = await asyncio.wait_for(self._client.post("/chat/completions", json=body, headers=headers), timeout=t)
            if r.status_code == 400 and "response_format" in r.text:
                body.pop("response_format")
                async with self._sem:
                    r = await asyncio.wait_for(self._client.post("/chat/completions", json=body, headers=headers), timeout=t)
            if r.status_code == 429:
                self.stats["http_429"] = self.stats.get("http_429", 0) + 1
                raise RuntimeError("http 429 (rate limited)")
            if r.status_code != 200:
                raise RuntimeError(f"http {r.status_code}")
            text = r.json()["choices"][0]["message"]["content"]
            data = parse_json(text)
            if not isinstance(data, dict):
                raise ValueError("non-json output")
            self._cache[key] = data
            self._save_cache()
            self._fail_streak = 0
            self.stats["ok"] += 1
            self.stats["last_ok"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            return data
        except asyncio.TimeoutError:
            self.stats["timeouts"] += 1
            self._trip("timeout")
        except Exception as e:  # never propagate: deterministic fallback always exists
            self.stats["errors"] += 1
            self._trip(type(e).__name__ + ":" + str(e)[:80])
        return None

    def _trip(self, why: str) -> None:
        self.stats["last_error"] = why
        self._fail_streak += 1
        if self._fail_streak >= 3:
            self._open_until = time.time() + 60
            self._fail_streak = 0


def parse_json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", text or "")
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


llm = LLMClient()
