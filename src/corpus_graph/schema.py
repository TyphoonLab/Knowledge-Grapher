"""Graph schema: node and edge types, loaded from canon/schema.yaml (falls back to the seed)."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml

from .util import read_yaml

ID_PREFIX = {
    "Document": "doc",
    "Scholar": "scholar",
    "AncientAuthor": "author",
    "PrimaryText": "pt",
    "Concept": "concept",
    "Object": "object",
    "Place": "place",
    "Period": "period",
    "Argument": "arg",
}


@dataclass
class Schema:
    node_types: dict[str, dict]
    edge_types: dict[str, dict]

    @classmethod
    def load(cls, canon_dir: Path | None = None) -> "Schema":
        data = None
        if canon_dir is not None:
            data = read_yaml(canon_dir / "schema.yaml")
        if not data:
            seed = resources.files("corpus_graph.data") / "canon_seed" / "schema.yaml"
            data = yaml.safe_load(seed.read_text(encoding="utf-8"))
        return cls(data["node_types"], data["edge_types"])

    @property
    def extractable_node_types(self) -> list[str]:
        return [t for t in self.node_types if t != "Document"]

    @property
    def extractable_relations(self) -> list[str]:
        return [r for r, d in self.edge_types.items() if not d.get("derived")]

    def allowed(self, relation: str, source_type: str, target_type: str) -> bool:
        d = self.edge_types.get(relation)
        return bool(d) and [source_type, target_type] in [list(p) for p in d["pairs"]]

    def prefix(self, node_type: str) -> str:
        return ID_PREFIX.get(node_type, node_type.lower())

    def registry_file(self, node_type: str) -> str | None:
        return (self.node_types.get(node_type) or {}).get("registry")

    def describe_for_prompt(self) -> str:
        lines = ["Node types:"]
        for t, d in self.node_types.items():
            if t == "Document":
                continue
            lines.append(f"- {t}: {d.get('description', '')}")
        lines.append("")
        lines.append("Edge types (direction matters: source -> target). Use the id \"DOC\" for the document itself:")
        for r, d in self.edge_types.items():
            if d.get("derived"):
                continue
            pairs = ", ".join(f"{s}->{t}" for s, t in d["pairs"])
            note = f"  ({d['note']})" if d.get("note") else ""
            lines.append(f"- {r}: {pairs}{note}")
        return "\n".join(lines)
