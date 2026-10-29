"""Stage 3: merge extracted entities across documents into canonical nodes.

Resolution order for every extracted node:
  1. exact canonical-name match in the canon/ registry for its type
  2. alias match in that registry
  3. deterministic normalisation
       PrimaryText  locus parsing ("Mk 6.37", "Mark 6:37", "Mc 6,37" -> Mark 6:37)
       Scholar      surname + first initial; surname-only references are attached by
                    publication year when several scholars share the surname
       others       case/accent/article/plural folding, plus "name of one is an alias of the other"
  4. fuzzy matching (optionally judged by an LLM) -> work/proposed_merges.json, never applied
     until approved (`corpus-graph merges approve`, recorded in canon/merges.yaml)
Items that cannot be identified stay as their own nodes with `unresolved: true`.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz, process

from .canon import Canon, Entry
from .chunking import ParsedMarkdown, QuoteLocator
from .extract import load_extractions
from .loci import Locus
from .schema import Schema
from .util import Workspace, eprint, fold, read_json, sha256_text, short_hash, slug, write_json, write_yaml

PARTICLES = {"von", "van", "de", "der", "den", "di", "da", "del", "della", "du", "le", "la", "ter", "ten", "zu", "dos", "das", "vom"}
ARTICLES = re.compile(r"^(the|a|an|der|die|das|le|la|les|l'|il|lo|gli|i|un|une|ein|eine)\s+", re.I)
FUZZY_CUTOFF = {"Argument": 78, "default": 88}
FUZZY_TYPES = ("Concept", "Object", "Place", "Period", "Argument", "AncientAuthor", "PrimaryText")
MAX_PROVENANCE_PER_EDGE = 50


# =========================================================================== data model


@dataclass
class Raw:
    doc: str
    chunk: str
    local: str
    type: str
    name: str
    aliases: list[str]
    item: dict
    prov: dict
    lang: str = "en"
    doc_year: int | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.doc, self.chunk, self.local)


@dataclass
class Cluster:
    id: str
    type: str
    name: str = ""
    aliases: set = field(default_factory=set)
    provenance: list = field(default_factory=list)
    method: str = ""
    unresolved: bool = False
    registry: bool = False
    attrs: dict = field(default_factory=dict)
    names: Counter = field(default_factory=Counter)
    notes: list = field(default_factory=list)

    def absorb(self, raw: Raw) -> None:
        self.names[raw.name] += 1
        self.aliases.update(a for a in raw.aliases if a)
        if raw.item.get("citation"):
            self.aliases.add(raw.item["citation"])
        self.provenance.append(raw.prov)
        if raw.item.get("note"):
            self.notes.append(raw.item["note"])


class UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, into: str) -> None:
        ra, rb = self.find(a), self.find(into)
        if ra != rb:
            self.parent[ra] = rb


# =========================================================================== helpers


def split_name(name: str, surname_hint: str | None, given_hint: str | None) -> tuple[str, str]:
    name = re.sub(r"\(?\b(1[5-9]\d\d|20\d\d)[a-z]?\b\)?", "", name)  # "Holt (1987)"
    name = re.sub(r"\s+", " ", name).strip(" ,;")
    if surname_hint:
        surname = surname_hint.strip()
        given = (given_hint or "").strip()
        if not given:
            rest = name.replace(surname, "").strip(" ,")
            given = rest
        return surname, given
    if "," in name:
        s, g = name.split(",", 1)
        return s.strip(), g.strip()
    toks = name.split()
    if len(toks) == 1:
        return toks[0], (given_hint or "").strip()
    i = len(toks) - 1
    while i > 0 and toks[i - 1].lower() in PARTICLES:
        i -= 1
    return " ".join(toks[i:]), (given_hint or " ".join(toks[:i])).strip()


def first_initial(given: str) -> str | None:
    m = re.search(r"[^\W\d_]", given or "")
    return fold(m.group(0)) if m else None


def full_first_name(given: str) -> str | None:
    tok = (given or "").split()
    if tok and len(tok[0].rstrip(".")) > 1 and not tok[0].endswith("."):
        return fold(tok[0])
    return None


def norm_key(ntype: str, name: str, canon: Canon, lang: str) -> str:
    s = name
    if ntype == "Argument":
        # canonicalise loci inside a claim so "Mt 20,2" and "Matt 20:2" phrasings coincide
        found = canon.parser.find(s, lang)
        for start, end, loci in reversed(found):
            s = s[:start] + "; ".join(l.canonical_name for l in loci) + s[end:]
    s = fold(s)
    s = ARTICLES.sub("", s)
    s = re.sub(r"[.,;:!?]+$", "", s).strip()
    if ntype == "Object" and re.match(r"^(p|o|sb|bgu|cil|ig|igrr|ogis|seg|cij|ciip|cpj)\b", s):
        s = re.sub(r"\.\s+", ".", s)  # "P. Oxy. 1.37" == "P.Oxy. 1.37"
    elif ntype in ("Concept", "Object", "Place", "Period"):
        words = s.split()
        if words:
            w = words[-1]
            if len(w) > 4 and w.endswith("ies"):
                words[-1] = w[:-3] + "y"
            elif len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is", "'s")):
                words[-1] = w[:-1]
            s = " ".join(words)
    return s


def best_name(c: Cluster) -> str:
    if not c.names:
        return c.name
    top = c.names.most_common()
    best = max(n for _, n in top)
    cands = [name for name, n in top if n == best]
    return sorted(cands, key=lambda x: (len(x), x))[0]


# =========================================================================== main


def run(ws: Workspace, llm_merge: bool = False, backend: str = "claude-cli", model: str = "sonnet",
        fixtures: Path | None = None, quiet: bool = False) -> dict:
    schema = Schema.load(ws.canon)
    canon = Canon(ws.canon, schema)
    ext = load_extractions(ws)
    if not ext:
        eprint("resolve: nothing extracted yet (run `corpus-graph extract`)")
        return {}
    r = Resolver(ws, schema, canon, ext)
    r.collect()
    r.resolve_all()
    r.harvest_decisions()
    r.apply_merges()
    r.propose_fuzzy()
    if llm_merge:
        r.judge_with_llm(backend, model, fixtures)
    r.finalise_clusters()
    r.build_edges()
    out = r.write()
    if not quiet:
        s = out["stats"]
        eprint(
            f"resolve: {s['raw_nodes']} extracted nodes -> {s['nodes']} canonical nodes "
            f"({s['unresolved']} unresolved), {s['edges']} edges; "
            f"{s['proposals_pending']} merge proposal(s) pending, {s['edge_rejected']} edge(s) rejected"
        )
        if s["proposals_pending"]:
            eprint("  review with `corpus-graph merges list`, then `corpus-graph merges approve <id>`")
    return out


class Resolver:
    def __init__(self, ws: Workspace, schema: Schema, canon: Canon, ext: dict):
        self.ws, self.schema, self.canon, self.ext = ws, schema, canon, ext
        self.documents: dict[str, dict] = {}
        self.raws: list[Raw] = []
        self.raw_edges: list[dict] = []
        self.clusters: dict[str, Cluster] = {}
        self.assign: dict[tuple, list[str]] = {}
        self.uf = UnionFind()
        self.proposals: dict[str, dict] = {}
        self.edge_rejected: list[dict] = []
        self.schema_proposals: list[dict] = []
        self.old_proposals = {p["id"]: p for p in (read_json(ws.proposed_merges, {}) or {}).get("proposals", [])}

    # ------------------------------------------------------------------ collection
    def collect(self) -> None:
        for h, e in self.ext.items():
            meta = read_json(self.ws.md_dir / f"{h}.meta.json", {}) or {}
            llm_meta = next((c.get("document_meta") for c in e["chunks"] if c.get("document_meta")), None) or {}
            doc = self._document(h, meta, llm_meta)
            self.documents[h] = doc
            for c in e["chunks"]:
                for n in c["nodes"]:
                    self.raws.append(Raw(h, c["chunk"]["id"], n["local_id"], n["type"], n["name"], n.get("aliases", []),
                                         n, n["provenance"], doc["language"], doc.get("year")))
                for ed in c["edges"]:
                    self.raw_edges.append({**ed, "doc": h, "chunk": c["chunk"]["id"]})
                for sp in c.get("schema_proposals", []):
                    self.schema_proposals.append({**sp, "doc": h})
            # the document's authors, from metadata, so written_by exists even if the LLM missed it
            md = ParsedMarkdown.load(self.ws.md_dir / f"{h}.md")
            loc = QuoteLocator(md)
            for i, a in enumerate(doc.get("authors") or []):
                where = loc.locate(a)
                prov = {"doc": h, "chunk": "meta", "quote": a, "match": "metadata",
                        "line": where[0] if where else 1, "page": where[1] if where else None}
                self.raws.append(Raw(h, "meta", f"author{i}", "Scholar", a, [],
                                     {"is_document_author": True, "cited_years": [doc["year"]] if doc.get("year") else []},
                                     prov, doc["language"], doc.get("year")))
                self.raw_edges.append({"source": "DOC", "target": f"author{i}", "relation": "written_by",
                                       "confidence": "extracted", "provenance": prov, "doc": h, "chunk": "meta"})

    def _document(self, h: str, meta: dict, llm_meta: dict) -> dict:
        src = meta.get("source_path", "")
        ov = self.canon.documents.get(src) or self.canon.documents.get(Path(src).name) or self.canon.documents.get(h) or {}
        title = ov.get("title") or meta.get("title")
        if (not title or title == Path(src).stem) and llm_meta.get("title"):
            title = llm_meta["title"]
        authors = ov.get("authors") or meta.get("authors") or llm_meta.get("authors") or []
        year = ov.get("year") or meta.get("year") or llm_meta.get("year") or meta.get("year_guess")
        try:
            year = int(str(year)[:4]) if year else None
        except ValueError:
            year = None
        return {
            "id": f"doc:{short_hash(h)}",
            "hash": h,
            "title": title or src,
            "authors": authors,
            "year": year,
            "venue": ov.get("venue") or meta.get("venue") or llm_meta.get("venue"),
            "pages": meta.get("pages"),
            "language": meta.get("language") or "en",
            "source_path": src,
            "md_path": f"work/md/{h}.md",
            "extractor": meta.get("extractor"),
            "quality": meta.get("quality", {}),
        }

    # ------------------------------------------------------------------ cluster helpers
    def _cluster(self, cid: str, ntype: str, **kw) -> Cluster:
        c = self.clusters.get(cid)
        if c is None:
            c = Cluster(cid, ntype, **kw)
            self.clusters[cid] = c
        else:
            for k, v in kw.items():
                if k == "attrs":
                    c.attrs.update(v)
                elif k == "unresolved":
                    c.unresolved = c.unresolved or v
                elif v and not getattr(c, k):
                    setattr(c, k, v)
        return c

    def _assign(self, raw: Raw, cid: str) -> None:
        self.assign.setdefault(raw.key, []).append(cid)
        self.clusters[cid].absorb(raw)

    def _from_entry(self, e: Entry, method: str) -> Cluster:
        attrs = {}
        if isinstance(e.data.get("locus"), Locus):
            attrs = e.data["locus"].to_attrs()
        elif e.data:
            attrs = {k: v for k, v in e.data.items() if k in ("surname", "given", "years", "note", "source")}
        c = self._cluster(e.id, e.type, name=e.name, method=method, registry=True, attrs=attrs)
        c.aliases.update(e.aliases)
        return c

    # ------------------------------------------------------------------ resolution
    def resolve_all(self) -> None:
        by_type: dict[str, list[Raw]] = defaultdict(list)
        for r in self.raws:
            by_type[r.type].append(r)
        for ntype, raws in by_type.items():
            if ntype == "PrimaryText":
                self._resolve_primary(raws)
            elif ntype == "Scholar":
                self._resolve_scholars(raws)
            else:
                self._resolve_generic(ntype, raws)

    def _registry_match(self, ntype: str, names: list[str]) -> tuple[list[Entry], str]:
        reg = self.canon.registries.get(ntype)
        if not reg:
            return [], ""
        for i, n in enumerate(names):
            hits = reg.lookup(n)
            if hits:
                exact = [e for e in hits if fold(e.name) == fold(n)]
                if exact:
                    return exact, "registry" if i == 0 else "registry-alias"
                return hits, "alias"
        return [], ""

    def _resolve_primary(self, raws: list[Raw]) -> None:
        for r in raws:
            cit = r.item.get("citation") or r.name
            hits, method = self._registry_match("PrimaryText", [cit, r.name, *r.aliases])
            if len(hits) == 1:
                self._assign(r, self._from_entry(hits[0], method).id)
                continue
            loci = self.canon.parser.parse(cit, r.lang) or (self.canon.parser.parse(r.name, r.lang) if r.name != cit else [])
            if loci:
                for l in loci:
                    self._cluster(l.node_id, "PrimaryText", name=l.canonical_name, method="locus-parse", attrs=l.to_attrs())
                    self._assign(r, l.node_id)
                continue
            cid = f"pt:unparsed:{slug(cit)}"
            self._cluster(cid, "PrimaryText", name=cit, method="unparsed", unresolved=True)
            self._assign(r, cid)

    def _resolve_generic(self, ntype: str, raws: list[Raw]) -> None:
        prefix = self.schema.prefix(ntype)
        pending = []
        for r in raws:
            hits, method = self._registry_match(ntype, [r.name, *r.aliases])
            if len(hits) == 1:
                self._assign(r, self._from_entry(hits[0], method).id)
            elif len(hits) > 1:
                cid = f"{prefix}:{slug(norm_key(ntype, r.name, self.canon, r.lang))}"
                self._cluster(cid, ntype, name=r.name, method="registry-ambiguous", unresolved=True)
                self._assign(r, cid)
                self._add_proposal("ambiguous", ntype, [cid] + [self._from_entry(e, "registry").id for e in hits], None, 0)
            else:
                pending.append(r)
        # deterministic normalisation; then "the name of one is an alias of another"
        uf = UnionFind()
        keys = {}
        for r in pending:
            k = norm_key(ntype, r.name, self.canon, r.lang)
            keys[id(r)] = k
            uf.find(k)
        name_keys = set(keys.values())
        for r in pending:
            for a in r.aliases:
                ak = norm_key(ntype, a, self.canon, r.lang)
                if ak in name_keys and ak != keys[id(r)]:
                    uf.union(ak, keys[id(r)])
        for r in pending:
            root = uf.find(keys[id(r)])
            cid = f"{prefix}:{slug(root)}"
            self._cluster(cid, ntype, method="normalized")
            self._assign(r, cid)

    # ------------------------------------------------------------------ scholars
    def _resolve_scholars(self, raws: list[Raw]) -> None:
        reg = self.canon.registries.get("Scholar")
        reg_by_surname: dict[str, list[Entry]] = defaultdict(list)
        for e in reg.entries if reg else []:
            sur = e.data.get("surname") or split_name(e.name, None, None)[0]
            reg_by_surname[fold(sur)].append(e)

        def entry_initial(e: Entry) -> str | None:
            return first_initial(e.data.get("given") or split_name(e.name, e.data.get("surname"), None)[1])

        def years_of(r: Raw) -> set[int]:
            ys = set(r.item.get("cited_years") or [])
            if r.item.get("is_document_author") and r.doc_year:
                ys.add(r.doc_year)
            return ys

        parsed = []
        for r in raws:
            surname, given = split_name(r.name, r.item.get("surname"), r.item.get("given_names"))
            parsed.append((r, surname, given, first_initial(given), full_first_name(given), years_of(r)))

        rest = []
        for r, sur, giv, ini, full, yrs in parsed:
            hits, method = self._registry_match("Scholar", [r.name, *r.aliases])
            if len(hits) == 1:
                self._assign(r, self._from_entry(hits[0], method).id)
                continue
            cands = [e for e in reg_by_surname.get(fold(sur), []) if ini is None or entry_initial(e) in (None, ini)]
            if len(cands) > 1 and yrs:
                by_year = [e for e in cands if set(e.data.get("years") or []) & yrs]
                if len(by_year) == 1:
                    self._assign(r, self._from_entry(by_year[0], "registry-surname+year").id)
                    continue
            if len(cands) == 1 and (ini is not None or not yrs or not cands[0].data.get("years")
                                    or set(cands[0].data.get("years")) & yrs):
                self._assign(r, self._from_entry(cands[0], "registry-surname").id)
                continue
            rest.append((r, sur, giv, ini, full, yrs, cands))

        # corpus-level groups for names with an initial
        groups: dict[tuple, dict] = {}
        for r, sur, giv, ini, full, yrs, _ in rest:
            if ini is None:
                continue
            g = groups.setdefault((fold(sur), ini), {"surname": sur, "fulls": Counter(), "years": set(), "raws": [], "givens": Counter()})
            g["raws"].append((r, full))
            g["years"] |= yrs
            g["givens"][giv] += 1
            if full:
                g["fulls"][full] += 1
        group_ids: dict[tuple, list[tuple[str, set]]] = defaultdict(list)  # (surname, initial) -> [(cid, years)]
        for (fs, ini), g in groups.items():
            fulls = list(g["fulls"])
            if len(fulls) <= 1:
                cid = f"scholar:{slug(g['surname'])}-{ini}"
                given = max(g["givens"], key=lambda x: (len(x), g["givens"][x]))
                self._cluster(cid, "Scholar", name=f"{given} {g['surname']}".strip(), method="surname+initial",
                              attrs={"surname": g["surname"], "given": given, "years": sorted(g["years"])})
                for r, _ in g["raws"]:
                    self._assign(r, cid)
                group_ids[(fs, ini)].append((cid, g["years"]))
            else:  # two different full first names with the same initial: split
                split_ids = {}
                for full in fulls:
                    cid = f"scholar:{slug(g['surname'])}-{slug(full)}"
                    yrs = set()
                    given = full.title()
                    for r, f in g["raws"]:
                        if f == full:
                            yrs |= set(r.item.get("cited_years") or [])
                            given = split_name(r.name, r.item.get("surname"), r.item.get("given_names"))[1] or given
                    self._cluster(cid, "Scholar", name=f"{given} {g['surname']}", method="surname+given",
                                  attrs={"surname": g["surname"], "given": given, "years": sorted(yrs)})
                    split_ids[full] = cid
                    group_ids[(fs, ini)].append((cid, yrs))
                for r, f in g["raws"]:
                    if f:
                        self._assign(r, split_ids[f])
                    else:
                        self._attach_by_year(r, g["surname"], [(c, y) for c, y in group_ids[(fs, ini)]], years_of(r))

        # surname-only references
        for r, sur, _giv, ini, _full, yrs, reg_cands in rest:
            if ini is not None:
                continue
            cands = [(cid, ys) for (fs, _), lst in group_ids.items() if fs == fold(sur) for cid, ys in lst]
            cands += [(self._from_entry(e, "registry").id, set(e.data.get("years") or [])) for e in reg_cands]
            if not cands:
                cid = f"scholar:{slug(sur)}"
                self._cluster(cid, "Scholar", name=sur, method="surname-only", attrs={"surname": sur, "years": []})
                self.clusters[cid].attrs["years"] = sorted(set(self.clusters[cid].attrs.get("years", [])) | yrs)
                self._assign(r, cid)
            elif len(cands) == 1:
                self._assign(r, cands[0][0])
                self.clusters[cands[0][0]].method += "; surname-unique"
            else:
                self._attach_by_year(r, sur, cands, yrs)

    def _attach_by_year(self, r: Raw, surname: str, cands: list[tuple[str, set]], yrs: set) -> None:
        by_year = [c for c, ys in cands if ys & yrs]
        if len(by_year) == 1:
            self._assign(r, by_year[0])
            self.clusters[by_year[0]].notes.append(f"'{r.name}' ({sorted(yrs)}) attached by publication year")
            return
        cid = f"scholar:{slug(surname)}-ambiguous"
        self._cluster(cid, "Scholar", name=f"{surname} (ambiguous)", method="ambiguous-surname", unresolved=True,
                      attrs={"surname": surname})
        self._assign(r, cid)
        self._add_proposal("ambiguous", "Scholar", [cid] + [c for c, _ in cands], None, 0,
                           reason=f"surname '{surname}' matches {len(cands)} scholars; years {sorted(yrs) or 'none'} do not decide")

    # ------------------------------------------------------------------ merges
    def _add_proposal(self, kind: str, ntype: str, members: list[str], into: str | None, score: float, reason: str = "") -> None:
        members = list(dict.fromkeys(members))
        pid = sha256_text(kind + ntype + "|".join(sorted(members)))[:10]
        old = self.old_proposals.get(pid, {})
        self.proposals[pid] = {
            "id": pid,
            "kind": kind,
            "type": ntype,
            "members": members,
            "into": old.get("into") or into,
            "score": round(score, 1),
            "reason": reason,
            "status": old.get("status", "pending"),
            "llm": old.get("llm"),
        }

    def harvest_decisions(self) -> None:
        """Statuses set by hand in work/proposed_merges.json are recorded in canon/merges.yaml."""
        changed = False
        approved_sets = {frozenset(a["members"]) for a in self.canon.approved}
        rejected_sets = {frozenset(a["members"]) for a in self.canon.rejected}
        for p in self.old_proposals.values():
            ms = frozenset(p["members"])
            if p.get("status") == "approved" and ms not in approved_sets:
                into = p.get("into") or p["members"][-1]
                self.canon.approved.append({"type": p["type"], "members": p["members"], "into": into})
                changed = True
            elif p.get("status") == "rejected" and ms not in rejected_sets:
                self.canon.rejected.append({"type": p["type"], "members": p["members"]})
                changed = True
        if changed:
            self.canon.save_merges()

    def apply_merges(self) -> None:
        for m in self.canon.approved:
            into = m.get("into") or m["members"][-1]
            for mem in m["members"]:
                if mem != into:
                    self.uf.union(mem, into)
            if into not in self.clusters:
                # the target may be a registry entry not yet mentioned
                for reg in self.canon.registries.values():
                    for e in reg.entries:
                        if e.id == into:
                            self._from_entry(e, "registry")
        # fold merged clusters together
        for cid in list(self.clusters):
            root = self.uf.find(cid)
            if root != cid and cid in self.clusters:
                src = self.clusters.pop(cid)
                dst = self.clusters.get(root) or self._cluster(root, src.type, name=src.name, method=src.method)
                dst.names.update(src.names)
                dst.aliases |= src.aliases | {src.name, *src.names}
                dst.provenance += src.provenance
                dst.notes += src.notes
                dst.method = (dst.method + "; merged-approved").strip("; ")
                for k, v in src.attrs.items():
                    dst.attrs.setdefault(k, v)
                if src.type == "Scholar" and src.attrs.get("years"):
                    dst.attrs["years"] = sorted(set(dst.attrs.get("years", [])) | set(src.attrs["years"]))
        for k, lst in self.assign.items():
            self.assign[k] = list(dict.fromkeys(self.uf.find(c) for c in lst))
        # an ambiguous scholar resolved by approval is no longer unresolved
        for c in self.clusters.values():
            if "merged-approved" in c.method and c.registry:
                c.unresolved = False

    def propose_fuzzy(self) -> None:
        decided = {frozenset(a["members"]) for a in self.canon.approved + self.canon.rejected}
        by_type: dict[str, list[Cluster]] = defaultdict(list)
        for c in self.clusters.values():
            if c.type in FUZZY_TYPES:
                if c.type == "PrimaryText" and not c.id.startswith("pt:unparsed:"):
                    continue
                by_type[c.type].append(c)
        for ntype, cl in by_type.items():
            if len(cl) < 2:
                continue
            names = [fold(best_name(c) if c.names else c.name) for c in cl]
            scorer = fuzz.token_sort_ratio if ntype == "Argument" else fuzz.WRatio
            cutoff = FUZZY_CUTOFF.get(ntype, FUZZY_CUTOFF["default"])
            mat = process.cdist(names, names, scorer=scorer, score_cutoff=cutoff, workers=-1)
            for i in range(len(cl)):
                for j in range(i + 1, len(cl)):
                    s = float(mat[i][j])
                    if s < cutoff:
                        continue
                    a, b = cl[i], cl[j]
                    if a.registry and b.registry:
                        continue  # two registry entries are distinct by decision
                    if frozenset([a.id, b.id]) in decided:
                        continue
                    if ntype == "PrimaryText" and (a.attrs.get("work") or b.attrs.get("work")):
                        continue
                    into = a.id if (a.registry or len(a.provenance) >= len(b.provenance)) and not b.registry else b.id
                    self._add_proposal("fuzzy", ntype, [a.id, b.id], into, s)
        # keep decisions visible: drop proposals already decided in canon/merges.yaml
        for p in list(self.proposals.values()):
            if frozenset(p["members"]) in decided:
                p["status"] = "approved" if frozenset(p["members"]) in {frozenset(a["members"]) for a in self.canon.approved} else "rejected"

    def judge_with_llm(self, backend: str, model: str, fixtures: Path | None) -> None:
        from .llm import make_backend
        from .prompts import MERGE_SCHEMA, MERGE_SYSTEM

        todo = [p for p in self.proposals.values() if p["status"] == "pending" and p["kind"] == "fuzzy" and not p.get("llm")]
        if not todo:
            return
        be = make_backend(backend, model, self.ws.llm_log, fixtures=fixtures)
        for i in range(0, len(todo), 30):
            batch = todo[i:i + 30]
            lines = []
            for p in batch:
                parts = []
                for mid in p["members"]:
                    c = self.clusters.get(mid)
                    if not c:
                        continue
                    q = "; ".join(f"\"{pv['quote']}\"" for pv in c.provenance[:2])
                    parts.append(f"  - {c.id}: {best_name(c) if c.names else c.name} | aliases: {', '.join(sorted(c.aliases)[:6]) or '-'} | context: {q}")
                lines.append(f"Candidate {p['id']} (type {p['type']}, similarity {p['score']}):\n" + "\n".join(parts))
            user = "Judge each candidate pair.\n\n" + "\n\n".join(lines)
            key = sha256_text("|".join(p["id"] for p in batch))[:12]
            try:
                res = be.complete_json(MERGE_SYSTEM, user, MERGE_SCHEMA, {"purpose": "merge", "key": key})
            except Exception as exc:
                eprint(f"  ! merge judging failed: {exc}")
                return
            for v in res.data.get("verdicts", []):
                if v.get("id") in self.proposals:
                    self.proposals[v["id"]]["llm"] = {"verdict": v["verdict"], "reason": v["reason"], "model": res.model}

    # ------------------------------------------------------------------ finalise
    def finalise_clusters(self) -> None:
        pending_members = defaultdict(list)
        for p in self.proposals.values():
            if p["status"] == "pending":
                for m in p["members"]:
                    pending_members[m].append(p["id"])
        for c in self.clusters.values():
            if not c.registry and c.method not in ("locus-parse",) and c.type != "Scholar" and c.names:
                c.name = best_name(c)
            if not c.name:
                c.name = best_name(c)
            c.aliases = {a for a in c.aliases | set(c.names) if a and a != c.name}
            if pending_members.get(c.id):
                c.unresolved = True
                c.attrs["pending_merges"] = pending_members[c.id]
            # dedupe provenance
            seen, prov = set(), []
            for pv in c.provenance:
                k = (pv["doc"], pv.get("chunk"), pv["quote"])
                if k not in seen:
                    seen.add(k)
                    prov.append(pv)
            c.provenance = prov

    def _ensure_author_node(self, author: str) -> str:
        hits, method = self._registry_match("AncientAuthor", [author])
        if hits:
            return self._from_entry(hits[0], method).id
        cid = f"author:{slug(author)}"
        self._cluster(cid, "AncientAuthor", name=author, method="locus table")
        return cid

    def build_edges(self) -> None:
        node_type = {cid: c.type for cid, c in self.clusters.items()}
        edges: dict[tuple, dict] = {}

        def add(s, t, rel, conf, provs, derived=False):
            if s == t:
                return
            k = (s, t, rel)
            e = edges.get(k)
            if e is None:
                e = edges[k] = {"source": s, "target": t, "relation": rel, "confidence": conf, "provenance": [], "derived": derived}
            if conf == "extracted":
                e["confidence"] = "extracted"
            e["derived"] = e["derived"] and derived
            have = {(p["doc"], p["quote"]) for p in e["provenance"]}
            for p in provs:
                if (p["doc"], p["quote"]) not in have and len(e["provenance"]) < MAX_PROVENANCE_PER_EDGE:
                    e["provenance"].append(p)
                    have.add((p["doc"], p["quote"]))

        def ends(h, local, chunk):
            if local == "DOC":
                return [self.documents[h]["id"]]
            return self.assign.get((h, chunk, local), [])

        for ed in self.raw_edges:
            h = ed["doc"]
            srcs = ends(h, ed["source"], ed["chunk"])
            tgts = ends(h, ed["target"], ed["chunk"])
            for s in srcs:
                for t in tgts:
                    st = "Document" if s.startswith("doc:") else node_type.get(s)
                    tt = "Document" if t.startswith("doc:") else node_type.get(t)
                    rel = ed["relation"]
                    if self.schema.allowed(rel, st, tt):
                        add(s, t, rel, ed["confidence"], [ed["provenance"]])
                    elif self.schema.allowed(rel, tt, st):
                        add(t, s, rel, ed["confidence"], [{**ed["provenance"], "direction_fixed": True}])
                    else:
                        self.edge_rejected.append({"doc": short_hash(h), "chunk": ed["chunk"], "relation": rel,
                                                   "source": s, "source_type": st, "target": t, "target_type": tt,
                                                   "reason": "type pair not allowed by schema", "quote": ed["provenance"]["quote"]})

        # derived Document edges from mentions
        derived_rel = {"PrimaryText": "cites", "Concept": "discusses", "Object": "discusses", "Place": "discusses",
                       "Period": "discusses", "AncientAuthor": "discusses"}
        for c in list(self.clusters.values()):
            rel = derived_rel.get(c.type)
            if rel and self.schema.allowed(rel, "Document", c.type):
                by_doc = defaultdict(list)
                for p in c.provenance:
                    by_doc[p["doc"]].append(p)
                for h, provs in by_doc.items():
                    add(self.documents[h]["id"], c.id, rel, "extracted", provs[:3], derived=True)
            # PrimaryText -> AncientAuthor from the locus tables
            if c.type == "PrimaryText" and c.attrs.get("ancient_author") and self.schema.allowed("attributed_to", "PrimaryText", "AncientAuthor"):
                aid = self._ensure_author_node(c.attrs["ancient_author"])
                add(c.id, aid, "attributed_to", "inferred", c.provenance[:3], derived=True)
                a = self.clusters[aid]
                if not a.provenance:
                    a.provenance = c.provenance[:1]
        self.edges = list(edges.values())

    # ------------------------------------------------------------------ output
    def write(self) -> dict:
        nodes = []
        for d in self.documents.values():
            nodes.append({"id": d["id"], "type": "Document", "canonical_name": d["title"], "aliases": [],
                          "provenance": [], "unresolved": False, "resolution": "document",
                          "attrs": {k: d[k] for k in ("hash", "authors", "year", "venue", "pages", "source_path", "md_path", "language")}})
        for c in sorted(self.clusters.values(), key=lambda c: (c.type, c.id)):
            if not c.provenance:
                continue  # registry entries pulled in by a merge target but never mentioned
            nodes.append({
                "id": c.id,
                "type": c.type,
                "canonical_name": c.name,
                "aliases": sorted(c.aliases),
                "provenance": c.provenance,
                "unresolved": c.unresolved,
                "resolution": c.method,
                "attrs": {**c.attrs, **({"notes": sorted(set(c.notes))} if c.notes else {})},
            })
        ids = {n["id"] for n in nodes}
        edges = [e for e in self.edges if e["source"] in ids and e["target"] in ids]
        proposals = sorted(self.proposals.values(), key=lambda p: (p["status"] != "pending", p["type"], -p["score"]))
        for p in proposals:  # human-readable member summaries
            p["member_names"] = {m: (self.clusters[m].name if m in self.clusters else None) for m in p["members"]}
            p["member_docs"] = {m: sorted({short_hash(pv["doc"]) for pv in self.clusters[m].provenance})
                                if m in self.clusters else [] for m in p["members"]}
        write_json(self.ws.proposed_merges, {
            "how_to": "Set status to approved/rejected here or run `corpus-graph merges approve|reject <id>`; "
                      "decisions are recorded in canon/merges.yaml on the next resolve.",
            "proposals": proposals,
        })
        write_json(self.ws.work / "resolve_rejected.json", self.edge_rejected)
        self._write_schema_proposals()
        self._write_canon_candidates(nodes)
        stats = {
            "raw_nodes": len(self.raws),
            "nodes": len(nodes),
            "unresolved": sum(1 for n in nodes if n["unresolved"]),
            "edges": len(edges),
            "edge_rejected": len(self.edge_rejected),
            "proposals_pending": sum(1 for p in proposals if p["status"] == "pending"),
        }
        out = {"documents": list(self.documents.values()), "nodes": nodes, "edges": edges, "stats": stats}
        write_json(self.ws.resolved_path, out)
        return out

    def _write_schema_proposals(self) -> None:
        agg: dict[tuple, dict] = {}
        for sp in self.schema_proposals:
            k = (sp.get("relation"), sp.get("source_type"), sp.get("target_type"))
            a = agg.setdefault(k, {"relation": k[0], "source_type": k[1], "target_type": k[2], "count": 0, "examples": []})
            a["count"] += 1
            if len(a["examples"]) < 5:
                a["examples"].append({"doc": short_hash(sp["doc"]), "source": sp.get("source"), "target": sp.get("target"),
                                      "rationale": sp.get("rationale"), "quote": sp["provenance"]["quote"],
                                      "page": sp["provenance"].get("page")})
        write_json(self.ws.schema_proposals, sorted(agg.values(), key=lambda a: -a["count"]))

    def _write_canon_candidates(self, nodes: list[dict]) -> None:
        out_dir = self.ws.work / "canon_candidates"
        by_file: dict[str, list] = defaultdict(list)
        for n in nodes:
            fname = self.schema.registry_file(n["type"])
            if not fname or n["resolution"].startswith("registry") or n["type"] == "PrimaryText":
                continue
            item: dict = {"name": n["canonical_name"]}
            if n["aliases"]:
                item["aliases"] = n["aliases"]
            if n["type"] == "Scholar":
                for k in ("surname", "given", "years"):
                    if n["attrs"].get(k):
                        item[k] = n["attrs"][k]
            item["mentions"] = len(n["provenance"])
            by_file[fname].append(item)
        for fname, items in by_file.items():
            items.sort(key=lambda x: -x["mentions"])
            write_yaml(out_dir / fname, items,
                       header=f"# Unregistered entities seen in the corpus, in {fname} format.\n"
                              f"# Copy entries into canon/{fname} (drop `mentions`) to make them canonical.")
