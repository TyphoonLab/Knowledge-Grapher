"""Desktop-integration behaviour: user config, notifications, theming, the long-running server."""

import os
import threading
import urllib.request

from corpus_graph import serve
from corpus_graph.util import Workspace, notify, user_config


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read().decode("utf-8")


def test_config_file_supplies_workspace(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    (cfg / "corpus-graph").mkdir(parents=True)
    (cfg / "corpus-graph" / "config").write_text(
        '# comment\nCORPUS_GRAPH_ROOT="$HOME/ws"\nexport CORPUS_GRAPH_PORT=9999\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CORPUS_GRAPH_ROOT", raising=False)
    assert user_config() == {"CORPUS_GRAPH_ROOT": f"{tmp_path}/ws", "CORPUS_GRAPH_PORT": "9999"}
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert Workspace.from_args().root == (tmp_path / "ws").resolve()
    # a directory that is itself a workspace wins over the config file
    # ...but not a source checkout that only ships canon/ (or a stray work/)
    (elsewhere / "canon").mkdir()
    (elsewhere / "work").mkdir()
    assert Workspace.from_args().root == (tmp_path / "ws").resolve()
    (elsewhere / "input").mkdir()
    assert Workspace.from_args().root == elsewhere.resolve()
    # and $CORPUS_GRAPH_ROOT / --root win over both
    monkeypatch.setenv("CORPUS_GRAPH_ROOT", str(tmp_path / "env"))
    assert Workspace.from_args().root == (tmp_path / "env").resolve()
    assert Workspace.from_args(root=str(tmp_path / "flag")).root == (tmp_path / "flag").resolve()


def test_notify_without_daemon_is_harmless(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert notify("x", "y") is False


def test_theme_inlined_and_served(sample_ws, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "corpus-graph").mkdir()
    (tmp_path / "corpus-graph" / "theme.css").write_text(":root:root:root:root { --accent: #ebbcba; --community-1: #ebbcba; }")
    page = serve.write_viewer(sample_ws).read_text(encoding="utf-8")
    assert '<style id="user-theme">:root:root:root:root { --accent: #ebbcba;' in page
    httpd = serve.make_server(sample_ws, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert "--accent: #ebbcba" in _get(base + "/theme.css")
        h = next(iter(d["hash"] for d in __import__("json").loads(sample_ws.graph_path.read_text())["documents"]))
        assert "--accent: #ebbcba" in _get(f"{base}/source/{h[:12]}?line=1")
    finally:
        httpd.shutdown()


def test_server_placeholder_then_viewer(tmp_path, monkeypatch, sample_ws):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = Workspace.from_args(root=str(tmp_path / "ws"))
    ws.out.mkdir(parents=True)
    httpd = serve.make_server(ws, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}/"
    try:
        assert "No graph yet" in _get(base)
        ws.graph_path.write_bytes(sample_ws.graph_path.read_bytes())  # a build lands
        assert 'id="graph-data"' in _get(base)
        first = (ws.out / "index.html").stat().st_mtime
        os.utime(ws.graph_path, (first + 10, first + 10))  # a later rebuild
        _get(base)
        assert (ws.out / "index.html").stat().st_mtime > first
    finally:
        httpd.shutdown()


def test_serve_reports_busy_port(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = Workspace.from_args(root=str(tmp_path / "ws"))
    ws.out.mkdir(parents=True)
    busy = serve.make_server(ws, 0)
    try:
        port = busy.server_address[1]
        assert serve.serve(ws, port, open_browser=False) == 1
        err = capsys.readouterr().err
        assert f"port {port} is already in use" in err and "--port 0" in err
    finally:
        busy.server_close()


def test_ctrl_c_exits_cleanly(tmp_path, monkeypatch, capsys):
    from corpus_graph import cli, ingest

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(ingest, "run", interrupted)
    assert cli.main(["--root", str(tmp_path), "ingest"]) == 130
    assert "re-run the same command" in capsys.readouterr().err


def test_billing_override(monkeypatch):
    from corpus_graph.llm import billing_override

    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
        monkeypatch.delenv(var, raising=False)
    assert billing_override() is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert billing_override() == "ANTHROPIC_API_KEY"
