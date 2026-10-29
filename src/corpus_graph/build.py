"""Stage 4: assemble the graph, detect communities, write out/graph.json.

Communities are detected on the *entity* graph by default: Document nodes are
left out (otherwise every community is just "the things one article mentions")
and entities are linked by their typed edges plus a co-mention weight for
appearing in the same chunk. Documents are then placed in the community most of
their neighbours belong to. `--community-mode full` runs on the whole graph.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from itertools import combinations

import networkx as nx

from .schema import Schema
from .util import Workspace, eprint, read_json, write_json

MAX_COMENTION_GROUP = 80


def to_networkx(graph: dict) -> nx.MultiDiGraph:
    G = nx.MultiDiGraph()
    for n in graph["nodes"]:
        G.add_node(n["id"], **{k: v for k, v in n.items() if k != "id"})
    for e in graph["edges"]:
        G.add_edge(e["source"], e["target"], key=e["relation"], **e)
    return G


def undirected_simple(G: nx.MultiDiGraph, exclude_types: set[str] = frozenset()) -> nx.Graph:
    H = nx.Graph()
    for n, d in G.nodes(data=True):
        if d.get("type") not in exclude_types:
            H.add_node(n)
    for s, t, d in G.edges(data=True):
        if s in H and t in H and s != t:
            w = 1.0 + 0.25 * (len(d.get("provenance") or []) - 1)
            if H.has_edge(s, t):
                H[s][t]["weight"] += w
            else:
                H.add_edge(s, t, weight=w)
    return H


def community_graph(G: nx.MultiDiGraph, graph: dict, mode: str) -> nx.Graph:
    if mode == "full":
        return undirected_simple(G)
    H = undirected_simple(G, exclude_types={"Document"})
    groups = defaultdict(set)
    for n in graph["nodes"]:
        if n["type"] == "Document":
            continue
        for p in n["provenance"]:
            groups[(p["doc"], p.get("chunk"))].add(n["id"])
    for members in groups.values():
        k = len(members)
        if k < 2 or k > MAX_COMENTION_GROUP:
            continue
        w = 1.0 / (k - 1)
        for a, b in combinations(sorted(members), 2):
            if H.has_edge(a, b):
                H[a][b]["weight"] += w
            else:
                H.add_edge(a, b, weight=w)
    return H


def detect_communities(H: nx.Graph, seed: int = 42, resolution: float = 1.0) -> tuple[list[set], str]:
    if H.number_of_nodes() == 0:
        return [], "none"
    try:
        import igraph as ig
        import leidenalg

        nodes = list(H.nodes())
        idx = {n: i for i, n in enumerate(nodes)}
        g = ig.Graph(n=len(nodes), edges=[(idx[a], idx[b]) for a, b in H.edges()])
        weights = [H[a][b].get("weight", 1.0) for a, b in H.edges()]
        part = leidenalg.find_partition(
            g, leidenalg.RBConfigurationVertexPartition, weights=weights or None,
            resolution_parameter=resolution, seed=seed,
        )
        comms = [{nodes[i] for i in c} for c in part]
        return comms, f"leiden (leidenalg {leidenalg.version}, RBConfiguration, resolution {resolution})"
    except ImportError:
        comms = nx.community.louvain_communities(H, weight="weight", seed=seed, resolution=resolution)
        return [set(c) for c in comms], f"louvain (networkx {nx.__version__})"


def label_community(members: list[str], G: nx.MultiDiGraph, k: int = 3) -> tuple[str, list[str]]:
    ents = [m for m in members if G.nodes[m].get("type") != "Document"] or members
    ranked = sorted(ents, key=lambda m: (-G.degree(m), G.nodes[m].get("canonical_name", m)))
    top = ranked[:k]
    def short(x: str) -> str:
        return x if len(x) <= 40 else x[:38].rstrip() + "…"

    return " · ".join(short(G.nodes[m].get("canonical_name", m)) for m in top), ranked[:10]


def run(ws: Workspace, community_mode: str = "entity", seed: int = 42, resolution: float = 1.0,
        llm_labels: bool = False, backend: str = "claude-cli", model: str = "sonnet", quiet: bool = False) -> dict:
    res = read_json(ws.resolved_path)
    if not res:
        eprint("build: run `corpus-graph resolve` first")
        return {}
    ws.out.mkdir(parents=True, exist_ok=True)
    schema = Schema.load(ws.canon)
    G = to_networkx(res)
    H = community_graph(G, res, community_mode)
    comms, algo = detect_communities(H, seed, resolution)
    comms = sorted(comms, key=lambda c: (-len(c), min(c)))
    cid_of: dict[str, int] = {}
    for i, c in enumerate(comms):
        for n in c:
            cid_of[n] = i
    # documents (and anything left out) join the community of most of their neighbours
    for n in G.nodes():
        if n in cid_of:
            continue
        votes = Counter(cid_of[m] for m in nx.all_neighbors(G, n) if m in cid_of)
        if votes:
            cid_of[n] = votes.most_common(1)[0][0]
        else:
            cid_of[n] = len(comms)
            comms.append({n})
    members: dict[int, list[str]] = defaultdict(list)
    for n, c in cid_of.items():
        members[c].append(n)
    communities = []
    for c in sorted(members):
        label, top = label_community(members[c], G)
        types = Counter(G.nodes[m]["type"] for m in members[c])
        communities.append({"id": c, "label": label, "size": len(members[c]), "top_nodes": top, "types": dict(types)})
    if llm_labels:
        _llm_label(ws, communities, G, backend, model)

    nodes = []
    for n in res["nodes"]:
        nodes.append({**n, "community": cid_of[n["id"]], "degree": G.degree(n["id"]),
                      "docs": sorted({p["doc"][:12] for p in n["provenance"]}) if n["type"] != "Document" else [n["attrs"]["hash"][:12]]})
    years = [d["year"] for d in res["documents"] if d.get("year")]
    graph = {
        "meta": {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "counts": {"nodes": len(nodes), "edges": len(res["edges"]), "documents": len(res["documents"]),
                       "communities": len(communities), "unresolved": sum(1 for n in nodes if n["unresolved"])},
            "community_algorithm": algo,
            "community_mode": community_mode,
            "year_range": [min(years), max(years)] if years else None,
            "schema": {"node_types": schema.node_types, "edge_types": schema.edge_types},
        },
        "documents": res["documents"],
        "nodes": nodes,
        "edges": res["edges"],
        "communities": communities,
    }
    write_json(ws.graph_path, graph, indent=None)
    if not quiet:
        eprint(f"build: {len(nodes)} nodes, {len(res['edges'])} edges, {len(communities)} communities ({algo}) -> {ws.graph_path}")
    return graph


def _llm_label(ws, communities, G, backend, model):
    from .llm import make_backend

    be = make_backend(backend, model, ws.llm_log)
    schema = {"type": "object", "additionalProperties": False, "required": ["labels"],
              "properties": {"labels": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                                   "required": ["id", "label"],
                                                                   "properties": {"id": {"type": "integer"}, "label": {"type": "string"}}}}}}
    lines = []
    for c in communities:
        if c["size"] < 3:
            continue
        names = [f"{G.nodes[m]['canonical_name']} ({G.nodes[m]['type']})" for m in c["top_nodes"]]
        lines.append(f"{c['id']}: " + "; ".join(names))
    if not lines:
        return
    system = "You name clusters in a knowledge graph of New Testament, classics and ancient-economy scholarship. Give each cluster a label of at most 6 words based only on the listed members."
    try:
        r = be.complete_json(system, "\n".join(lines), schema, {"purpose": "community-labels"})
    except Exception as exc:
        eprint(f"  ! LLM labelling failed ({exc}); keeping degree-based labels")
        return
    by_id = {c["id"]: c for c in communities}
    for item in r.data.get("labels", []):
        if item["id"] in by_id:
            by_id[item["id"]]["llm_label"] = item["label"]
