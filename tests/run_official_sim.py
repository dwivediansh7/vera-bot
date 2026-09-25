"""Run the OFFICIAL judge_simulator.py scenarios against a running bot.

Usage:
  python -m tests.run_official_sim [scenario]            # scenario: all | warmup | auto_reply_hell | intent_transition | hostile
  LLM_PROVIDER=groq LLM_API_KEY=... python -m tests.run_official_sim phase2_short   # LLM-scored scenarios need a real key

Without a key, a stub provider is used; it is only valid for scenarios that do not score messages
(all / warmup / auto_reply_hell / intent_transition / hostile). Scores are never faked.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import judge_simulator as js  # noqa: E402

NON_SCORING = {"all", "warmup", "auto_reply_hell", "intent_transition", "hostile"}


class StubLLM(js.LLMProvider):
    def complete(self, prompt: str, system: str = None) -> str:
        raise RuntimeError("stub provider: no LLM scoring available")

    def name(self) -> str:
        return "stub (no scoring)"


def main() -> int:
    scenario = sys.argv[1] if len(sys.argv) > 1 else "all"
    # judge_simulator.py itself reads BOT_URL / LLM_PROVIDER / LLM_API_KEY / LLM_MODEL from env or .env
    if js.LLM_API_KEY:
        llm = js.create_provider()
        pace = float(os.environ.get("JUDGE_PACE_S", "0"))
        if pace > 0:   # free tiers: space judge calls out so none are scored by the simulator's crude fallback
            import time
            inner = llm.complete

            def paced(prompt, system=None):
                time.sleep(pace)
                for attempt in range(3):
                    try:
                        return inner(prompt, system)
                    except Exception as e:
                        if "429" not in str(e) or attempt == 2:
                            raise
                        time.sleep(20 * (attempt + 1))
            llm.complete = paced
    else:
        if scenario not in NON_SCORING:
            print(f"Scenario '{scenario}' needs LLM_API_KEY (it scores messages with an LLM). Refusing to fake scores.")
            return 2
        llm = StubLLM()
    judge = js.JudgeSimulator(llm)
    ok = judge.run(scenario)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
