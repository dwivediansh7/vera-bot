"""Human-facing landing page: README.md rendered as a styled HTML page with a live status badge."""

from __future__ import annotations

import html
from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vera: Grounded Merchant Assistant</title>
<style>
  :root {{ --bg:#f7f8fa; --card:#ffffff; --text:#1c2230; --muted:#5b6475; --line:#e3e7ee; --accent:#4f46e5;
          --accent-soft:#eef0ff; --ok:#16a34a; --code:#f1f3f7; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#0f1218; --card:#171b23; --text:#e7eaf0; --muted:#9aa3b2; --line:#2a303b; --accent:#8b86ff;
            --accent-soft:#232642; --ok:#22c55e; --code:#1f2430; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
         font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,Helvetica,Arial,sans-serif; }}
  .wrap {{ max-width:920px; margin:0 auto; padding:40px 20px 64px; }}
  .hero {{ background:var(--card); border:1px solid var(--line); border-radius:16px; padding:28px 28px 22px; margin-bottom:22px; }}
  .hero h1 {{ margin:0 0 6px; font-size:30px; letter-spacing:-.02em; }}
  .hero p {{ margin:0; color:var(--muted); }}
  .badges {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:16px; }}
  .badge {{ font-size:13px; padding:5px 11px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-weight:600; }}
  .badge.ok {{ background:rgba(22,163,74,.12); color:var(--ok); }}
  .dot {{ display:inline-block; width:8px; height:8px; border-radius:50%; background:currentColor; margin-right:6px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:16px; padding:8px 28px 22px; }}
  h2 {{ font-size:20px; margin:28px 0 10px; padding-top:6px; border-top:1px solid var(--line); }}
  .card > h2:first-of-type {{ border-top:none; }}
  table {{ width:100%; border-collapse:collapse; margin:10px 0 6px; font-size:14.5px; }}
  th, td {{ text-align:left; padding:9px 10px; border-bottom:1px solid var(--line); vertical-align:top; }}
  th {{ color:var(--muted); font-weight:600; font-size:13px; text-transform:uppercase; letter-spacing:.03em; }}
  code {{ background:var(--code); padding:2px 6px; border-radius:6px; font-size:13.5px;
         font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }}
  pre {{ background:var(--code); padding:14px 16px; border-radius:10px; overflow-x:auto; }}
  pre code {{ background:none; padding:0; }}
  ol li {{ margin:6px 0; }}
  a {{ color:var(--accent); }}
  a.badge {{ text-decoration:none; }}
  .foot {{ color:var(--muted); font-size:13px; text-align:center; margin-top:18px; }}
  @media (max-width:600px) {{ .hero, .card {{ padding-left:16px; padding-right:16px; }} table {{ font-size:13px; }} }}
</style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <h1>{title}</h1>
    <p>{subtitle}</p>
    <div class="badges">
      <span class="badge ok" id="status"><span class="dot"></span>Checking status…</span>
      <a class="badge" href="/demo">Try the live demo</a>
      <a class="badge" href="https://github.com/dwivediansh7/vera-bot" target="_blank" rel="noopener">Source code on GitHub</a>
      <span class="badge">API: <code>/v1/healthz</code> · <code>/v1/metadata</code></span>
    </div>
  </div>
  <div class="card">{body}</div>
  <div class="foot">Vera is an API service. The judge talks to it through the <code>/v1/*</code> endpoints listed above.</div>
</div>
<script>
fetch('/v1/healthz').then(r => r.json()).then(d => {{
  const el = document.getElementById('status');
  el.innerHTML = '<span class="dot"></span>Live · status ' + d.status + ' · up ' + Math.round(d.uptime_seconds / 60) + ' min';
}}).catch(() => {{ const el = document.getElementById('status'); el.className = 'badge'; el.textContent = 'Status unavailable'; }});
</script>
</body>
</html>"""


def render_page() -> str:
    text = README.read_text(encoding="utf-8")
    lines = text.splitlines()
    title = "Vera"
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        lines = lines[1:]
    # the short "Team / Member / Contact" block becomes the subtitle
    meta, rest, i = [], [], 0
    while i < len(lines) and (not lines[i].strip() or ":" in lines[i]) and not lines[i].startswith("#"):
        if lines[i].strip():
            meta.append(lines[i].strip())
        i += 1
    rest = lines[i:]
    try:
        import markdown
        body = markdown.markdown("\n".join(rest), extensions=["tables", "fenced_code", "sane_lists"])
    except Exception:  # never fail the page: fall back to escaped preformatted text
        body = f"<pre>{html.escape(chr(10).join(rest))}</pre>"
    return PAGE.format(title=html.escape(title), subtitle=html.escape("  ·  ".join(meta)), body=body)
