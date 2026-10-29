"""corpus-graph command line."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .util import Workspace, eprint

DESCRIPTION = """\
Turn a folder of scholarly documents into a typed, provenance-backed knowledge graph.

Pipeline (each stage is idempotent and reads/writes work/):
  init → ingest → extract → resolve → build → report → serve
Query layer (pure graph lookups, no model calls):
  query "<question>" | path <a> <b> | explain <node>   (exit 0 answer, 3 ambiguous, 4 no match)
"""


def _ws(a) -> Workspace:
    return Workspace.from_args(a.root, a.input, a.work, a.out, a.canon)


def _llm_args(p, default_backend="claude-cli"):
    p.add_argument("--backend", default=default_backend, choices=["claude-cli", "anthropic", "replay"],
                   help="LLM backend (default: claude-cli, i.e. `claude -p` on your subscription)")
    p.add_argument("--model", default="sonnet", help="model alias or id (default: sonnet)")
    p.add_argument("--fixtures", type=Path, help="recorded responses for --backend replay")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="corpus-graph", description=DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="workspace root (default: current directory or $CORPUS_GRAPH_ROOT)")
    ap.add_argument("--input", help="source documents (default: <root>/input)")
    ap.add_argument("--work", help="intermediate files (default: <root>/work)")
    ap.add_argument("--out", help="outputs (default: <root>/out)")
    ap.add_argument("--canon", help="canonical registries (default: <root>/canon)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create input/, canon/ (seed registries) and work/ in the workspace")
    p.add_argument("--force", action="store_true", help="overwrite existing canon files with the seeds")

    p = sub.add_parser("ingest", help="PDF/DOCX/MD in input/ -> work/md/<hash>.md + .meta.json")
    p.add_argument("--force", action="store_true", help="re-convert unchanged files")
    p.add_argument("--ocr", action="store_true", help="allow pymupdf4llm's OCR on image-only pages (needs an OCR engine)")

    p = sub.add_parser("extract", help="LLM extraction of nodes and edges per chunk (cached)")
    _llm_args(p)
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], help="model effort level")
    p.add_argument("--jobs", type=int, default=2, help="parallel LLM calls (default 2)")
    p.add_argument("--chunk-tokens", type=int, default=6000, help="target chunk size (default 6000)")
    p.add_argument("--limit", type=int, help="process at most N documents this run")
    p.add_argument("--only", nargs="+", help="only documents whose path or hash contains one of these")
    p.add_argument("--yes", action="store_true", help="do not ask before processing more than 30 documents")
    p.add_argument("--dry-run", action="store_true", help="print the plan and cost estimate only")
    p.add_argument("--force", action="store_true", help="ignore the cache")
    p.add_argument("--record", type=Path, help="also save responses as replay fixtures in this directory")

    p = sub.add_parser("resolve", help="merge entities into canonical nodes; write proposals")
    _llm_args(p)
    p.add_argument("--llm-merge", action="store_true", help="ask the LLM to judge fuzzy merge candidates (still not applied)")

    p = sub.add_parser("merges", help="review proposed merges")
    msub = p.add_subparsers(dest="mcmd", required=True)
    q = msub.add_parser("list", help="list proposals")
    q.add_argument("--status", default="pending", choices=["pending", "approved", "rejected", "all"])
    q.add_argument("--type")
    q = msub.add_parser("approve", help="approve proposals by id (prefix ok)")
    q.add_argument("ids", nargs="*")
    q.add_argument("--into", help="target node id (required for ambiguous proposals)")
    q.add_argument("--min-score", type=float, help="approve every pending fuzzy proposal scoring at least this")
    q.add_argument("--type", help="with --min-score: only this node type")
    q = msub.add_parser("reject", help="reject proposals by id (prefix ok)")
    q.add_argument("ids", nargs="+")

    p = sub.add_parser("build", help="networkx graph + communities -> out/graph.json")
    p.add_argument("--community-mode", choices=["entity", "full"], default="entity")
    p.add_argument("--resolution", type=float, default=1.0, help="Leiden resolution (higher = smaller communities)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--llm-labels", action="store_true", help="name communities with the LLM")
    _llm_args(p)

    sub.add_parser("report", help="computed findings -> out/REPORT.md")

    p = sub.add_parser("serve", help="write out/index.html and serve it locally")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--write-only", action="store_true", help="write index.html and exit")
    p.add_argument("--no-browser", action="store_true", help="do not open a browser")

    p = sub.add_parser("run", help="ingest, extract, resolve, build, report and write the viewer")
    _llm_args(p)
    p.add_argument("--chunk-tokens", type=int, default=6000)
    p.add_argument("--jobs", type=int, default=2)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--limit", type=int)

    p = sub.add_parser("query", help="subgraph for a question, as Markdown")
    p.add_argument("question", nargs="+")
    p.add_argument("--quotes", type=int, default=2, help="quotes per edge (default 2)")
    p.add_argument("--max-edges", type=int, default=25, help="edges shown per matched node (default 25)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("path", help="shortest path between two nodes")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--quotes", type=int, default=2)
    p.add_argument("--via-documents", action="store_true", help="allow the path to pass through Document nodes")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("explain", help="everything the graph holds about one node")
    p.add_argument("node", nargs="+")
    p.add_argument("--quotes", type=int, default=3)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("export-vault", help="write an Obsidian vault: source notes + one note per node")
    p.add_argument("dest", type=Path, nargs="?", help="vault folder (default: <out>/vault)")

    a = ap.parse_args(argv)
    ws = _ws(a)

    if a.cmd == "init":
        from .canon import init_canon

        ws.input.mkdir(parents=True, exist_ok=True)
        ws.ensure()
        written = init_canon(ws.canon, a.force)
        print(f"workspace at {ws.root}: put documents in {ws.input}; canon files written: {', '.join(written) or 'none (already present)'}")
        return 0
    if a.cmd == "ingest":
        from . import ingest

        r = ingest.run(ws, force=a.force, use_ocr=a.ocr)
        return 1 if r.get("failed") else 0
    if a.cmd == "extract":
        from . import extract

        r = extract.run(ws, a.backend, a.model, a.effort, a.jobs, a.chunk_tokens, a.yes, a.limit, a.force,
                        a.fixtures, a.record, a.only, a.dry_run)
        return 1 if r.get("failed") else 0
    if a.cmd == "resolve":
        from . import resolve

        resolve.run(ws, llm_merge=a.llm_merge, backend=a.backend, model=a.model, fixtures=a.fixtures)
        return 0
    if a.cmd == "merges":
        from . import merges

        if a.mcmd == "list":
            return merges.list_(ws, a.status, a.type)
        return merges.decide(ws, a.ids, a.mcmd == "approve", getattr(a, "into", None),
                             getattr(a, "min_score", None), getattr(a, "type", None))
    if a.cmd == "build":
        from . import build

        build.run(ws, a.community_mode, a.seed, a.resolution, a.llm_labels, a.backend, a.model)
        return 0
    if a.cmd == "report":
        from . import report

        report.run(ws)
        return 0
    if a.cmd == "serve":
        from . import serve

        path = serve.write_viewer(ws)
        eprint(f"serve: wrote {path}")
        if a.write_only:
            return 0
        return serve.serve(ws, a.port, open_browser=not a.no_browser)
    if a.cmd == "run":
        from . import build, extract, ingest, report, resolve, serve

        ingest.run(ws)
        r = extract.run(ws, a.backend, a.model, None, a.jobs, a.chunk_tokens, a.yes, a.limit, fixtures=a.fixtures)
        if r.get("failed"):
            eprint("extract had failures; continuing with what was extracted")
        resolve.run(ws)
        build.run(ws)
        report.run(ws)
        eprint(f"serve: wrote {serve.write_viewer(ws)}  (run `corpus-graph serve` to browse)")
        return 0
    if a.cmd in ("query", "path", "explain"):
        from . import query
        from .graphstore import GraphStore

        try:
            store = GraphStore.load(ws)
        except FileNotFoundError as exc:
            eprint(str(exc))
            return 2
        if a.cmd == "query":
            return query.query(store, " ".join(a.question), a.quotes, a.max_edges, a.json)
        if a.cmd == "path":
            return query.path(store, a.a, a.b, a.quotes, a.via_documents, a.json)
        return query.explain(store, " ".join(a.node), a.quotes, a.json)
    if a.cmd == "export-vault":
        from . import vault

        dest = vault.export(ws, a.dest)
        print(f"vault written to {dest}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
