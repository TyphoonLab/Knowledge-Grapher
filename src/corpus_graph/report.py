"""Stage 5: out/REPORT.md. Every section is computed from the graph; every claim cites provenance."""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from itertools import combinations

import networkx as nx

from .build import to_networkx, undirected_simple
from .graphstore import GraphStore
from .util import Workspace, eprint, write_text

TOP_K = 5
MD_PREFIX = "../"  # REPORT.md lives in out/; paths in graph.json are relative to the workspace root


class Report:
    def __init__(self, store: GraphStore):
        self.s = store
        self.G = to_networkx(store.g)
        self.U = undirected_simple(self.G)
        n = self.U.number_of_nodes()
        k = None if n <= 2500 else 500  # sampled betweenness above 2,500 nodes
        self.btw = nx.betweenness_centrality(self.U, k=k, normalized=True, weight=None, seed=7) if n else {}
        self.deg = dict(self.U.degree())
        self.comm = {nid: nd.get("community") for nid, nd in self.G.nodes(data=True)}
        self.lines: list[str] = []
        self.findings: dict[str, list] = {}

    # ------------------------------------------------------------------ helpers
    def w(self, s: str = "") -> None:
        self.lines.append(s)

    def quotes_for_node(self, nid: str, k: int = 1, docs: set[str] | None = None) -> list[str]:
        n = self.s.nodes[nid]
        provs = [p for p in n["provenance"] if docs is None or p["doc"] in docs]
        if not provs:  # Document nodes: quote their strongest extracted edges
            es = [e for _, e in self.s.edges_of(nid) if not e.get("derived")] or [e for _, e in self.s.edges_of(nid)]
            es.sort(key=lambda e: -len(e["provenance"]))
            provs = [e["provenance"][0] for e in es if e["provenance"]]
        out, seen = [], set()
        for p in provs:
            if p["doc"] in seen and len(provs) > k:
                continue
            seen.add(p["doc"])
            out.append(self.s.cite(p, MD_PREFIX))
            if len(out) >= k:
                break
        return out

    def edge_quote(self, e: dict, doc: str | None = None) -> str:
        return self.s.cite(self.s.ranked_provenance(e, doc)[0], MD_PREFIX)

    def comm_label(self, cid) -> str:
        c = self.s.communities.get(cid)
        return f"C{cid} ({c.get('llm_label') or c['label']})" if c else f"C{cid}"

    # ------------------------------------------------------------------ sections
    def hubs(self) -> None:
        self.w("## (a) Hub nodes")
        self.w("")
        self.w("Degree counts distinct neighbours in the undirected graph; betweenness is normalised "
               "shortest-path betweenness" + (" (sampled, k=500)" if self.U.number_of_nodes() > 2500 else "") + ".")
        by_type = defaultdict(list)
        for nid, n in self.s.nodes.items():
            by_type[n["type"]].append(nid)
        hubs = []
        for t in sorted(by_type):
            ids = by_type[t]
            top_d = sorted(ids, key=lambda i: (-self.deg.get(i, 0), self.s.name(i)))[:TOP_K]
            top_b = sorted(ids, key=lambda i: (-self.btw.get(i, 0), self.s.name(i)))[:TOP_K]
            self.w("")
            self.w(f"### {t}")
            self.w("")
            self.w("| by degree | deg | by betweenness | betw. |")
            self.w("|---|---:|---|---:|")
            for a, b in zip(top_d, top_b):
                self.w(f"| {self.s.name(a)} (`{a}`) | {self.deg.get(a, 0)} | {self.s.name(b)} (`{b}`) | {self.btw.get(b, 0):.3f} |")
            self.w("")
            for nid in dict.fromkeys(top_d[:2] + top_b[:1]):
                if self.deg.get(nid, 0) == 0:
                    continue
                self.w(f"- {self.s.ref(nid)}: degree {self.deg.get(nid, 0)}, betweenness {self.btw.get(nid, 0):.3f}, "
                       f"in {len(self.s.docs_of(nid))} document(s).")
                for q in self.quotes_for_node(nid, 1):
                    self.w("")
                    self.w(_indent(q))
                self.w("")
                hubs.append(nid)
        self.findings["hubs"] = hubs

    def bridges(self) -> None:
        self.w("## (b) Bridge nodes")
        self.w("")
        self.w("Entities (documents excluded) whose betweenness is high relative to their degree and whose "
               "neighbours fall in two or more communities. Score = betweenness × participation coefficient ÷ log(1+degree).")
        self.w("")
        import math

        rows = []
        for nid, n in self.s.nodes.items():
            if n["type"] == "Document" or self.deg.get(nid, 0) < 2:
                continue
            nbrs = [m for m in self.U.neighbors(nid) if self.s.nodes[m]["type"] != "Document"]
            cs = Counter(self.comm.get(m) for m in nbrs)
            if len(cs) < 2:
                continue
            k = sum(cs.values())
            pc = 1 - sum((v / k) ** 2 for v in cs.values())
            score = self.btw.get(nid, 0) * pc / math.log(1 + self.deg[nid])
            if score > 0:
                rows.append((score, nid, cs, nbrs))
        rows.sort(key=lambda r: (-r[0], r[1]))
        if not rows:
            self.w("_No node connects two or more communities through non-document neighbours._")
            self.w("")
        for score, nid, cs, nbrs in rows[:10]:
            self.w(f"- {self.s.ref(nid)}: score {score:.4f}, betweenness {self.btw.get(nid, 0):.3f}, degree {self.deg[nid]}; "
                   f"connects {', '.join(self.comm_label(c) for c in sorted(cs, key=lambda c: -cs[c]))}.")
            # one quote from an edge into each of the two largest communities
            shown = 0
            for c in sorted(cs, key=lambda c: -cs[c])[:2]:
                for dirn, e in self.s.edges_of(nid):
                    other = e["target"] if dirn == "out" else e["source"]
                    if self.comm.get(other) == c and other in nbrs and e["provenance"]:
                        rel = f"{self.s.name(e['source'])} —{e['relation']}→ {self.s.name(e['target'])}"
                        self.w("")
                        self.w(f"  {rel}:")
                        self.w("")
                        self.w(_indent(self.edge_quote(e)))
                        shown += 1
                        break
            self.w("")
        self.findings["bridges"] = [r[1] for r in rows[:10]]

    def cocitation(self) -> None:
        self.w("## (c) Co-citation surprises")
        self.w("")
        n_docs = len(self.s.docs)
        surprises = []
        for label, ntype, docs_fn in (
            ("primary texts", "PrimaryText", self._cited_docs),
            ("scholars", "Scholar", self._mention_docs),
        ):
            cited = {nid: docs_fn(nid) for nid, n in self.s.nodes.items() if n["type"] == ntype}
            cited = {k: v for k, v in cited.items() if v}
            counts = sorted(len(v) for v in cited.values())
            if not counts:
                continue
            p75 = counts[int(0.75 * (len(counts) - 1))]
            heavy = max(2, p75)
            hv = [k for k, v in cited.items() if len(v) >= heavy]
            rows = []
            for a, b in combinations(sorted(hv), 2):
                both = cited[a] & cited[b]
                if not 1 <= len(both) <= 2:
                    continue
                ca, cb = len(cited[a]), len(cited[b])
                if len(both) >= min(ca, cb):
                    continue  # not "elsewhere": every citation of one is a co-citation
                expected = ca * cb / max(1, n_docs)
                rows.append((expected - len(both), min(ca, cb) / len(both), a, b, both, ca, cb, expected))
            rows.sort(key=lambda r: (-r[0], -r[1], r[2], r[3]))
            per_node: Counter = Counter()
            kept = []
            for r in rows:  # no node in more than two listed pairs
                if per_node[r[2]] < 2 and per_node[r[3]] < 2:
                    kept.append(r)
                    per_node[r[2]] += 1
                    per_node[r[3]] += 1
            rows = kept
            self.w(f"### {label.capitalize()}")
            self.w("")
            self.w(f"Pairs each cited in at least {heavy} documents (≥ 75th percentile, minimum 2) but cited together in only one or two.")
            self.w("")
            if not rows:
                self.w(f"_None at this corpus size ({n_docs} documents)._")
                self.w("")
            for _, _ratio, a, b, both, ca, cb, exp in rows[:10]:
                self.w(f"- {self.s.ref(a)} ({ca} docs) and {self.s.ref(b)} ({cb} docs): together only in "
                       f"{', '.join(self.s.doc_label(h) for h in sorted(both))} (expected {exp:.1f} if independent).")
                for h in sorted(both):
                    for nid in (a, b):
                        q = self.quotes_for_node(nid, 1, docs={h})
                        if q:
                            self.w("")
                            self.w(_indent(q[0]))
                self.w("")
                surprises.append((a, b))
        self.findings["cocitation"] = surprises

    def _cited_docs(self, nid: str) -> set[str]:
        return {self.s.docs_by_id[e["source"]]["hash"] for e in self.s.in_edges.get(nid, [])
                if e["relation"] == "cites" and e["source"] in self.s.docs_by_id}

    def _mention_docs(self, nid: str) -> set[str]:
        own = {self.s.docs_by_id[e["source"]]["hash"] for e in self.s.in_edges.get(nid, [])
               if e["relation"] == "written_by" and e["source"] in self.s.docs_by_id}
        return self.s.docs_of(nid) - own

    def contested(self) -> None:
        self.w("## (d) Contested passages")
        self.w("")
        self.w("Primary texts grounding an Argument that one scholar argues and another disputes, or grounding "
               "rival Arguments whose proponents are recorded as disagreeing.")
        self.w("")
        found = []
        for pt, n in sorted(self.s.nodes.items()):
            if n["type"] != "PrimaryText":
                continue
            args = [e for e in self.s.in_edges.get(pt, []) if e["relation"] == "grounded_in"]
            if not args:
                continue
            pro: dict[str, list] = defaultdict(list)  # arg -> [(scholar, edge)]
            con: dict[str, list] = defaultdict(list)
            for g in args:
                a = g["source"]
                for e in self.s.in_edges.get(a, []):
                    if e["relation"] == "argues":
                        pro[a].append((e["source"], e))
                    elif e["relation"] == "disputes":
                        con[a].append((e["source"], e))
            conflicts = []
            for a in pro:
                if con.get(a):
                    conflicts.append(("argued and disputed", a, pro[a], con[a], None))
            arg_ids = sorted(pro)
            for a1, a2 in combinations(arg_ids, 2):
                for s1, _ in pro[a1]:
                    for s2, _ in pro[a2]:
                        if s1 == s2:
                            continue
                        dis = [e for e in self.s.out_edges.get(s1, []) + self.s.out_edges.get(s2, [])
                               if e["relation"] == "disagrees_with" and {e["source"], e["target"]} == {s1, s2}]
                        if dis:
                            conflicts.append(("rival arguments", a1, pro[a1], pro[a2], (a2, dis[0])))
            if not conflicts:
                continue
            direct = [c for c in conflicts if c[0] == "argued and disputed"]
            conflicts = direct + [c for c in conflicts if c[0] != "argued and disputed"][: max(0, 3 - len(direct))]
            found.append(pt)
            self.w(f"### {n['canonical_name']} (`{pt}`)")
            self.w("")
            seen = set()
            for kind, a, side1, side2, rival in conflicts:
                key = (kind, a, rival[0] if rival else None)
                if key in seen:
                    continue
                seen.add(key)
                grounding = next(g for g in args if g["source"] == a)
                if kind == "argued and disputed":
                    self.w(f"- **{self.s.name(a)}** (`{a}`)")
                    for s, e in side1:
                        self.w(f"  - argued by {self.s.ref(s)}")
                        self.w("")
                        self.w(_indent(self.edge_quote(e), 4))
                    for s, e in side2:
                        self.w(f"  - disputed by {self.s.ref(s)}")
                        self.w("")
                        self.w(_indent(self.edge_quote(e), 4))
                else:
                    a2, dis = rival
                    self.w(f"- **{self.s.name(a)}** vs **{self.s.name(a2)}**")
                    for s, e in side1[:1]:
                        self.w(f"  - {self.s.ref(s)} argues the first")
                        self.w("")
                        self.w(_indent(self.edge_quote(e), 4))
                    for s, e in side2[:1]:
                        self.w(f"  - {self.s.ref(s)} argues the second")
                        self.w("")
                        self.w(_indent(self.edge_quote(e), 4))
                    self.w(f"  - recorded disagreement: {self.s.name(dis['source'])} —disagrees_with→ {self.s.name(dis['target'])}")
                    self.w("")
                    self.w(_indent(self.edge_quote(dis), 4))
                self.w(f"  - grounding in {n['canonical_name']}:")
                self.w("")
                self.w(_indent(self.edge_quote(grounding), 4))
                self.w("")
        if not found:
            self.w("_No contested passages recorded._")
            self.w("")
        # disagreements not tied to any passage, for completeness
        loose = []
        for e in self.s.g["edges"]:
            if e["relation"] == "disagrees_with":
                loose.append(e)
        if loose:
            self.w("### All recorded scholarly disagreements")
            self.w("")
            for e in loose[:20]:
                self.w(f"- {self.s.ref(e['source'])} —disagrees_with→ {self.s.ref(e['target'])}")
                self.w("")
                self.w(_indent(self.edge_quote(e)))
                self.w("")
        self.findings["contested"] = found
        self.findings["disagreements"] = [(e["source"], e["target"]) for e in loose]

    def orphans(self) -> None:
        self.w("## (e) Orphans and thin spots")
        self.w("")
        single = [nid for nid, n in self.s.nodes.items() if n["type"] == "Concept" and len(self.s.docs_of(nid)) == 1]
        by_doc = defaultdict(list)
        for nid in single:
            by_doc[next(iter(self.s.docs_of(nid)))].append(nid)
        self.w(f"**{len(single)} concept(s) appear in only one document.**")
        self.w("")
        for h in sorted(by_doc, key=lambda h: self.s.doc_label(h)):
            self.w(f"- {self.s.doc_label(h)}:")
            for nid in sorted(by_doc[h], key=self.s.name):
                q = self.quotes_for_node(nid, 1)
                self.w(f"  - {self.s.ref(nid)}")
                if q:
                    self.w("")
                    self.w(_indent(q[0], 4))
                    self.w("")
        isolated = [nid for nid in self.s.nodes if self.deg.get(nid, 0) == 0]
        unresolved = [nid for nid, n in self.s.nodes.items() if n.get("unresolved")]
        self.w("")
        self.w(f"**{len(isolated)} isolated node(s)** (no edges): "
               + (", ".join(f"`{i}`" for i in sorted(isolated)[:30]) + (" …" if len(isolated) > 30 else "") if isolated else "none") + ".")
        self.w("")
        self.w(f"**{len(unresolved)} unresolved node(s)** awaiting curation (see `corpus-graph merges list`): "
               + (", ".join(f"`{i}`" for i in sorted(unresolved)[:30]) + (" …" if len(unresolved) > 30 else "") if unresolved else "none") + ".")
        self.w("")
        self.findings["orphans"] = single

    def questions(self) -> None:
        self.w("## (f) Questions this graph can answer")
        self.w("")
        qs: list[tuple[str, list[str], str]] = []
        f = self.findings
        for pt in f.get("contested", [])[:3]:
            args = [e["source"] for e in self.s.in_edges.get(pt, []) if e["relation"] == "grounded_in"]
            qs.append((f"Who disagrees about {self.s.name(pt)}, and on what grounds?", [pt] + args[:3],
                       f'corpus-graph query "{self.s.name(pt)}"'))
        for a, b in f.get("disagreements", [])[:2]:
            qs.append((f"What exactly does {self.s.name(a)} dispute in {self.s.name(b)}'s work?", [a, b],
                       f'corpus-graph path "{a}" "{b}"'))
        for a, b in f.get("cocitation", [])[:2]:
            qs.append((f"Why are {self.s.name(a)} and {self.s.name(b)} cited together, and only rarely?", [a, b],
                       f'corpus-graph path "{a}" "{b}"'))
        for nid in f.get("bridges", [])[:2]:
            qs.append((f"How does {self.s.name(nid)} connect otherwise separate lines of discussion?", [nid],
                       f'corpus-graph explain "{nid}"'))
        args = sorted((nid for nid, n in self.s.nodes.items() if n["type"] == "Argument"),
                      key=lambda i: -len(self.s.docs_of(i)))
        for a in args[:2]:
            pts = [e["target"] for e in self.s.out_edges.get(a, []) if e["relation"] == "grounded_in"]
            qs.append((f"Which passages ground the claim that {self.s.name(a)}, and who holds it?", [a] + pts[:4],
                       f'corpus-graph explain "{a}"'))
        objs = sorted((nid for nid, n in self.s.nodes.items() if n["type"] == "Object"), key=lambda i: -self.deg.get(i, 0))
        for o in objs[:2]:
            pts = [e["target"] for e in self.s.out_edges.get(o, []) if e["relation"] == "attested_in"]
            qs.append((f"Where is the {self.s.name(o)} attested in the primary texts, and how is that evidence used?", [o] + pts[:4],
                       f'corpus-graph query "{self.s.name(o)}"'))
        for nid in f.get("hubs", []):
            if len(qs) >= 10:
                break
            if self.s.nodes[nid]["type"] in ("Scholar", "Concept"):
                qs.append((f"What does the corpus say about {self.s.name(nid)}?", [nid], f'corpus-graph explain "{nid}"'))
        seen, uniq = set(), []
        for q in qs:
            if q[0] not in seen:
                seen.add(q[0])
                uniq.append(q)
        for i, (q, ids, cmd) in enumerate(uniq[:10], 1):
            self.w(f"{i}. {q}")
            self.w(f"   - nodes: {', '.join(f'`{x}`' for x in dict.fromkeys(ids))}")
            self.w(f"   - try: `{cmd}`")
        self.w("")

    def render(self) -> str:
        g = self.s.g
        m = g["meta"]
        self.w("# corpus-graph report")
        self.w("")
        self.w(f"Generated {time.strftime('%Y-%m-%d %H:%M')} from `out/graph.json`: {m['counts']['documents']} documents, "
               f"{m['counts']['nodes']} nodes, {m['counts']['edges']} edges, {m['counts']['communities']} communities "
               f"({m['community_algorithm']}).")
        self.w("")
        self.w("Every figure below is computed from the graph. Quotes are verbatim provenance captured at extraction; "
               "the path after each quote is `markdown file:line`.")
        self.w("")
        self.w("### Communities")
        self.w("")
        for c in g["communities"][:15]:
            self.w(f"- C{c['id']} ({c['size']} nodes): {c.get('llm_label') or c['label']}")
        self.w("")
        for section in (self.hubs, self.bridges, self.cocitation, self.contested, self.orphans, self.questions):
            section()
        return "\n".join(self.lines).rstrip() + "\n"


def _indent(block: str, n: int = 2) -> str:
    pad = " " * n
    return "\n".join(pad + line for line in block.splitlines())


def run(ws: Workspace, quiet: bool = False) -> str:
    store = GraphStore.load(ws)
    text = Report(store).render()
    write_text(ws.out / "REPORT.md", text)
    if not quiet:
        eprint(f"report: wrote {ws.out / 'REPORT.md'}")
    return text
