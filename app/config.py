"""Runtime configuration. Everything comes from environment variables; nothing secret is hard-coded."""

from __future__ import annotations

import os
from pathlib import Path

try:  # .env is optional; real environment variables always win
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


class Settings:
    def __init__(self) -> None:
        # --- identity (/v1/metadata) ---
        self.team_name = _env("TEAM_NAME", "Ansh Dwivedi")
        self.team_members = [m.strip() for m in _env("TEAM_MEMBERS", "Ansh Dwivedi").split(",") if m.strip()]
        self.contact_email = _env("CONTACT_EMAIL", "dwivediansh0@gmail.com")
        self.submitted_at = _env("SUBMITTED_AT", "2026-09-26T00:00:00Z")

        # --- LLM (optional; any OpenAI-compatible endpoint: Groq, Gemini, OpenRouter, OpenAI) ---
        self.llm_api_key = _env("LLM_API_KEY") or _env("GROQ_API_KEY") or _env("GEMINI_API_KEY") or _env("OPENAI_API_KEY")
        self.llm_provider = _env("LLM_PROVIDER", self._guess_provider())
        self.llm_base_url = _env("LLM_BASE_URL", self._default_base_url(self.llm_provider))
        self.llm_model = _env("LLM_MODEL", self._default_model(self.llm_provider))
        # off  -> templates only; polish -> LLM rewrites the validated template draft
        self.llm_mode = _env("VERA_LLM_MODE", "polish" if self.llm_api_key else "off")
        self.llm_timeout_s = _env_float("LLM_TIMEOUT_S", 5.0)
        # Outbound (tick) messages are template-only by default; the LLM words /v1/reply turns only.
        self.llm_outbound = _env("LLM_OUTBOUND", "off").lower() in ("on", "1", "true", "yes")
        # Persisted LLM response cache -> identical outputs across restarts ("" disables persistence)
        self.llm_cache_path = _env("LLM_CACHE_PATH", str(Path(__file__).resolve().parents[1] / "llm_cache.json"))
        self.llm_rpm = _env_int("LLM_RPM", 25)               # stay under free-tier per-minute caps
        self.llm_concurrency = _env_int("LLM_CONCURRENCY", 4)

        # --- latency budgets (judge hard limit is 30 s) ---
        self.tick_budget_s = _env_float("TICK_BUDGET_S", 12.0)
        self.reply_budget_s = _env_float("REPLY_BUDGET_S", 8.0)

        # --- policy ---
        self.max_actions_per_tick = 20
        self.max_payload_bytes = 500 * 1024
        self.send_threshold = _env_float("SEND_THRESHOLD", 1.0)
        self.debug_endpoints = _env("VERA_DEBUG", "0") == "1"

    def _guess_provider(self) -> str:
        if _env("GROQ_API_KEY"):
            return "groq"
        if _env("GEMINI_API_KEY"):
            return "gemini"
        if _env("OPENAI_API_KEY"):
            return "openai"
        return "groq"

    @staticmethod
    def _default_base_url(provider: str) -> str:
        return {
            "groq": "https://api.groq.com/openai/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
            "openrouter": "https://openrouter.ai/api/v1",
            "openai": "https://api.openai.com/v1",
        }.get(provider, "https://api.groq.com/openai/v1")

    @staticmethod
    def _default_model(provider: str) -> str:
        return {
            "groq": "llama-3.3-70b-versatile",
            "gemini": "gemini-3.5-flash-lite",
            "openrouter": "meta-llama/llama-3.3-70b-instruct:free",
            "openai": "gpt-4o-mini",
        }.get(provider, "llama-3.3-70b-versatile")

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key) and self.llm_mode != "off"


settings = Settings()
