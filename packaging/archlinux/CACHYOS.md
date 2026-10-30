# corpus-graph on CachyOS (niri + Noctalia), without the AUR

This is a local pacman package built with `makepkg` from this repository. Nothing is taken from the AUR and no AUR helper (paru/yay) is involved.

## Where every piece comes from

| piece | source |
|---|---|
| `uv`, `git` (build only); `gcc-libs`, `curl`, `xdg-utils`, `util-linux` | official repos, via pacman |
| optional: `libnotify`, `tesseract` + `tesseract-data-grc/heb/lat/eng/deu/fra/ita`, `obsidian`, `chromium` | official repos, via pacman |
| Python libraries (PyMuPDF, pymupdf4llm, networkx, igraph, leidenalg, rapidfuzz, PyYAML, …) | PyPI wheels pinned in `uv.lock`, installed with `--require-hashes` into `/opt/corpus-graph/lib` |
| Python itself | a private CPython 3.13 (python-build-standalone, fetched by uv) in `/opt/corpus-graph/python`; or the system Python if you build with `CORPUS_GRAPH_PYTHON=system` |
| Claude Code (the default LLM backend) | Anthropic's native installer into `~/.local/bin`, not the AUR `claude-code` package |

Why not use Arch's own Python packages? Two dependencies cannot come from the official repos: `leidenalg` is AUR-only, and `pymupdf4llm` refuses to import unless PyMuPDF is exactly the version it was released with, which Arch's `python-pymupdf` (1.26.x) is not. Rather than mix system and pip packages, the package carries one consistent, hash-verified set. The bundled interpreter also means a rolling-release Python upgrade (3.13 → 3.14) cannot break the compiled wheels.

## Install

```bash
sudo pacman -S --needed base-devel git uv
```

```bash
git clone https://github.com/TyphoonLab/Knowledge-Grapher.git && cd Knowledge-Grapher/packaging/archlinux
```

```bash
makepkg -si
```

The build downloads CPython and the wheels (about 290 MB, much of it onnxruntime, which pymupdf4llm's layout model needs), runs the sample pipeline as a check, and installs about 425 MB under `/opt/corpus-graph`. The package is built from the checkout's committed `HEAD`; makepkg warns if you have uncommitted changes.

To use the system Python instead (smaller; rebuild after every Python minor upgrade, and a pacman hook reminds you):

```bash
CORPUS_GRAPH_PYTHON=system makepkg -si
```

Optional extras from the official repos:

```bash
sudo pacman -S --needed libnotify tesseract tesseract-data-grc tesseract-data-heb tesseract-data-lat tesseract-data-eng tesseract-data-deu tesseract-data-fra tesseract-data-ita
```

Claude Code, if you do not have it yet (official installer; no Node.js, no AUR):

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

CachyOS's default shell is fish; put `~/.local/bin` on PATH with:

```fish
fish_add_path ~/.local/bin
```

(`corpus-graph` itself adds `~/.local/bin` to PATH when it looks for `claude`, so launching from niri or the systemd unit works either way.)

## Per-user setup

```bash
corpus-graph-setup --all
```

That is idempotent and does, as your user:

- creates the workspace (default `~/Research/corpus-graph`; choose with `--workspace DIR`) with `input/` and seeded `canon/`, and writes `~/.config/corpus-graph/config` (see `/usr/share/corpus-graph/config.example`);
- copies the Claude Code skill into `<workspace>/.claude/skills/`, so `claude` started in the workspace answers from the graph;
- `--niri`: copies `corpus-graph.kdl` to `~/.config/niri/` and appends `include "corpus-graph.kdl"` to `config.kdl` (once, marked, with a backup), then runs `niri validate`;
- `--noctalia`: registers the theme template with Noctalia (v5 `config.toml`, or legacy v4 `user-templates.toml`), appended once and marked;
- `--service`: enables `corpus-graph-serve.service`, a systemd user unit that keeps the viewer on `http://127.0.0.1:8765/`;
- `--sample`: builds the bundled sample in `~/corpus-graph-sample` (recorded responses, no tokens). Browse it with `corpus-graph --root ~/corpus-graph-sample serve --port 8766`; port 8765 is taken by the service, which shows your main workspace.

Drop any flag to do that part by hand; the snippets live in `/usr/share/corpus-graph/`.

## Daily use

```bash
cp ~/Downloads/*.pdf ~/Research/corpus-graph/input/
```

```bash
corpus-graph extract --dry-run
```

```bash
corpus-graph run --notify
```

`corpus-graph` finds the workspace from `~/.config/corpus-graph/config` when you are not inside one, so these work from any directory. `--notify` sends a desktop notification at the end, which Noctalia shows.

- **Launcher:** "Corpus Graph" appears in Noctalia's application launcher (it reads the standard desktop entries). Its actions are *Rebuild graph* (resolve, build, report; no LLM calls), *Open report* and *Export and open Obsidian vault*.
- **niri:** `Mod+Shift+G` opens the viewer, `Mod+Ctrl+Shift+G` rebuilds. The window rule gives the viewer a ¾-width column; edit `~/.config/niri/corpus-graph.kdl`. niri reloads it live.
- **Browser:** the viewer opens in an app window if your default browser is Chromium-based, or in a new window for Firefox-based browsers (Firefox, LibreWolf, Zen, Floorp). Override with `CORPUS_GRAPH_BROWSER="chromium --app=%u"` in the config.
- **Theme:** with the Noctalia template registered, every palette change renders `~/.config/corpus-graph/theme.css`. The viewer (and the source pages behind provenance links) re-reads it whenever the window regains focus, so it follows your wallpaper-derived colours without a reload. Delete that file to return to the built-in light/dark theme.
- **Obsidian:** the vault export is plain Markdown; Noctalia's community `obsidian` template themes Obsidian itself.
- **Claude Code:** start `claude` in the workspace; the skill makes it query the graph and cite quotes. Noctalia's community `claude-code` template themes the CLI.

## Files

| path | what |
|---|---|
| `/usr/bin/corpus-graph` | the CLI (runs the bundled Python with `-s -E -X utf8`) |
| `/usr/bin/corpus-graph-open` | desktop/niri entry point (viewer, report, vault, rebuild) |
| `/usr/bin/corpus-graph-serve`, `/usr/lib/systemd/user/corpus-graph-serve.service` | the viewer service |
| `/usr/bin/corpus-graph-setup` | per-user setup |
| `/usr/share/corpus-graph/niri/corpus-graph.kdl` | niri binds and window rule |
| `/usr/share/corpus-graph/noctalia/` | theme template + v5/v4 registration snippets |
| `/usr/share/corpus-graph/sample/` | sample corpus with recorded responses |
| `/opt/corpus-graph/` | bundled Python and libraries |

## Updating and removing

```bash
cd Knowledge-Grapher && git pull && cd packaging/archlinux && makepkg -si
```

```bash
systemctl --user restart corpus-graph-serve
```

```bash
sudo pacman -R corpus-graph
```

Removal leaves your workspace and `~/.config/corpus-graph` alone; the `include` line in niri's `config.kdl` is marked `// added by corpus-graph-setup` (on niri 26.04+ you can make it `include "corpus-graph.kdl" optional=true` so removing the file never breaks the config).

## Troubleshooting

- **Viewer does not open:** `journalctl --user -u corpus-graph-serve`. Before the first `build`, the server shows a placeholder page that reloads itself.
- **`claude` not found:** set `CORPUS_GRAPH_CLAUDE=$HOME/.local/bin/claude` in the config.
- **Theme not applied:** `noctalia theme --list-templates` should list `corpus_graph`; re-apply the theme once; check `~/.config/corpus-graph/theme.css` exists.
- **niri window rule ignored:** some browsers set the title after the window maps. Use `niri msg windows` to read the app-id and match `app-id=` instead.
- **Scanned or badly laid-out PDFs:** install `tesseract` and the language data and use `corpus-graph ingest --force --ocr`. The marker-pdf fallback (PyTorch, several GB) is deliberately not bundled.
