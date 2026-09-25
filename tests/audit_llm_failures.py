"""8.6: LLM failure modes -> template fallback, valid output, within time. Spawns separate bot instances."""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.dataset import load_all  # noqa: E402


class H429(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        self.send_response(429)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":{"code":429,"message":"quota"}}')

    def log_message(self, *a):
        pass


class HHang(H429):
    def do_POST(self):
        time.sleep(30)


def serve(handler, port):
    s = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def run_case(name, env, port):
    e = {**os.environ, "PYTHONUTF8": "1", "VERA_DEBUG": "1", **env}
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "error"],
                         cwd=ROOT, env=e, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    C = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=35)
    try:
        for _ in range(40):
            try:
                if C.get("/v1/healthz").status_code == 200:
                    break
            except Exception:
                time.sleep(0.3)
        d = load_all()
        for s in ("category", "merchant", "customer", "trigger"):
            for cid, pl in d[s].items():
                C.post("/v1/context", json={"scope": s, "context_id": cid, "version": 1, "payload": pl})
        t0 = time.time()
        acts = C.post("/v1/tick", json={"now": "2026-09-26T10:00:00Z", "available_triggers": [
            "trg_023_competitor_opened_dentist", "trg_010_ipl_match_delhi"]}).json()["actions"]
        tick_ms = (time.time() - t0) * 1000
        t0 = time.time()
        r = C.post("/v1/reply", json={"conversation_id": acts[0]["conversation_id"], "merchant_id": acts[0]["merchant_id"],
                                      "from_role": "merchant", "message": "why should I not match the price, tell me more", "turn_number": 2}).json()
        reply_ms = (time.time() - t0) * 1000
        dec = C.get("/v1/debug/decisions", params={"limit": 20}).json()
        h = C.get("/v1/healthz").json()
        ok = len(acts) == 2 and all(a["body"].strip() for a in acts) and r.get("action") in ("send", "wait", "end") and tick_ms < 8000 and reply_ms < 8000
        reasons = sorted({f["reason"][:60] for f in dec["llm_fallbacks"]})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: llm_enabled={h['llm']['enabled']} tick {tick_ms:.0f}ms ({len(acts)} valid actions), "
              f"reply {reply_ms:.0f}ms ({r.get('action')}); fallback reasons={reasons or 'n/a (LLM disabled)'}")
    finally:
        p.terminate()
        try:
            _, err = p.communicate(timeout=10)
        except Exception:
            p.kill()
            err = b""
        key = (open(ROOT / ".env", encoding="utf-8").read().split("LLM_API_KEY=")[1].split()[0] if (ROOT / ".env").exists() else "")
        if key and key in (err or b"").decode("utf-8", "ignore"):
            print("   !! KEY APPEARED IN STDERR")


if __name__ == "__main__":
    serve(H429, 8097)
    serve(HHang, 8098)
    run_case("no key", {"LLM_API_KEY": "", "GEMINI_API_KEY": "", "VERA_LLM_MODE": "polish"}, 8091)
    run_case("bad key", {"LLM_API_KEY": "invalid-key-for-audit"}, 8092)
    run_case("429 from provider", {"LLM_BASE_URL": "http://127.0.0.1:8097"}, 8093)
    run_case("provider hangs (timeout 5s)", {"LLM_BASE_URL": "http://127.0.0.1:8098"}, 8094)
