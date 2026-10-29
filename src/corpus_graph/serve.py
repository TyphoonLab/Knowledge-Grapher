"""Stage 6: write the self-contained viewer (out/index.html) and serve it on localhost.

index.html inlines D3 (vendored, no CDN) and graph.json, so it also works when
opened directly from disk. The local server adds:
  /source/<hash>   the converted Markdown with line numbers, so provenance links jump to the line
  /theme.css       the user theme (~/.config/corpus-graph/theme.css, e.g. rendered by a Noctalia
                   template); the page re-reads it when it regains focus, so palette changes follow
and regenerates index.html whenever graph.json is newer, so a rebuild needs no server restart.
"""

from __future__ import annotations

import html
import json
import re
import threading
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .util import Workspace, config_dir, eprint, read_json, write_text

_lock = threading.Lock()


def theme_path() -> Path:
    return config_dir() / "theme.css"


def user_theme() -> str:
    try:
        css = theme_path().read_text(encoding="utf-8")
    except OSError:
        return ""
    return css.replace("</", "<\\/")  # it is inlined into a <style> element


def write_viewer(ws: Workspace) -> Path:
    graph = read_json(ws.graph_path)
    if graph is None:
        raise SystemExit("serve: out/graph.json not found; run `corpus-graph build` first")
    pkg = resources.files("corpus_graph.viewer")
    template = (pkg / "template.html").read_text(encoding="utf-8")
    d3 = (pkg / "vendor" / "d3.min.js").read_text(encoding="utf-8")
    data = json.dumps(graph, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = template.replace("/*__D3__*/", d3).replace("/*__THEME__*/", user_theme()).replace("/*__GRAPH__*/", data)
    out = ws.out / "index.html"
    write_text(out, page)
    return out


SOURCE_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>
:root{{--bg:#fbfaf7;--ink:#1d1f23;--muted:#8a909b;--hi:#fff2b3;--line:#e3e1db}}
@media (prefers-color-scheme: dark){{:root{{--bg:#15171b;--ink:#e8e6e1;--muted:#6f7682;--hi:#4a4220;--line:#2e323a}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 ui-sans-serif,system-ui,sans-serif}}
header{{padding:10px 16px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg)}}
pre{{margin:0;padding:8px 0;font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;overflow-wrap:anywhere}}
.l{{display:grid;grid-template-columns:4.5em 1fr;padding:0 16px 0 0}}
.l a{{color:var(--muted);text-align:right;padding-right:1em;user-select:none;text-decoration:none}}
.l:target,.l.hi{{background:var(--hi)}}
</style><style>{theme}</style></head><body><header><b>{title}</b> · <span style="color:var(--muted)">{path}</span></header><pre>{lines}</pre>
<script>const h=location.hash||("#L"+(new URLSearchParams(location.search).get("line")||""));const el=document.querySelector(h);if(el){{el.classList.add("hi");el.scrollIntoView({{block:"center"}});}}</script>
</body></html>"""

PLACEHOLDER = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>corpus-graph</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="15">
<style>:root{{--bg:#fbfaf7;--ink:#1d1f23;--muted:#5d6470}}@media (prefers-color-scheme: dark){{:root{{--bg:#15171b;--ink:#e8e6e1;--muted:#9aa1ad}}}}
body{{margin:0;padding:48px 24px;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,sans-serif}}main{{max-width:640px;margin:auto}}
code{{font:13px ui-monospace,monospace}}p{{color:var(--muted)}}</style><style>{theme}</style></head><body><main>
<h1>corpus-graph</h1><p>No graph yet in <code>{root}</code>. Put documents in <code>input/</code> and run</p>
<p><code>corpus-graph run</code></p><p>This page reloads itself and shows the viewer as soon as <code>out/graph.json</code> exists.</p>
</main></body></html>"""


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, ws: Workspace, **kwargs):
        self.ws = ws
        super().__init__(*args, directory=str(ws.out), **kwargs)

    def log_message(self, fmt, *args):  # quiet
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        m = re.match(r"^/source/([0-9a-f]{12,64})$", u.path)
        if m:
            return self._source(m.group(1), parse_qs(u.query).get("line", [""])[0])
        if u.path == "/theme.css":
            return self._send(user_theme().replace("<\\/", "</"), "text/css; charset=utf-8")
        if u.path in ("/", "/index.html"):
            if not self.ws.graph_path.exists():
                return self._send(PLACEHOLDER.format(theme=user_theme(), root=html.escape(str(self.ws.root))),
                                  "text/html; charset=utf-8")
            self._refresh_viewer()
            self.path = "/index.html"
        return super().do_GET()

    def _refresh_viewer(self):
        index = self.ws.out / "index.html"
        with _lock:
            if not index.exists() or index.stat().st_mtime < self.ws.graph_path.stat().st_mtime:
                write_viewer(self.ws)

    def _send(self, text: str, ctype: str):
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _source(self, h: str, line: str):
        cands = list(self.ws.md_dir.glob(f"{h}*.md"))
        if not cands:
            self.send_error(404, "no such document")
            return
        p = cands[0]
        meta = read_json(p.with_suffix(".meta.json"), {}) or {}
        rows = p.read_text(encoding="utf-8").split("\n")
        body = "".join(f'<div class="l" id="L{i}"><a href="#L{i}">{i}</a><span>{html.escape(r) or " "}</span></div>'
                       for i, r in enumerate(rows, 1))
        page = SOURCE_PAGE.format(title=html.escape(meta.get("title") or p.name), path=html.escape(f"work/md/{p.name}"),
                                  lines=body, theme=user_theme())
        self._send(page, "text/html; charset=utf-8")


def make_server(ws: Workspace, port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, ws=ws))


def serve(ws: Workspace, port: int = 8765, open_browser: bool = True) -> int:
    httpd = make_server(ws, port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    eprint(f"serve: {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
