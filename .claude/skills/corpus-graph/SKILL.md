---
name: corpus-graph
description: Answer questions about the user's scholarly corpus (New Testament, classics, ancient economy) from the corpus-graph knowledge graph. Use before answering ANY question about what the corpus, its articles, scholars, arguments, primary texts (biblical or classical loci), coins/objects, places or periods say, or how they connect. Runs `corpus-graph query`, `path` or `explain`, cites the provenance quotes returned, and says so explicitly when the graph has nothing.
---

# corpus-graph: the corpus as a query layer

The user's documents have been turned into a typed knowledge graph (`out/graph.json`) in which every node and edge carries verbatim quotes from the source articles. Treat the graph as the only authority on **what the corpus says**. Your general knowledge is not the corpus.

## Before answering any question about the corpus

1. Run one or more of these from the workspace root (or pass `--root <dir>`; for the bundled sample use `--root sample`). If `corpus-graph` is not on PATH, run it as `uv run corpus-graph` from this repository:

   ```bash
   corpus-graph query "<the user's question, or its key terms>"
   corpus-graph explain "<node name or id>"
   corpus-graph path "<node A>" "<node B>"
   ```

   - `query` matches the question's terms against canonical names and aliases, parses citations deterministically ("Mark 12" matches every PrimaryText in Mark 12; "Mt 20,2", "Mk 6.37" and "Mc 6,37" all work), expands one hop, and prints nodes, typed edges and quotes.
   - `explain` gives everything about one node (all edges, all quotes). Use it to follow up on an id that `query` returned.
   - `path` finds the shortest chain of typed relations between two nodes; add `--via-documents` to allow "both mentioned in the same article" links.
   - Add `--json` if you need to post-process. Use `--quotes N` for more evidence per edge.

2. Read the exit code:
   - `0` an answer: use it.
   - `3` an ambiguous name: the output lists candidate node ids (e.g. two scholars called Holt). Pick the one the user means from context, or ask, then re-run with the id (`corpus-graph explain scholar:holt-w`) or with a disambiguating year or initial ("Holt 2004").
   - `4` no match: the graph holds nothing on this. Say so plainly (see below). Try one or two rephrasings or a narrower term before concluding.

3. Prefer several targeted calls over one vague one: e.g. `explain` each scholar in a disagreement, then `path` between them.

## How to answer

- Base every factual statement about the corpus on the returned material, and cite the quote that supports it, with its document and page, e.g.:
  > "Pace Marwick, the denarius in Mt 20,2 is a narrative convention" (Renner 2021, p. 33)
- Keep quotes verbatim. Do not translate or "fix" Greek, Hebrew, German, French or Italian.
- Distinguish `extracted` edges (stated in the text) from `inferred` ones (implied) and `derived` ones (added deterministically, e.g. a document citing a passage it mentions) when it matters.
- Nodes flagged **unresolved** have uncertain identity (a pending merge or an ambiguous name). Mention this if the answer depends on them.
- If the user asks something the graph cannot answer, say explicitly: "The corpus graph returned nothing on X." Only then, and clearly labelled as general knowledge rather than the corpus, may you add what you know, and only if the user wants it.
- Do not claim the corpus is silent on something just because one query missed: check aliases, the locus form, and `explain` on neighbouring nodes first.

## Useful facts

- Node ids are prefixed by type: `doc:`, `scholar:`, `author:` (ancient author), `pt:` (primary-text locus, e.g. `pt:matt.20.2`), `concept:`, `object:`, `place:`, `period:`, `arg:` (a scholarly claim).
- Relations: written_by, cites, discusses, advances, argues, disputes, disagrees_with, builds_on, grounded_in, about, attested_in, related_to, attributed_to. Direction matters (`Scholar —argues→ Argument`, `Argument —grounded_in→ PrimaryText`).
- Each quote ends with `` `work/md/<hash>.md:<line>` ``: the converted Markdown and line where it occurs. Open that file if you need the surrounding passage.
- `out/REPORT.md` has computed hubs, bridges, co-citation surprises, contested passages and thin spots; read it for an overview question ("what are the main debates in my corpus?").
- These commands make no model calls; they are fast and free to run repeatedly.
