"""Human-edited canonical registries in canon/ (YAML)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from .loci import LocusParser
from .schema import Schema
from .util import eprint, fold, read_yaml, slug, write_yaml


@dataclass
class Entry:
    id: str
    type: str
    name: str
    aliases: list[str]
    data: dict = field(default_factory=dict)


@dataclass
class Registry:
    type: str
    entries: list[Entry] = field(default_factory=list)
    by_key: dict[str, list[Entry]] = field(default_factory=dict)

    def add(self, e: Entry) -> None:
        self.entries.append(e)
        for k in {fold(e.name), *(fold(a) for a in e.aliases)}:
            if k:
                self.by_key.setdefault(k, []).append(e)

    def lookup(self, name: str) -> list[Entry]:
        return self.by_key.get(fold(name), [])


class Canon:
    def __init__(self, canon_dir: Path, schema: Schema):
        self.dir = canon_dir
        self.schema = schema
        self.registries: dict[str, Registry] = {}
        pt = read_yaml(canon_dir / "primary_texts.yaml", {}) or {}
        self.extra_works = list(pt.get("works") or [])
        self.parser = LocusParser(extra_works=self.extra_works)
        for ntype in schema.node_types:
            reg = Registry(ntype)
            fname = schema.registry_file(ntype)
            if fname:
                raw = read_yaml(canon_dir / fname, [])
                items = (raw.get("passages") or []) if isinstance(raw, dict) else (raw or [])
                for item in items:
                    if not isinstance(item, dict) or not item.get("name"):
                        continue
                    e = self._entry(ntype, item)
                    if e:
                        reg.add(e)
            self.registries[ntype] = reg
        # authors of works in the locus tables are known AncientAuthors even if not registered
        aa = self.registries.get("AncientAuthor")
        if aa is not None:
            for w in self.parser.works.values():
                if w.author and not aa.lookup(w.author):
                    aa.add(Entry(f"author:{slug(w.author)}", "AncientAuthor", w.author, [], {"source": "locus table"}))
        self.documents = read_yaml(canon_dir / "documents.yaml", {}) or {}
        merges = read_yaml(canon_dir / "merges.yaml", {}) or {}
        self.approved = list(merges.get("approved") or [])
        self.rejected = list(merges.get("rejected") or [])

    def _entry(self, ntype: str, item: dict) -> Entry | None:
        name = str(item["name"]).strip()
        aliases = [str(a).strip() for a in item.get("aliases") or [] if str(a).strip()]
        data = {k: v for k, v in item.items() if k not in ("name", "aliases", "id")}
        if ntype == "PrimaryText":
            loci = self.parser.parse(name)
            if len(loci) != 1:
                eprint(f"  ! canon/primary_texts.yaml: cannot parse passage name {name!r}; skipped")
                return None
            data["locus"] = loci[0]
            return Entry(loci[0].node_id, ntype, loci[0].canonical_name, aliases + ([name] if name != loci[0].canonical_name else []), data)
        eid = item.get("id") or slug(name)
        return Entry(f"{self.schema.prefix(ntype)}:{eid}", ntype, name, aliases, data)

    def save_merges(self) -> None:
        write_yaml(
            self.dir / "merges.yaml",
            {"approved": self.approved, "rejected": self.rejected},
            header="# Merge decisions. Written by `corpus-graph merges approve|reject`; you can also edit by hand.",
        )


def init_canon(canon_dir: Path, force: bool = False) -> list[str]:
    """Copy the seed registries into canon/ (never overwrites unless force)."""
    canon_dir.mkdir(parents=True, exist_ok=True)
    seed = resources.files("corpus_graph.data") / "canon_seed"
    written = []
    for item in seed.iterdir():
        if not item.name.endswith(".yaml"):
            continue
        dest = canon_dir / item.name
        if dest.exists() and not force:
            continue
        with resources.as_file(item) as src:
            shutil.copyfile(src, dest)
        written.append(item.name)
    return written
