"""Prepare the real setup template with synthetic API data, without running setup."""

import base64
import json
from pathlib import Path

from varyz.setup_web import _fonts, render_page

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp" / "analytics-preview"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "setup.html").write_bytes(render_page("synthetic-consent-preview"))
    (OUT / "fonts.json").write_text(
        json.dumps({name: str(path) for name, path in _fonts().items()}), encoding="utf-8"
    )
    # A standalone version lets the owner click through without installing anything.
    html = render_page("synthetic-consent-preview").decode()
    for name, path in _fonts().items():
        mime = "font/woff2" if name.endswith(".woff2") else "font/ttf"
        encoded = base64.b64encode(path.read_bytes()).decode()
        html = html.replace(f"/fonts/{name}", f"data:{mime};base64,{encoded}")
    preview = """<script>
const previewPlan = {config: 'Preview archive', analytics: {sharing: false},
  ai: {selected: 'conversations', cloud_conversations: true, choices: ['conversations']},
  agents: [{id: 'codex', name: 'Codex', installed: true}],
  sources: [{id: 'ai_conversations', name: 'AI chats', checked: true, lines: []}]};
window.fetch = async (url) => {
  const path = String(url).split('?')[0];
  const body = path === '/api/plan' ? previewPlan : path === '/api/state'
    ? {started: false} : path === '/api/run'
    ? {error: 'Preview only. Your choice has not been saved and setup has not started.'}
    : {ok: true};
  return new Response(JSON.stringify(body), {status: path === '/api/run' ? 409 : 200,
    headers: {'Content-Type': 'application/json'}});
};
</script>"""
    (OUT / "preview.html").write_text(html.replace("<script>", preview + "<script>", 1),
                                      encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()
