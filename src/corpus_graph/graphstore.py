"""Read-only access to out/graph.json: indices, name matching and provenance formatting.

Shared by `report`, `query`, `path` and `explain`. No model calls.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz, process

from .loci import LocusParser, locus_from_attrs
from .util import fold, read_json, read_yaml

STOPWORDS = set(
    """a an and are as at be between by can did do does for from has have how i in is it its me of on or
    what which who whom whose why with about against does was were the this that these those there their
    them they than then into over under any all some show tell find list give explain who's where when
    disagree disagrees agreement agree argue argues argued claim claims cite cites cited mention mentions
    passage passages text texts scholar scholars article articles document documents graph corpus say says
    said use used using connect connected connection related relation relationship between both other""".split()
)


@dataclass
class Match:
    span: str
    node_ids: list[str]
    via: str  # "id" | "locus" | "name" | "alias" | "surname" | "fuzzy"


@dataclass
class MatchResult:
    matches: list[Match] = field(default_factory=list)
    ambiguous: list[Match] = field(default_factory=list)

    @property
    def node_ids(self) -> list[str]:
        out = []
        for m in self.matches:
            for n in m.node_ids:
                if n not in out:
                    out.append(n)
        return out


class GraphStore:
    def __init__(self, graph: dict, root: Path, canon_dir: Path | None = None):
        self.g = graph
        self.root = root
        self.nodes = {n["id"]: n for n in graph["nodes"]}
        self.docs = {d["hash"]: d for d in graph["documents"]}
        self.docs_by_id = {d["id"]: d for d in graph["documents"]}
        self.out_edges: dict[str, list[dict]] = defaultdict(list)
        self.in_edges: dict[str, list[dict]] = defaultdict(list)
        for e in graph["edges"]:
            self.out_edges[e["source"]].append(e)
            self.in_edges[e["target"]].append(e)
        self.communities = {c["id"]: c for c in graph.get("communities", [])}
        extra = []
        if canon_dir is not None:
            pt = read_yaml(canon_dir / "primary_texts.yaml", {}) or {}
            extra = pt.get("works") or [] if isinstance(pt, dict) else []
        self.parser = LocusParser(extra_works=extra)
        self.loci = {}
        for n in graph["nodes"]:
            if n["type"] == "PrimaryText" and n["attrs"].get("work"):
                l = locus_from_attrs(n["attrs"], self.parser)
                if l:
                    self.loci[n["id"]] = l
        self.name_index: dict[str, set[str]] = defaultdict(set)
        self.canon_index: dict[str, set[str]] = defaultdict(set)
        self.surname_index: dict[str, set[str]] = defaultdict(set)
        for n in graph["nodes"]:
            k = fold(n["canonical_name"])
            self.canon_index[k].add(n["id"])
            self.name_index[k].add(n["id"])
            for a in n.get("aliases", []):
                self.name_index[fold(a)].add(n["id"])
            if n["type"] == "Scholar" and n["attrs"].get("surname"):
                self.surname_index[fold(n["attrs"]["surname"])].add(n["id"])
        self.authored: dict[str, set[str]] = defaultdict(set)  # scholar id -> doc hashes they wrote
        for e in graph["edges"]:
            if e["relation"] == "written_by" and e["source"] in self.docs_by_id:
                self.authored[e["target"]].add(self.docs_by_id[e["source"]]["hash"])
        self._fuzzy_names = [(fold(n["canonical_name"]), n["id"]) for n in graph["nodes"] if len(n["canonical_name"]) >= 6]

    @classmethod
    def load(cls, ws) -> "GraphStore":
        g = read_json(ws.graph_path)
        if not g:
            raise FileNotFoundError(f"{ws.graph_path} not found: run `corpus-graph build`")
        return cls(g, ws.root, ws.canon)

    # ------------------------------------------------------------------ neighbourhood
    def edges_of(self, nid: str) -> list[tuple[str, dict]]:
        """(direction, edge) with direction 'out' or 'in'."""
        return [("out", e) for e in self.out_edges.get(nid, [])] + [("in", e) for e in self.in_edges.get(nid, [])]

    def neighbours(self, nid: str) -> set[str]:
        return {e["target"] for e in self.out_edges.get(nid, [])} | {e["source"] for e in self.in_edges.get(nid, [])}

    def docs_of(self, nid: str) -> set[str]:
        n = self.nodes[nid]
        if n["type"] == "Document":
            return {n["attrs"]["hash"]}
        return {p["doc"] for p in n["provenance"]}

    # ------------------------------------------------------------------ provenance choice
    def _name_tokens(self, nid: str) -> set[str]:
        n = self.nodes.get(nid)
        if not n:
            return set()
        names = [n["canonical_name"], *n.get("aliases", [])]
        if n["type"] == "Scholar" and n["attrs"].get("surname"):
            names.append(n["attrs"]["surname"])
        toks = set()
        for x in names:
            toks |= {t for t in re.findall(r"\w+", fold(x)) if len(t) > 3 and t not in STOPWORDS}
        return toks

    def ranked_provenance(self, e: dict, doc: str | None = None) -> list[dict]:
        """Edge provenance, best first: quotes from the claimant's own article, then quotes naming both ends."""
        src, tgt = self._name_tokens(e["source"]), self._name_tokens(e["target"])
        own = self.authored.get(e["source"], set())

        def score(p):
            q = set(re.findall(r"\w+", fold(p["quote"])))
            return ((doc is not None and p["doc"] == doc) * 4 + (p["doc"] in own) * 3
                    + bool(q & src) + bool(q & tgt) + (p.get("match") == "exact") * 0.1)

        return sorted(e["provenance"], key=score, reverse=True)

    # ------------------------------------------------------------------ formatting
    def doc_label(self, h: str) -> str:
        d = self.docs.get(h)
        if not d:
            return h[:12]
        authors = d.get("authors") or []
        who = ", ".join(a.split()[-1] for a in authors[:2]) + (" et al." if len(authors) > 2 else "")
        bits = [f"*{d['title']}*"]
        if who or d.get("year"):
            bits.append(f"({' '.join(x for x in [who, str(d.get('year') or '')] if x)})")
        return " ".join(bits)

    def cite(self, p: dict, md_prefix: str = "") -> str:
        page = f", p. {p['page']}" if p.get("page") else ""
        d = self.docs.get(p["doc"], {})
        path = d.get("md_path", f"work/md/{p['doc']}.md")
        loc = f"{md_prefix}{path}:{p.get('line')}" if p.get("line") else f"{md_prefix}{path}"
        return f"> \"{p['quote']}\"\n> — {self.doc_label(p['doc'])}{page} · `{loc}`"

    def name(self, nid: str) -> str:
        n = self.nodes.get(nid)
        return n["canonical_name"] if n else nid

    def ref(self, nid: str) -> str:
        n = self.nodes.get(nid)
        if not n:
            return f"`{nid}`"
        flag = " ⚠unresolved" if n.get("unresolved") else ""
        return f"**{n['canonical_name']}** (`{nid}`, {n['type']}{flag})"

    # ------------------------------------------------------------------ matching
    def match(self, text: str, types: set[str] | None = None) -> MatchResult:
        res = MatchResult()
        consumed: list[tuple[int, int]] = []

        def ok_type(nid):
            return types is None or self.nodes[nid]["type"] in types

        # explicit node ids
        for m in re.finditer(r"\b(?:doc|scholar|author|pt|concept|object|place|period|arg):[\w.:\-]+", text):
            nid = m.group(0).rstrip(".,;:")
            if nid in self.nodes and ok_type(nid):
                res.matches.append(Match(nid, [nid], "id"))
                consumed.append((m.start(), m.end()))
        # loci ("Mark 12" matches every PrimaryText within Mark 12)
        for start, end, loci in self.parser.find(text, "en", allow_chapter_only=True):
            if any(a <= start < b for a, b in consumed):
                continue
            hits = [nid for nid, l in self.loci.items() if any(q.overlaps(l) for q in loci)]
            consumed.append((start, end))
            if hits and (types is None or "PrimaryText" in types):
                hits.sort(key=lambda n: self.loci[n].interval())
                res.matches.append(Match(text[start:end], hits, "locus"))
        # names and aliases, longest n-gram first
        years = {int(y) for y in re.findall(r"\b(1[5-9]\d\d|20\d\d)\b", text)}
        toks = [(m.start(), m.end()) for m in re.finditer(r"[^\s,;:!?()\"“”«»]+", text)]
        used = [False] * len(toks)
        for i, (a, _) in enumerate(toks):
            if any(x <= a < y for x, y in consumed):
                used[i] = True
        for n in range(min(8, len(toks)), 0, -1):
            for i in range(len(toks) - n + 1):
                if any(used[i:i + n]):
                    continue
                s, e = toks[i][0], toks[i + n - 1][1]
                span = text[s:e].strip(".'’")
                key = fold(span)
                key2 = re.sub(r"'s$", "", key)
                if n == 1 and (key in STOPWORDS or len(key) < 3):
                    continue
                hits = [h for h in (self.name_index.get(key) or self.name_index.get(key2) or set()) if ok_type(h)]
                via = "name"
                if not hits and n == 1:
                    hits = [h for h in self.surname_index.get(key2, set()) if ok_type(h)]
                    via = "surname"
                if not hits:
                    continue
                for j in range(i, i + n):
                    used[j] = True
                hits = sorted(hits)
                exact = [h for h in hits if h in self.canon_index.get(key, set()) or h in self.canon_index.get(key2, set())]
                by_type = defaultdict(list)
                for h in hits:
                    by_type[self.nodes[h]["type"]].append(h)
                if any(len(v) > 1 for v in by_type.values()) and len(exact) != 1:
                    # several nodes of one type share this name: try publication years for scholars
                    scholars = by_type.get("Scholar", [])
                    if years and scholars:
                        pick = [h for h in scholars if set(self.nodes[h]["attrs"].get("years") or []) & years]
                        if len(pick) == 1:
                            res.matches.append(Match(span, pick, "surname+year"))
                            continue
                    res.ambiguous.append(Match(span, hits, via))
                    continue
                res.matches.append(Match(span, exact if len(exact) == 1 else hits, via))
        # last resort: fuzzy match of the whole question against long names
        if not res.matches and not res.ambiguous and self._fuzzy_names:
            q = fold(text)
            found = process.extract(q, [n for n, _ in self._fuzzy_names], scorer=fuzz.partial_ratio, score_cutoff=92, limit=5)
            ids = [self._fuzzy_names[i][1] for _, _, i in found]
            ids = [i for i in ids if ok_type(i)]
            if ids:
                res.matches.append(Match(text, ids[:3], "fuzzy"))
        return res

    def resolve_one(self, text: str) -> tuple[str | None, list[str]]:
        """Resolve a node reference for path/explain: (node id, candidates if ambiguous)."""
        text = text.strip()
        if text in self.nodes:
            return text, []
        exact = self.canon_index.get(fold(text), set())
        if len(exact) == 1:
            return next(iter(exact)), []
        r = self.match(text)
        if r.ambiguous:
            return None, sorted({h for m in r.ambiguous for h in m.node_ids})
        ids = r.node_ids
        if len(ids) == 1:
            return ids[0], []
        if len(ids) > 1:
            return None, ids
        return None, []
