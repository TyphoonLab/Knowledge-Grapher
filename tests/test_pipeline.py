import json

import pytest

from corpus_graph import cli, extract
from corpus_graph.chunking import ParsedMarkdown, QuoteLocator
from corpus_graph.schema import Schema
from corpus_graph.util import read_json, read_jsonl, write_yaml


def nodes(ws):
    return {n["id"]: n for n in read_json(ws.graph_path)["nodes"]}


def edges(ws):
    return read_json(ws.graph_path)["edges"]


def run_cli(ws, *args, capsys):
    code = cli.main(["--root", str(ws.root), *args])
    return code, capsys.readouterr().out


# --------------------------------------------------------------------------- extraction


def test_every_item_has_verified_provenance(sample_ws):
    for n in read_json(sample_ws.graph_path)["nodes"]:
        if n["type"] == "Document":
            continue
        assert n["provenance"], n["id"]
        for p in n["provenance"]:
            assert p["quote"] and len(p["quote"].split()) <= 25
            assert p["line"] and p["doc"]
    for e in edges(sample_ws):
        assert e["provenance"] and e["confidence"] in ("extracted", "inferred")


def test_quotes_occur_at_the_cited_line(sample_ws):
    from corpus_graph.util import match_text

    g = read_json(sample_ws.graph_path)
    docs = {d["hash"]: d for d in g["documents"]}
    for n in g["nodes"][:40]:
        for p in n["provenance"]:
            if p.get("match") != "exact":
                continue
            lines = (sample_ws.root / docs[p["doc"]]["md_path"]).read_text(encoding="utf-8").split("\n")
            window = match_text(" ".join(lines[p["line"] - 1:p["line"] + 2]))
            assert match_text(p["quote"])[:30] in window


def test_validation_rejects_unverifiable_items(sample_ws):
    h = next(iter(read_json(sample_ws.work / "manifest.json")))
    md = ParsedMarkdown.load(sample_ws.md_dir / f"{h}.md")
    chunk = md.chunks(700)[0]
    data = {
        "document_meta": None,
        "nodes": [
            {"id": "n1", "type": "Concept", "name": "invented", "aliases": [], "quote": "this sentence is not in the article"},
            {"id": "n2", "type": "Concept", "name": "no quote", "aliases": [], "quote": ""},
            {"id": "n3", "type": "Concept", "name": "long", "aliases": [], "quote": " ".join(chunk.text.split()[40:75])},
        ],
        "edges": [{"source": "n1", "target": "n3", "relation": "related_to", "quote": "x", "confidence": "extracted"}],
        "schema_proposals": [],
    }
    v = extract.validate(data, chunk, h, QuoteLocator(md), Schema.load())
    assert [n["name"] for n in v["nodes"]] == ["long"]
    assert v["nodes"][0]["provenance"].get("truncated") and len(v["nodes"][0]["provenance"]["quote"].split()) == 25
    reasons = sorted(r["reason"] for r in v["rejected"])
    assert reasons == ["endpoint missing or rejected", "missing quote", "quote not found in chunk"]


def test_cache_skips_unchanged_chunks_and_logs_calls(sample_ws):
    r = extract.run(sample_ws, backend="replay", fixtures=sample_ws.root / "nope", chunk_tokens=700)
    assert r["processed"] == 0 and r["estimate"]["chunks"] == 0
    log = read_jsonl(sample_ws.llm_log)
    assert len(log) == 6 and all(x["chunk_id"] and x["model"] for x in log)


def test_document_gate_stops_large_runs(tmp_path, monkeypatch):
    from corpus_graph import ingest
    from corpus_graph.util import Workspace

    ws = Workspace.from_args(root=str(tmp_path))
    ws.input.mkdir()
    for i in range(31):
        (ws.input / f"d{i}.md").write_text(f"# Doc {i}\n\nThe denarius in Mark 6:37 number {i}.\n")
    ingest.run(ws, quiet=True)
    monkeypatch.setattr(extract, "confirm", lambda prompt: False)
    with pytest.raises(SystemExit) as ex:
        extract.run(ws, backend="replay", fixtures=tmp_path)
    assert ex.value.code == 2
    assert extract.run(ws, backend="replay", fixtures=tmp_path, dry_run=True)["estimate"]["docs"] == 31


# --------------------------------------------------------------------------- resolution


def test_locus_normalisation_merges_conventions(sample_ws):
    n = nodes(sample_ws)
    assert n["pt:mark.6.37"]["canonical_name"] == "Mark 6:37"
    docs_citing = {e["source"] for e in edges(sample_ws) if e["target"] == "pt:matt.20.2" and e["relation"] == "cites"}
    assert len(docs_citing) == 3  # "Matt 20:2" (Marwick), "Mt 20,2" (Renner, Ferrante)
    assert "Mt 20,2" in n["pt:matt.20.2"]["aliases"]
    assert n["pt:tac-ann.1.17"]["attrs"]["ancient_author"] == "Tacitus"
    assert any(e["source"] == "pt:tac-ann.1.17" and e["target"] == "author:tacitus" and e["relation"] == "attributed_to"
               for e in edges(sample_ws))


def test_scholar_surname_year_disambiguation(sample_ws):
    n = nodes(sample_ws)
    holt_l, holt_w = n["scholar:holt-l"], n["scholar:holt-w"]
    assert holt_l["canonical_name"] == "Lisbeth Holt" and holt_l["attrs"]["years"] == [1987]
    assert holt_w["attrs"]["years"] == [2004]
    # Renner cites "Holt 2004" (surname only): attached to W. Holt by year
    renner = next(d for d in read_json(sample_ws.graph_path)["documents"] if d["authors"] == ["Tobias Renner"])
    assert renner["hash"] in {p["doc"] for p in holt_w["provenance"]}
    assert renner["hash"] not in {p["doc"] for p in holt_l["provenance"]}
    assert not any(i.endswith("-ambiguous") for i in n)


def test_registry_aliases_and_greek(sample_ws):
    n = nodes(sample_ws)
    den = n["object:denarius"]
    assert den["resolution"].startswith("registry") and "δηνάριον" in den["aliases"]
    assert len({p["doc"] for p in den["provenance"]}) == 3


def test_registry_scholar_years(tmp_path):
    from conftest import copy_sample, run_pipeline

    ws = copy_sample(tmp_path / "ws")
    write_yaml(ws.canon / "scholars.yaml", [
        {"name": "Lisbeth Holt", "surname": "Holt", "given": "Lisbeth", "years": [1987]},
        {"name": "Werner Holt", "surname": "Holt", "given": "Werner", "years": [2004], "aliases": ["W. Holt"]},
    ])
    run_pipeline(ws)
    n = nodes(ws)
    assert n["scholar:werner-holt"]["canonical_name"] == "Werner Holt"
    assert "registry-surname+year" in json.dumps([x["resolution"] for x in n.values()])
    assert "scholar:holt-w" not in n


def test_unresolved_and_proposals(sample_ws):
    pm = read_json(sample_ws.proposed_merges)
    pending = [p for p in pm["proposals"] if p["status"] == "pending"]
    assert pending and all(p["kind"] in ("fuzzy", "ambiguous") for p in pending)
    n = nodes(sample_ws)
    for p in pending:
        for m in p["members"]:
            assert n[m]["unresolved"]


def test_merge_approval_round_trip(fresh_sample, capsys):
    ws = fresh_sample
    pm = read_json(ws.proposed_merges)
    p = next(p for p in pm["proposals"] if p["status"] == "pending" and p["kind"] == "fuzzy")
    before = len(nodes(ws))
    code, out = run_cli(ws, "merges", "approve", p["id"][:6], capsys=capsys)
    assert code == 0 and "approved" in out
    merges = (ws.canon / "merges.yaml").read_text()
    assert p["members"][0] in merges and p["members"][1] in merges
    run_cli(ws, "resolve", capsys=capsys)
    run_cli(ws, "build", capsys=capsys)
    after = nodes(ws)
    assert len(after) == before - 1
    merged = after[p["into"]]
    assert not merged["unresolved"] and "merged-approved" in merged["resolution"]
    # the decision survives deleting work/
    import shutil
    shutil.rmtree(ws.work / "extract")
    (ws.proposed_merges).unlink()
    from conftest import run_pipeline
    run_pipeline(ws)
    assert p["into"] in nodes(ws) and len(nodes(ws)) == before - 1


def test_schema_rejections_are_recorded(sample_ws):
    rej = read_json(sample_ws.work / "resolve_rejected.json")
    assert all(r["reason"] == "type pair not allowed by schema" for r in rej)
    for e in edges(sample_ws):
        st = "Document" if e["source"].startswith("doc:") else nodes(sample_ws)[e["source"]]["type"]
        tt = "Document" if e["target"].startswith("doc:") else nodes(sample_ws)[e["target"]]["type"]
        assert Schema.load().allowed(e["relation"], st, tt), e


# --------------------------------------------------------------------------- build / report / viewer


def test_graph_json_has_communities(sample_ws):
    g = read_json(sample_ws.graph_path)
    assert g["communities"] and all(c["label"] for c in g["communities"])
    assert all("community" in n for n in g["nodes"])
    assert "leiden" in g["meta"]["community_algorithm"]


def test_report_sections_and_contested_passage(sample_ws):
    text = (sample_ws.out / "REPORT.md").read_text(encoding="utf-8")
    for h in ("## (a) Hub nodes", "## (b) Bridge nodes", "## (c) Co-citation surprises", "## (d) Contested passages",
              "## (e) Orphans and thin spots", "## (f) Questions this graph can answer"):
        assert h in text
    contested = text.split("## (d)")[1].split("## (e)")[0]
    assert "### Matt 20:2 (`pt:matt.20.2`)" in contested
    assert "argued by **Helena Marwick**" in contested and "disputed by **Tobias Renner**" in contested
    assert "Pace Marwick" in contested
    qs = text.split("## (f)")[1]
    assert qs.count("   - nodes:") == 10


def test_viewer_is_self_contained(sample_ws):
    html = (sample_ws.out / "index.html").read_text(encoding="utf-8")
    assert "d3js.org v7" in html and '"canonical_name":"Matt 20:2"' in html
    assert "<script src=" not in html and "<link" not in html  # nothing fetched from the network


# --------------------------------------------------------------------------- query layer


def test_query_locus_chapter(sample_ws, capsys):
    code, out = run_cli(sample_ws, "query", "Who argues about Mark 12?", capsys=capsys)
    assert code == 0
    assert "`pt:mark.12.15`" in out and "`pt:mark.12.42`" in out
    assert "Jesus asks for a denarius to be brought" in out  # provenance quote printed
    assert "p. 35" in out


def test_query_ambiguous_exit_3(sample_ws, capsys):
    code, out = run_cli(sample_ws, "query", "Holt", capsys=capsys)
    assert code == 3 and "scholar:holt-l" in out and "scholar:holt-w" in out
    code, out = run_cli(sample_ws, "query", "Holt 2004", capsys=capsys)
    assert code == 0 and "W. Holt" in out


def test_query_no_match_exit_4(sample_ws, capsys):
    code, out = run_cli(sample_ws, "query", "Pythagorean tuning", capsys=capsys)
    assert code == 4 and "No match" in out


def test_explain_and_path(sample_ws, capsys):
    code, out = run_cli(sample_ws, "explain", "Tobias Renner", capsys=capsys)
    assert code == 0 and "### disagrees_with" in out and "Helena Marwick" in out
    code, out = run_cli(sample_ws, "path", "Lisbeth Holt", "Tobias Renner", capsys=capsys)
    assert code == 0 and "hop(s)" in out and "builds_on" in out
    code, out = run_cli(sample_ws, "explain", "Holt", capsys=capsys)
    assert code == 3
    code, out = run_cli(sample_ws, "path", "Holt 1987", "Nobody at all", capsys=capsys)
    assert code == 4


def test_query_json(sample_ws, capsys):
    code, out = run_cli(sample_ws, "query", "Mt 20,2", "--json", capsys=capsys)
    data = json.loads(out)
    assert code == 0 and data["status"] == "ok"
    assert "pt:matt.20.2" in {n["id"] for n in data["nodes"]}


def test_vault_export(sample_ws, capsys):
    code, out = run_cli(sample_ws, "export-vault", capsys=capsys)
    vault = sample_ws.out / "vault"
    assert code == 0 and (vault / "Nodes" / "PrimaryText" / "Matt 20.2.md").exists()
    note = (vault / "Nodes" / "PrimaryText" / "Matt 20.2.md").read_text(encoding="utf-8")
    assert "aliases:" in note and "#^cg-L" in note
