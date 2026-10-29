"""`query`, `path`, `explain`: pure graph lookups that print Markdown for Claude Code (or you).

Exit codes: 0 answer, 3 ambiguous name (candidates listed), 4 no match.
No model calls happen here.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque

from .graphstore import GraphStore

EXIT_OK, EXIT_AMBIGUOUS, EXIT_NO_MATCH = 0, 3, 4


def _edge_line(s: GraphStore, nid: str, direction: str, e: dict, quotes: int) -> list[str]:
    other = e["target"] if direction == "out" else e["source"]
    arrow = f"—{e['relation']}→ {s.ref(other)}" if direction == "out" else f"←{e['relation']}— {s.ref(other)}"
    tag = e["confidence"] + (", derived" if e.get("derived") else "")
    lines = [f"- {arrow}  _[{tag}; {len(e['provenance'])} quote(s)]_"]
    for p in s.ranked_provenance(e)[:quotes]:
        lines.append("")
        lines += ["  " + l for l in s.cite(p).splitlines()]
    lines.append("")
    return lines


def _node_block(s: GraphStore, nid: str, quotes: int, max_edges: int | None, heading: str = "##") -> list[str]:
    n = s.nodes[nid]
    out = [f"{heading} {n['canonical_name']} · {n['type']} · `{nid}`", ""]
    if n.get("unresolved"):
        out += ["**Unresolved**: identity not settled"
                + (f" (pending merge proposals {', '.join(n['attrs'].get('pending_merges', []))})" if n['attrs'].get('pending_merges') else "")
                + ".", ""]
    if n.get("aliases"):
        out += [f"Aliases: {', '.join(n['aliases'])}", ""]
    a = n.get("attrs", {})
    if n["type"] == "PrimaryText" and a.get("work"):
        rng = ".".join(map(str, a["start"])) + ("–" + ".".join(map(str, a["end"])) if a["start"] != a["end"] else "")
        out += [f"Locus: work `{a['work']}` ({a.get('work_display')}), reference {rng}"
                + (" ff." if a.get("open_end") else "")
                + (f"; ancient author {a['ancient_author']}" if a.get("ancient_author") else ""), ""]
    if n["type"] == "Scholar" and a.get("years"):
        out += [f"Publication years seen: {', '.join(map(str, a['years']))}", ""]
    if n["type"] == "Document":
        out += [f"{', '.join(a.get('authors') or [])} ({a.get('year') or 'n.d.'}). {a.get('venue') or ''} · source `{a.get('source_path')}` · `{a.get('md_path')}`", ""]
    if a.get("notes"):
        out += ["Resolver notes: " + "; ".join(a["notes"]), ""]
    groups: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for d, e in s.edges_of(nid):
        groups[e["relation"]].append((d, e))
    shown = 0
    for rel in sorted(groups, key=lambda r: (r in ("cites", "discusses"), r)):
        items = sorted(groups[rel], key=lambda de: -len(de[1]["provenance"]))
        out += [f"### {rel} ({len(items)})", ""]
        for d, e in items:
            if max_edges is not None and shown >= max_edges:
                out += [f"- … {len(items) - items.index((d, e))} more (use `corpus-graph explain {nid}`)", ""]
                break
            out += _edge_line(s, nid, d, e, quotes)
            shown += 1
    if n["provenance"]:
        out += [f"### Mentions ({len(n['provenance'])} quote(s) in {len(s.docs_of(nid))} document(s))", ""]
        for p in n["provenance"][: max(quotes, 3) if max_edges is not None else None]:
            out += s.cite(p).splitlines() + [""]
    return out


def _ambiguous(s: GraphStore, spans: list, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"status": "ambiguous", "candidates": {m.span: m.node_ids for m in spans}}, ensure_ascii=False, indent=2))
        return EXIT_AMBIGUOUS
    print("# Ambiguous name\n")
    for m in spans:
        print(f"\"{m.span}\" matches several nodes:\n")
        for nid in m.node_ids:
            n = s.nodes[nid]
            yrs = n["attrs"].get("years")
            extra = f"; years {', '.join(map(str, yrs))}" if yrs else ""
            print(f"- {s.ref(nid)}: {len(n['provenance'])} mention(s) in {len(s.docs_of(nid))} document(s){extra}")
        print()
    print("Re-run with the node id (e.g. `corpus-graph explain <id>`) or add a disambiguating year or initial.")
    return EXIT_AMBIGUOUS


def _no_match(text: str, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"status": "no_match", "query": text}))
    else:
        print(f"# No match\n\nThe graph has no node matching \"{text}\". The corpus says nothing about it that the "
              "graph has captured; do not answer from general knowledge as if it came from the corpus.")
    return EXIT_NO_MATCH


def query(s: GraphStore, question: str, quotes: int = 2, max_neighbours: int = 25, as_json: bool = False) -> int:
    r = s.match(question)
    if r.ambiguous and not r.matches:
        return _ambiguous(s, r.ambiguous, as_json)
    ids = r.node_ids
    if not ids:
        return _no_match(question, as_json)
    if as_json:
        nb = {i: sorted(s.neighbours(i)) for i in ids}
        sub_ids = set(ids) | {m for v in nb.values() for m in v}
        print(json.dumps({
            "status": "ok",
            "matched": [{"span": m.span, "via": m.via, "nodes": m.node_ids} for m in r.matches],
            "ambiguous": [{"span": m.span, "nodes": m.node_ids} for m in r.ambiguous],
            "nodes": [s.nodes[i] for i in sorted(sub_ids)],
            "edges": [e for e in s.g["edges"] if e["source"] in ids or e["target"] in ids],
        }, ensure_ascii=False, indent=2))
        return EXIT_AMBIGUOUS if r.ambiguous else EXIT_OK
    print(f"# corpus-graph query: \"{question}\"\n")
    print("Matched:")
    for m in r.matches:
        shown = ", ".join(s.ref(i) for i in m.node_ids[:12]) + (f", … {len(m.node_ids) - 12} more" if len(m.node_ids) > 12 else "")
        print(f"- \"{m.span}\" ({m.via}) → {shown}")
    if r.ambiguous:
        for m in r.ambiguous:
            print(f"- \"{m.span}\" is ambiguous between {', '.join(s.ref(i) for i in m.node_ids)}; not expanded")
    print()
    print("Every claim below is backed by the verbatim quote under it (document, page, `markdown:line`).\n")
    for nid in ids[:15]:
        print("\n".join(_node_block(s, nid, quotes, max_neighbours)))
    if len(ids) > 15:
        print(f"_… {len(ids) - 15} further matched nodes not shown: {', '.join(ids[15:40])}_\n")
    # one-hop neighbourhood summary
    nb = set()
    for i in ids:
        nb |= s.neighbours(i)
    nb -= set(ids)
    docs = sorted(i for i in nb if s.nodes[i]["type"] == "Document")
    others = sorted((i for i in nb if s.nodes[i]["type"] != "Document"), key=lambda i: (s.nodes[i]["type"], s.name(i)))
    print("## One-hop neighbourhood\n")
    if docs:
        print("Documents: " + "; ".join(f"{s.name(d)} (`{d}`)" for d in docs) + "\n")
    by_type = defaultdict(list)
    for i in others:
        by_type[s.nodes[i]["type"]].append(i)
    for t, lst in by_type.items():
        print(f"- {t}: " + ", ".join(f"{s.name(i)} (`{i}`)" for i in lst))
    print()
    return EXIT_AMBIGUOUS if r.ambiguous else EXIT_OK


def explain(s: GraphStore, ref: str, quotes: int = 3, as_json: bool = False) -> int:
    nid, cands = s.resolve_one(ref)
    if nid is None and cands:
        from .graphstore import Match
        return _ambiguous(s, [Match(ref, cands, "name")], as_json)
    if nid is None:
        return _no_match(ref, as_json)
    if as_json:
        print(json.dumps({"status": "ok", "node": s.nodes[nid],
                          "edges": [e for _, e in s.edges_of(nid)]}, ensure_ascii=False, indent=2))
        return EXIT_OK
    print("\n".join(_node_block(s, nid, quotes, None, heading="#")))
    return EXIT_OK


def path(s: GraphStore, a: str, b: str, quotes: int = 2, via_documents: bool = False, as_json: bool = False) -> int:
    ids = []
    for ref in (a, b):
        nid, cands = s.resolve_one(ref)
        if nid is None and cands:
            from .graphstore import Match
            return _ambiguous(s, [Match(ref, cands, "name")], as_json)
        if nid is None:
            return _no_match(ref, as_json)
        ids.append(nid)
    src, dst = ids
    p = _shortest(s, src, dst, allow_docs=via_documents)
    used_docs = via_documents
    if p is None and not via_documents:
        p = _shortest(s, src, dst, allow_docs=True)
        used_docs = True
    if p is None:
        if as_json:
            print(json.dumps({"status": "no_path", "source": src, "target": dst}))
        else:
            print(f"# No path\n\n{s.ref(src)} and {s.ref(dst)} are not connected in the graph.")
        return EXIT_NO_MATCH
    if as_json:
        print(json.dumps({"status": "ok", "path": [{"node": n, "edge": e} for n, e in p]}, ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"# Path: {s.name(src)} → {s.name(dst)}\n")
    print(f"{len(p) - 1} hop(s)" + (" (through document nodes: no entity-only path exists)" if used_docs and not via_documents else "") + ".\n")
    for i in range(1, len(p)):
        prev, _ = p[i - 1]
        cur, e = p[i]
        fwd = e["source"] == prev
        arrow = f"{s.ref(prev)} —{e['relation']}→ {s.ref(cur)}" if fwd else f"{s.ref(prev)} ←{e['relation']}— {s.ref(cur)}"
        print(f"{i}. {arrow}  _[{e['confidence']}]_\n")
        for pv in s.ranked_provenance(e)[:quotes]:
            print("\n".join("   " + l for l in s.cite(pv).splitlines()) + "\n")
    return EXIT_OK


def _shortest(s: GraphStore, src: str, dst: str, allow_docs: bool):
    """BFS over the undirected view; returns [(node, edge_used_to_reach_it)]."""
    prev: dict[str, tuple[str, dict] | None] = {src: None}
    q = deque([src])
    while q:
        u = q.popleft()
        if u == dst:
            break
        for d, e in s.edges_of(u):
            v = e["target"] if d == "out" else e["source"]
            if v in prev:
                continue
            if not allow_docs and s.nodes[v]["type"] == "Document" and v != dst:
                continue
            prev[v] = (u, e)
            q.append(v)
    if dst not in prev:
        return None
    out, cur = [], dst
    while cur is not None:
        pe = prev[cur]
        out.append((cur, pe[1] if pe else None))
        cur = pe[0] if pe else None
    return list(reversed(out))
