"""Stage 2: LLM extraction of typed nodes and edges, one call per chunk, cached per chunk.

work/extract/<hash>/index.json     the document's current chunk list (ids, lines, pages, hashes)
work/extract/<hash>/<chunk>.json   raw response + validated items with provenance
work/rejected.jsonl                items dropped at validation, with the reason
"""

from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .chunking import DEFAULT_CHUNK_TOKENS, Chunk, ParsedMarkdown, QuoteLocator, verify_quote
from .llm import LLMError, list_price, make_backend, model_id
from .prompts import PROMPT_VERSION, extraction_schema, system_prompt, user_prompt
from .schema import Schema
from .util import (Workspace, append_jsonl, confirm, eprint, estimate_tokens, nfc, read_json, read_jsonl, sha256_text,
                   write_json)

MAX_QUOTE_WORDS = 25
DOC_GATE = 30  # ask before processing more documents than this in one run
# Output-token model per chunk: OUTPUT_FIXED + OUTPUT_RATIO * chunk tokens. The JSON (a quote on every
# node and edge) dominates, so output often exceeds input. Overridden by the ratio observed in
# work/llm_log.jsonl once at least 3 extraction calls have been logged.
OUTPUT_FIXED = 1500
OUTPUT_RATIO = 1.0
PROMPT_OVERHEAD = 2000  # tokens the `claude -p` harness adds around our system prompt


@dataclass
class DocPlan:
    hash: str
    source: str
    title: str
    md_path: Path
    chunks: list[Chunk]
    header: str
    todo: list[Chunk] = field(default_factory=list)


def chunk_key(header: str, chunk: Chunk, schema_hash: str) -> str:
    fn = "\n".join(f"{f.label}:{f.text}" for f in chunk.footnotes)
    return sha256_text("\x1e".join([schema_hash, header, chunk.text, fn]))[:24]


def schema_fingerprint(schema: Schema) -> str:
    return sha256_text(PROMPT_VERSION + system_prompt(schema) + json.dumps(extraction_schema(schema), sort_keys=True))[:16]


def plan(ws: Workspace, schema: Schema, chunk_tokens: int, force: bool = False, only: list[str] | None = None) -> list[DocPlan]:
    manifest = read_json(ws.work / "manifest.json", {}) or {}
    sh = schema_fingerprint(schema)
    plans = []
    for h, info in sorted(manifest.items(), key=lambda kv: kv[1]["source"]):
        if only and not any(o in info["source"] or h.startswith(o) for o in only):
            continue
        md_path = ws.md_dir / f"{h}.md"
        if not md_path.exists():
            continue
        md = ParsedMarkdown.load(md_path)
        chunks = md.chunks(chunk_tokens)
        header = md.header()
        p = DocPlan(h, info["source"], md.meta.get("title") or info["source"], md_path, chunks, header)
        for c in chunks:
            cached = read_json(ws.extract_dir / h / f"{c.id}.json")
            if force or not cached or cached.get("chunk_key") != chunk_key(header, c, sh):
                p.todo.append(c)
        plans.append(p)
    return plans


def observed_output_model(log_path: Path) -> tuple[float, float] | None:
    """(fixed, ratio) fitted from logged extraction calls, or None if too few."""
    rows = [r for r in read_jsonl(log_path)
            if r.get("ok") and r.get("purpose") == "extract" and r.get("chunk_tokens") and r.get("tokens_out")
            and r.get("backend") != "replay"]
    if len(rows) < 3:
        return None
    xs = [r["chunk_tokens"] for r in rows]
    ys = [r["tokens_out"] for r in rows]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    var = sum((x - mx) ** 2 for x in xs)
    if var < 1e-9:
        return 0.0, my / mx
    ratio = max(0.0, sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var)
    return max(0.0, my - ratio * mx), ratio


def estimate(plans: list[DocPlan], schema: Schema, model: str, log_path: Path | None = None) -> dict:
    sys_tokens = PROMPT_OVERHEAD + estimate_tokens(system_prompt(schema)) + estimate_tokens(json.dumps(extraction_schema(schema)))
    fixed, ratio = (observed_output_model(log_path) if log_path else None) or (OUTPUT_FIXED, OUTPUT_RATIO)
    chunks = [(p, c) for p in plans for c in p.todo]
    tin = sum(sys_tokens + estimate_tokens(p.header) + c.tokens for p, c in chunks)
    tout = int(sum(fixed + ratio * c.tokens for _, c in chunks))
    return {"docs": sum(1 for p in plans if p.todo), "chunks": len(chunks), "tokens_in": tin, "tokens_out": tout,
            "cost_usd": list_price(model, tin, tout), "output_model": {"fixed": round(fixed), "ratio": round(ratio, 2)}}


def run(ws: Workspace, backend: str = "claude-cli", model: str = "sonnet", effort: str | None = None,
        jobs: int = 2, chunk_tokens: int = DEFAULT_CHUNK_TOKENS, yes: bool = False, limit: int | None = None,
        force: bool = False, fixtures: Path | None = None, record: Path | None = None,
        only: list[str] | None = None, dry_run: bool = False) -> dict:
    ws.ensure()
    schema = Schema.load(ws.canon)
    plans = plan(ws, schema, chunk_tokens, force, only)
    for p in plans:  # chunk index is always refreshed, even when nothing needs extracting
        write_json(ws.extract_dir / p.hash / "index.json", {
            "source": p.source, "title": p.title, "header": p.header,
            "chunks": [{"id": c.id, "line_start": c.line_start, "line_end": c.line_end, "pages": c.pages,
                        "tokens": c.tokens} for c in p.chunks],
        })
    todo = [p for p in plans if p.todo]
    if limit is not None:
        todo = todo[:limit]
    est = estimate(todo, schema, model, ws.llm_log)
    eprint(
        f"extract: {est['docs']} document(s), {est['chunks']} chunk(s) to process "
        f"({sum(len(p.chunks) for p in plans) - sum(len(p.todo) for p in plans)} cached). "
        f"Estimate ~{est['tokens_in']:,} input + ~{est['tokens_out']:,} output tokens "
        f"≈ ${est['cost_usd']:.2f} at {model_id(model)} list price"
        + (" (replayed: no tokens spent)" if backend == "replay" else "")
    )
    if not todo or dry_run:
        return {"estimate": est, "processed": 0}
    if len(todo) > DOC_GATE and not yes:
        if not confirm(f"This run would process {len(todo)} documents (more than {DOC_GATE}). Continue?"):
            eprint(f"Stopped before extraction. Re-run with --yes, or --limit {DOC_GATE} to process a first batch.")
            sys.exit(2)

    be = make_backend(backend, model, ws.llm_log, effort=effort, fixtures=fixtures, record=record)
    sys_p = system_prompt(schema)
    json_schema = extraction_schema(schema)
    sh = schema_fingerprint(schema)
    lock = threading.Lock()
    totals = {"in": 0, "out": 0, "cost": 0.0, "done": 0, "failed": 0, "nodes": 0, "edges": 0, "rejected": 0}
    jobs_list = [(p, c) for p in todo for c in p.todo]
    locators: dict[str, QuoteLocator] = {}

    def work(p: DocPlan, c: Chunk):
        tag = {"purpose": "extract", "doc": p.hash[:12], "chunk": c.id, "source_stem": Path(p.source).stem,
               "chunk_tokens": c.tokens}
        res = be.complete_json(sys_p, user_prompt(p.header, c), json_schema, tag)
        with lock:
            loc = locators.get(p.hash) or locators.setdefault(p.hash, QuoteLocator(ParsedMarkdown.load(p.md_path)))
        validated = validate(res.data, c, p.hash, loc, schema)
        rec = {
            "chunk_key": chunk_key(p.header, c, sh),
            "prompt_version": PROMPT_VERSION,
            "backend": be.name,
            "model": res.model,
            "usage": {"tokens_in": res.tokens_in, "tokens_out": res.tokens_out, "cache_read": res.cache_read,
                      "cache_write": res.cache_write, "cost_usd": res.cost_usd},
            "chunk": {"id": c.id, "line_start": c.line_start, "line_end": c.line_end, "pages": c.pages},
            **validated,
            "raw": res.data,
        }
        write_json(ws.extract_dir / p.hash / f"{c.id}.json", rec)
        for r in validated["rejected"]:
            append_jsonl(ws.rejected_log, {"doc": p.hash[:12], "source": p.source, "chunk": c.id, **r})
        return res, validated

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        futs = {ex.submit(work, p, c): (p, c) for p, c in jobs_list}
        for fut in as_completed(futs):
            p, c = futs[fut]
            try:
                res, v = fut.result()
            except (LLMError, json.JSONDecodeError, KeyError) as exc:
                totals["failed"] += 1
                eprint(f"  ! {p.source} {c.id}: {exc}")
                continue
            totals["done"] += 1
            totals["in"] += res.tokens_in + res.cache_read + res.cache_write
            totals["out"] += res.tokens_out
            if not be.name.startswith("replay"):  # replayed responses cost nothing
                totals["cost"] += res.cost_usd or list_price(model, res.tokens_in, res.tokens_out, res.cache_read, res.cache_write)
            totals["nodes"] += len(v["nodes"])
            totals["edges"] += len(v["edges"])
            totals["rejected"] += len(v["rejected"])
            eprint(
                f"  [{totals['done'] + totals['failed']}/{len(jobs_list)}] {p.source} {c.id}: "
                f"{len(v['nodes'])} nodes, {len(v['edges'])} edges, {len(v['rejected'])} rejected | "
                f"run: {totals['in']:,} in / {totals['out']:,} out tokens, ${totals['cost']:.3f}"
            )
    eprint(
        f"extract: {totals['done']} chunk(s) done, {totals['failed']} failed; "
        f"{totals['nodes']} nodes, {totals['edges']} edges kept, {totals['rejected']} rejected "
        f"(see {ws.rejected_log.name})"
    )
    return {"estimate": est, "processed": totals["done"], **totals}


# --------------------------------------------------------------------------- validation


def _trim_quote(q: str) -> str:
    words = q.split()
    return " ".join(words[:MAX_QUOTE_WORDS])


def validate(data: dict, chunk: Chunk, doc_hash: str, locator: QuoteLocator, schema: Schema) -> dict:
    """Keep only items whose quote is found verbatim in the chunk; attach provenance."""
    hay = chunk.verification_text()
    rejected = []

    def prov(quote: str, kind: str, label: str):
        quote = nfc((quote or "").strip().strip('"“”'))
        if not quote:
            rejected.append({"kind": kind, "item": label, "reason": "missing quote"})
            return None
        trimmed = _trim_quote(quote)
        v = verify_quote(trimmed, hay)
        if v is None:
            rejected.append({"kind": kind, "item": label, "reason": "quote not found in chunk", "quote": quote})
            return None
        status, matched = v
        where = locator.locate(trimmed if status == "exact" else matched)
        line, page = where if where else (chunk.line_start, chunk.pages[0] if chunk.pages else None)
        return {"doc": doc_hash, "chunk": chunk.id, "quote": trimmed, "match": status, "line": line, "page": page,
                **({"truncated": True} if trimmed != quote else {})}

    nodes, ids = [], {"DOC"}
    for n in data.get("nodes") or []:
        name = nfc((n.get("name") or "").strip())
        ntype = n.get("type")
        label = f"{ntype}:{name}"
        if not name or ntype not in schema.node_types or ntype == "Document":
            rejected.append({"kind": "node", "item": label, "reason": "missing name or unknown type"})
            continue
        pv = prov(n.get("quote"), "node", label)
        if pv is None:
            continue
        nodes.append({
            "local_id": n.get("id"),
            "type": ntype,
            "name": name,
            "aliases": [nfc(a.strip()) for a in (n.get("aliases") or []) if a and a.strip() and a.strip() != name],
            "surname": n.get("surname"),
            "given_names": n.get("given_names"),
            "cited_years": [int(y) for y in (n.get("cited_years") or []) if isinstance(y, int) or str(y).isdigit()],
            "is_document_author": bool(n.get("is_document_author")),
            "citation": nfc(n["citation"]) if n.get("citation") else None,
            "note": n.get("note"),
            "provenance": pv,
        })
        ids.add(n.get("id"))

    edges = []
    for e in data.get("edges") or []:
        label = f"{e.get('source')} -{e.get('relation')}-> {e.get('target')}"
        if e.get("source") not in ids or e.get("target") not in ids:
            rejected.append({"kind": "edge", "item": label, "reason": "endpoint missing or rejected"})
            continue
        if e.get("relation") not in schema.edge_types:
            rejected.append({"kind": "edge", "item": label, "reason": "relation not in schema"})
            continue
        pv = prov(e.get("quote"), "edge", label)
        if pv is None:
            continue
        edges.append({"source": e["source"], "target": e["target"], "relation": e["relation"],
                      "confidence": e.get("confidence") if e.get("confidence") in ("extracted", "inferred") else "extracted",
                      "provenance": pv})

    proposals = []
    for sp in data.get("schema_proposals") or []:
        pv = prov(sp.get("quote"), "schema_proposal", sp.get("relation", "?"))
        if pv is None:
            continue
        proposals.append({**{k: sp.get(k) for k in ("relation", "source", "target", "source_type", "target_type", "rationale")},
                          "provenance": pv})

    meta = data.get("document_meta")
    if meta and not any(meta.get(k) for k in ("title", "authors", "year", "venue")):
        meta = None
    return {"nodes": nodes, "edges": edges, "schema_proposals": proposals, "document_meta": meta, "rejected": rejected}


def load_extractions(ws: Workspace) -> dict[str, dict]:
    """doc hash -> {"index": ..., "chunks": [chunk records]} for current chunks only."""
    manifest = read_json(ws.work / "manifest.json", {}) or {}
    out = {}
    for h in manifest:
        idx = read_json(ws.extract_dir / h / "index.json")
        if not idx:
            continue
        recs = []
        for c in idx["chunks"]:
            r = read_json(ws.extract_dir / h / f"{c['id']}.json")
            if r:
                recs.append(r)
        out[h] = {"index": idx, "chunks": recs}
    return out

