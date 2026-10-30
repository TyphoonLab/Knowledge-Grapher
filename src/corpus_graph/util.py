"""Shared helpers: hashing, Unicode normalisation, JSON/YAML IO, workspace paths."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".md", ".markdown"}


# --------------------------------------------------------------------------- workspace


@dataclass(frozen=True)
class Workspace:
    """Resolved locations of the pipeline directories.

    Every stage reads from and writes to these; nothing outside `work/` and `out/`
    is ever written, and nothing under `input/` is ever modified.
    """

    root: Path
    input: Path
    work: Path
    out: Path
    canon: Path

    @classmethod
    def from_args(cls, root=None, input=None, work=None, out=None, canon=None) -> "Workspace":
        """Root precedence: --root, $CORPUS_GRAPH_ROOT, the current directory if it is a
        workspace, CORPUS_GRAPH_ROOT in ~/.config/corpus-graph/config, the current directory."""
        r = Path(root or os.environ.get("CORPUS_GRAPH_ROOT") or _default_root()).expanduser().resolve()

        def pick(v, name):
            return Path(v).resolve() if v else r / name

        return cls(r, pick(input, "input"), pick(work, "work"), pick(out, "out"), pick(canon, "canon"))

    # work/ layout
    @property
    def md_dir(self) -> Path:
        return self.work / "md"

    @property
    def extract_dir(self) -> Path:
        return self.work / "extract"

    @property
    def llm_log(self) -> Path:
        return self.work / "llm_log.jsonl"

    @property
    def resolved_path(self) -> Path:
        return self.work / "resolved.json"

    @property
    def proposed_merges(self) -> Path:
        return self.work / "proposed_merges.json"

    @property
    def schema_proposals(self) -> Path:
        return self.work / "schema_proposals.json"

    @property
    def rejected_log(self) -> Path:
        return self.work / "rejected.jsonl"

    @property
    def graph_path(self) -> Path:
        return self.out / "graph.json"

    def ensure(self) -> None:
        for d in (self.work, self.md_dir, self.extract_dir, self.out):
            d.mkdir(parents=True, exist_ok=True)


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "corpus-graph"


def user_config() -> dict[str, str]:
    """~/.config/corpus-graph/config: shell-style KEY=VALUE lines (also sourced by the desktop scripts)."""
    out: dict[str, str] = {}
    try:
        text = (config_dir() / "config").read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k] = os.path.expanduser(os.path.expandvars(v))
    return out


def _default_root() -> Path:
    cwd = Path.cwd()
    # `init` always creates input/; canon/ or work/ alone do not count, so a source checkout
    # (which ships canon/) does not shadow the configured workspace
    if (cwd / "input").is_dir():
        return cwd
    cfg = user_config().get("CORPUS_GRAPH_ROOT")
    return Path(cfg) if cfg else cwd


# --------------------------------------------------------------------------- hashing / IO


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short_hash(h: str) -> str:
    return h[:12]


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


_UMASK = os.umask(0)
os.umask(_UMASK)


def _finish(tmp: str, path: Path) -> None:
    os.chmod(tmp, 0o666 & ~_UMASK)  # mkstemp creates 0600; give the file normal permissions
    os.replace(tmp, path)


def write_json(path: Path, data: Any, indent: int | None = 2) -> None:
    """Atomic JSON write (temp file + rename) so an interrupted run never leaves a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
        f.write("\n")
    _finish(tmp, path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    _finish(tmp, path)


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def read_yaml(path: Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        return default
    return default if data is None else data


def write_yaml(path: Path, data: Any, header: str = "") -> None:
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)
    write_text(path, (header + "\n" if header else "") + text)


def iter_sources(input_dir: Path) -> Iterable[Path]:
    if not input_dir.exists():
        return []
    return sorted(
        p
        for p in input_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES and not p.name.startswith((".", "~$"))
    )


# --------------------------------------------------------------------------- text normalisation

_WS = re.compile(r"\s+")
_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-"})
_QUOTES = str.maketrans({"‘": "'", "’": "'", "ʼ": "'", "“": '"', "”": '"', "„": '"', "«": '"', "»": '"'})


def nfc(s: str) -> str:
    """NFC-normalise. Greek and Hebrew are kept as Unicode; nothing is transliterated."""
    return unicodedata.normalize("NFC", s)


def strip_marks(s: str) -> str:
    """Remove combining marks (accents, breathings, niqqud) for comparison keys only."""
    decomposed = unicodedata.normalize("NFD", s)
    return unicodedata.normalize("NFC", "".join(c for c in decomposed if not unicodedata.combining(c)))


def fold(s: str) -> str:
    """Comparison key: casefolded, mark-stripped, punctuation-normalised, whitespace-collapsed."""
    s = nfc(s).translate(_DASHES).translate(_QUOTES)
    s = strip_marks(s).casefold()
    s = s.replace("ß", "ss").replace("ς", "σ")
    s = re.sub(r"[^\w\s:.,'\-]", " ", s)
    return _WS.sub(" ", s).strip()


def match_text(s: str) -> str:
    """Normalisation used when verifying that a quote occurs verbatim in a chunk.

    Tolerates the differences that PDF extraction introduces (hyphenation across
    line breaks, ligatures, curly quotes, Markdown emphasis, footnote markers) but
    not changes of wording.
    """
    s = nfc(s)
    s = s.replace("ﬁ", "fi").replace("ﬂ", "fl").replace("ﬀ", "ff").replace("ﬃ", "ffi").replace("ﬄ", "ffl")
    s = re.sub(r"\[\^\w+\]", "", s)  # footnote references
    s = re.sub(r"</?(sup|sub|u|mark)>", "", s)
    s = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", s)  # hyphenation at line end
    s = s.translate(_DASHES).translate(_QUOTES)
    s = re.sub(r"[*_`]", "", s)
    s = s.casefold()
    return _WS.sub(" ", s).strip()


def slug(s: str, maxlen: int = 60) -> str:
    s = strip_marks(nfc(s)).casefold()
    s = re.sub(r"[^\w]+", "-", s, flags=re.UNICODE).strip("-")
    return s[:maxlen].rstrip("-") or "x"


def word_count(s: str) -> int:
    return len(re.findall(r"\S+", s))


def estimate_tokens(text: str) -> int:
    """Cheap token estimate without a network call.

    UTF-8 bytes / 4 approximates English well and weights Greek and Hebrew
    (2 bytes per character, and tokenised less efficiently) appropriately.
    """
    return max(1, len(text.encode("utf-8")) // 4)


# --------------------------------------------------------------------------- language detection

_STOP = {
    "en": "the of and to in that is was for with as by this which are on not be from it an".split(),
    "de": "der die das und ist nicht zu den von mit sich des auf für im dem ein eine auch als".split(),
    "fr": "le la les et des est que une dans pour qui pas sur par au du ne il se plus".split(),
    "it": "il di che è la per non una sono del della con gli le si da nel anche come".split(),
}


def detect_language(text: str) -> str:
    words = re.findall(r"[a-zàâäçéèêëîïôöùûüÿœæß]+", text[:20000].lower())
    if not words:
        return "en"
    counts = {lang: 0 for lang in _STOP}
    sets = {lang: set(ws) for lang, ws in _STOP.items()}
    for w in words:
        for lang, st in sets.items():
            if w in st:
                counts[lang] += 1
    return max(counts, key=counts.get)


# --------------------------------------------------------------------------- terminal


def eprint(*args, **kwargs) -> None:
    print(*args, file=sys.stderr, **kwargs)


def notify(summary: str, body: str = "", urgency: str = "normal") -> bool:
    """Desktop notification via notify-send (libnotify); any freedesktop daemon, e.g. Noctalia's, shows it."""
    exe = shutil.which("notify-send")
    if not exe:
        return False
    try:
        subprocess.run([exe, "-a", "corpus-graph", "-i", "corpus-graph", "-u", urgency, summary, body],
                       capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return True


def confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False
