# corpus-graph

Turn a folder of scholarly documents (PDF, DOCX, Markdown) into a typed knowledge graph with verbatim provenance on every node and edge; browse it in a local viewer, read a computed report of non-obvious connections, keep the converted Markdown in Obsidian, and let Claude Code answer questions about the corpus from the graph rather than from memory.

Designed for New Testament / classics / ancient-economy scholarship: Greek and Hebrew stay Unicode, footnotes are kept apart from the body but travel with the passages that cite them, journal pagination is preserved, and biblical and classical citations are parsed deterministically across English, German, French and Italian conventions (`Mk 6.37`, `Mark 6:37`, `Mc 6,37` → `Mark 6:37`; `Tac. Ann. 1.17`; `m. B. Meṣ. 7:1`; `b. B. Meṣ. 83a`).

```
input/  ──ingest──▶ work/md/<sha256>.md (+ .meta.json)        ← keep these in Obsidian
        ──extract─▶ work/extract/<sha256>/<chunk>.json          LLM, cached per chunk
        ──resolve─▶ work/resolved.json, proposed_merges.json    canon/ registries decide
        ──build───▶ out/graph.json                              networkx + Leiden
        ──report──▶ out/REPORT.md                               computed, quoted
        ──serve───▶ out/index.html                              D3 viewer, offline
query | path | explain                                          Markdown for Claude Code
```

## Install

Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

Optional extras:

```bash
uv sync --extra marker      # marker-pdf fallback for scanned / badly laid-out PDFs (pulls in PyTorch, several GB)
```

```bash
uv sync --extra anthropic   # Anthropic SDK backend (needs ANTHROPIC_API_KEY or `ant auth login`)
```

The default LLM backend is the `claude` CLI (Claude Code) in print mode, which runs on your Claude subscription; it must be installed and logged in.

**CachyOS / Arch (niri + Noctalia), no AUR:** build a local pacman package with `cd packaging/archlinux && makepkg -si`, then run `corpus-graph-setup --all`. That adds a launcher entry, niri keybindings, a Noctalia theme template for the viewer and a systemd user service; see [packaging/archlinux/CACHYOS.md](packaging/archlinux/CACHYOS.md).

Try it on the bundled sample (three synthetic articles; recorded LLM responses, so no tokens are spent):

```bash
make sample
```

That runs every stage into `sample/work` and `sample/out` and opens the viewer at http://127.0.0.1:8765/. `make test` runs the test suite against the same sample.

## Usage

Run everything from a workspace directory (the repo root works; or pass `--root DIR`, or set `CORPUS_GRAPH_ROOT`).

```bash
uv run corpus-graph init                 # creates input/, work/, out/ and canon/ (seed registries)
```

Copy documents into `input/` (subfolders are fine), then:

```bash
uv run corpus-graph ingest
```

```bash
uv run corpus-graph extract --dry-run    # plan + token/cost estimate, no calls
```

```bash
uv run corpus-graph extract --limit 3    # a first, calibrating batch
```

```bash
uv run corpus-graph extract              # the rest (asks before more than 30 documents; --yes to skip)
```

```bash
uv run corpus-graph resolve
```

```bash
uv run corpus-graph build
```

```bash
uv run corpus-graph report
```

```bash
uv run corpus-graph serve                # writes out/index.html and serves it on 127.0.0.1:8765
```

`corpus-graph run` does ingest → report in one go. Every stage is idempotent: re-running skips unchanged files and cached chunks, and sources in `input/` are never modified.

Useful flags: `extract --model opus|sonnet|haiku`, `--effort low|medium|high`, `--jobs N` (parallel calls, default 2), `--chunk-tokens N` (default 6000), `--only <path or hash fragment>`, `--force`; `extract --backend anthropic` for the SDK; `resolve --llm-merge` to have the model annotate merge candidates; `build --resolution 1.5` for smaller communities, `--llm-labels` to name them; `export-vault [DIR]` for Obsidian.

### What you get

- **`work/md/<sha256>.md`**: clean Markdown with YAML frontmatter (title, authors, year, venue, pages, language, source), `<!-- page N -->` markers (invisible in Obsidian's reading view), and all footnotes under a final `## Footnotes` heading as `[^n]:` definitions. Metadata sidecar in `.meta.json`, including the extractor used and a text-quality report.
- **`out/graph.json`**: nodes (id, type, canonical_name, aliases, provenance, unresolved, resolution method, community), edges (source, target, relation, provenance, confidence), documents and communities.
- **`out/REPORT.md`**: (a) hubs by degree and betweenness per type, (b) bridge nodes, (c) co-citation surprises for primary texts and scholars, (d) contested passages, (e) orphans and thin spots, (f) ten questions the graph can answer with node ids and the command to run. Every item is followed by its verbatim quotes with document, page and `markdown:line`.
- **`out/index.html`**: the viewer (works from disk; `serve` adds line-accurate source links). Colour = community, shape = node type; filters by type, community, document, year range and relation; search with autocomplete over names and aliases; click a node for its type, names, edges grouped by relation and the quotes behind them (each linking to the Markdown line); **Path** mode (pick two nodes); **Documents** mode; hide unresolved; drag to pin, double-click to unpin, Reset; more than 3,000 visible nodes collapse into a community view with drill-down; light/dark follows the system.
- **`work/proposed_merges.json`**, **`work/schema_proposals.json`**, **`work/canon_candidates/*.yaml`**, **`work/rejected.jsonl`**, **`work/llm_log.jsonl`**: the curation queue, the extractor's requests for new relations, unregistered entities in registry format, items dropped at validation, and every LLM call.

### Provenance rules

Every extracted node and edge carries the document hash, chunk id, a verbatim quote of at most 25 words, the Markdown line and the page. Items without a quote are rejected, and so are items whose quote cannot be found in the chunk (paraphrase protection; see DECISIONS.md). Edges also carry `confidence`: `extracted` (stated) or `inferred` (implied); deterministic edges (a document citing a passage it mentions, a passage attributed to its ancient author) are marked `derived`.

## Editing the canon registries

`canon/` is yours: plain YAML, read on every `resolve`, never overwritten (`init` only fills in missing files). One file per node type:

| file | type | notes |
|---|---|---|
| `scholars.yaml` | Scholar | `name`, `aliases`, `surname`, `given`, `years` (publication years: used to decide which of two same-surname scholars a "Holt 2004" refers to) |
| `ancient_authors.yaml` | AncientAuthor | names in EN/DE/FR/IT/Greek as aliases |
| `primary_texts.yaml` | PrimaryText | `passages:` named pericopes mapped to a locus (`{name: "Luke 15:8–10", aliases: [parable of the lost coin, verlorene Drachme]}`); `works:` extra works for the locus parser (`{key, display, author, abbr: [...]}`) |
| `concepts.yaml`, `coins_and_objects.yaml`, `places.yaml`, `periods.yaml`, `arguments.yaml` | | `name` + `aliases` (put δηνάριον, דינר, Denar, denier in the aliases of `denarius`) |
| `documents.yaml` | Document | metadata overrides by file name or hash, when PDF metadata is wrong |
| `merges.yaml` | | approved/rejected merges (written by `merges approve|reject`) |
| `schema.yaml` | | node and edge types (see below) |

Resolution order for each extracted entity: exact canonical name in the registry → registry alias → deterministic normalisation (locus parsing for primary texts; surname + initial + year for scholars; case/accent/article/plural folding otherwise) → fuzzy candidates, which are only *proposed*. Anything that cannot be identified stays as its own node with `unresolved: true`.

The fastest way to grow the registries: after a `resolve`, open `work/canon_candidates/<file>.yaml`, which lists every unregistered entity in the right format (most-mentioned first). Copy the entries you want into `canon/`, fix names, add aliases, and re-run `resolve`.

## Approving proposed merges

```bash
uv run corpus-graph merges list                 # pending proposals with member names and documents
```

```bash
uv run corpus-graph merges approve 3f9c2a       # id prefix is enough; target defaults to the suggested node
```

```bash
uv run corpus-graph merges approve 3f9c2a --into scholar:holt-w     # required for "ambiguous" proposals
```

```bash
uv run corpus-graph merges approve --min-score 92 --type Argument   # bulk-approve near-identical claims
```

```bash
uv run corpus-graph merges reject 7b1e04
```

Then `resolve` and `build` again. Decisions live in `canon/merges.yaml` (so they survive deleting `work/`); you can also set `"status": "approved"` or `"rejected"` directly in `work/proposed_merges.json` and the next `resolve` records it. Rejected pairs are never proposed again.

## Adding a node or edge type

Edit `canon/schema.yaml`:

```yaml
node_types:
  Manuscript: {registry: manuscripts.yaml, shape: square, description: "A New Testament manuscript (P46, Codex Sinaiticus)."}
edge_types:
  cites_scholar: {pairs: [[Document, Scholar]]}
  preserved_in:  {pairs: [[PrimaryText, Manuscript]]}
```

- The extraction prompt, its JSON schema, the resolver's edge validation and the viewer legend are generated from this file. Changing it changes the prompt fingerprint, so `extract` re-processes chunks on the next run (budget accordingly; `--only` to test on a few documents first).
- A new node type needs an id prefix: types not in the built-in list get their lower-cased name (`manuscript:`). Create `canon/<registry>.yaml` (a list of `{name, aliases}`) if you named one.
- Shapes: circle, square, triangle, diamond, hexagon, star, pentagon, cross, wye.
- `work/schema_proposals.json` shows which relations the extractor has been asking for, with counts and example quotes: a good guide to what to add.

## Claude Code as a query layer

```bash
uv run corpus-graph query "Who disagrees about Mark 12?"
```

```bash
uv run corpus-graph explain "Tobias Renner"
```

```bash
uv run corpus-graph path "Lisbeth Holt" "Tobias Renner"
```

`query` matches the question's words against canonical names and aliases (longest phrase first), parses any citation in it so that "Mark 12" matches every PrimaryText within Mark 12, expands one hop, and prints the matched nodes, their typed edges and the provenance quotes with document title, page and `markdown:line`. `explain` prints everything about one node; `path` prints the shortest chain of typed relations (through documents only if no entity-only path exists, or with `--via-documents`). All three accept `--json`. Exit codes: **0** answer, **3** ambiguous name (candidates listed; add a year or initial, or use the node id), **4** no match. No model calls happen in these commands.

The skill in `.claude/skills/corpus-graph/SKILL.md` tells Claude Code to run these commands before answering any question about the corpus, to cite the quotes it gets back, and to say explicitly when the graph returned nothing instead of answering from general knowledge. Claude Code discovers project skills automatically when started in this repository; you can also invoke it with `/corpus-graph`. If your workspace is elsewhere, tell Claude (or set `CORPUS_GRAPH_ROOT`) so it passes `--root`.

## Cost

Measured on the sample (live `claude -p`, Sonnet, 6 chunks): every call carries ~6k tokens of fixed prompt (our system prompt and schema plus the CLI's own scaffolding, mostly cache reads after the first call); output is about **80 tokens per kept node or edge**, because each one carries its own quote. Output therefore dominates cost and scales with how citation-dense the text is, not with page count as such.

Estimate for an average **30-page article** (~15,000 words with notes ≈ 21,000 tokens → 4 chunks of 6,000):

| | Sonnet 5.5 (default) | Opus 5.5 | Haiku 5.5 |
|---|---|---|---|
| input (≈ 45k tokens, ~40 % cached) | ≈ $0.07 | ≈ $0.14 | < $0.01 |
| output (≈ 30–55k tokens: 350–700 items) | ≈ $0.30–0.55 | ≈ $0.60–1.10 | ≈ $0.02–0.03 |
| **per article** | **≈ $0.35–0.65** | **≈ $0.75–1.25** | **≈ $0.03** |

These are list prices. Through `claude -p` on a subscription nothing is billed per token, but the same volume counts against your usage limits, and each output-heavy chunk takes one to three minutes (run `--jobs 2–4`). The sample's synthetic articles are about twice as dense as real prose, which is why the range above is wide. Calibrate on your own material: run `extract --limit 3` on three representative articles (say, one footnote-heavy German article, one two-column PDF, one English monograph chapter), then `extract --dry-run`. Once three live calls are logged, the estimate is fitted from `work/llm_log.jsonl` and prints a calibrated figure for the rest of the corpus.

## Layout

```
src/corpus_graph/
  cli.py          command line
  ingest.py       PDF (pymupdf4llm layout, marker fallback) / DOCX / MD → Markdown
  chunking.py     section chunks, footnote attachment, quote location and verification
  prompts.py      extraction prompt + JSON schema (versioned)
  llm.py          claude -p, Anthropic SDK, replay/record backends; llm_log
  extract.py      per-chunk extraction, validation, cache, cost estimate, 30-document gate
  loci.py         deterministic biblical/classical citation parser (data/*.yaml tables)
  canon.py        registries; resolve.py entity resolution; merges.py approvals
  build.py        networkx + Leiden communities; report.py REPORT.md
  graphstore.py   shared matching/provenance; query.py query/path/explain
  serve.py        viewer writer + local server; viewer/template.html (D3 vendored)
  vault.py        Obsidian export
canon/            your registries (seeded by `init`)
sample/           three synthetic articles, their canon, and recorded extraction responses
tests/            pytest suite (runs the whole sample pipeline)
```

See [DECISIONS.md](DECISIONS.md) for the design choices, departures from the original brief, and known limitations.
