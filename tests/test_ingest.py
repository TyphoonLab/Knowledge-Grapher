import subprocess
import sys
import zipfile
from pathlib import Path

from corpus_graph import ingest
from corpus_graph.chunking import ParsedMarkdown, verify_quote
from corpus_graph.util import Workspace, read_json

HERE = Path(__file__).parent


def _ws(tmp_path) -> Workspace:
    (tmp_path / "input").mkdir()
    return Workspace.from_args(root=str(tmp_path))


def test_markdown_footnotes_and_pages(tmp_path):
    ws = _ws(tmp_path)
    (ws.input / "a.md").write_text(
        "---\ntitle: T\nauthors: [A. Author]\nyear: 2020\n---\n<!-- page 5 -->\n# T\n\nBody with a note.[^1] Greek δηνάριον.\n\n"
        "<!-- page 6 -->\nMore.[^2]\n\n## Footnotes\n\n<!-- page 5 -->\n\n[^1]: First note, Mk 6,37.\n\n<!-- page 6 -->\n\n[^2]: Second.\n",
        encoding="utf-8",
    )
    src_bytes = (ws.input / "a.md").read_bytes()
    ingest.run(ws, quiet=True)
    assert (ws.input / "a.md").read_bytes() == src_bytes  # sources are never modified
    (h,) = read_json(ws.work / "manifest.json")
    md = ParsedMarkdown.load(ws.md_dir / f"{h}.md")
    assert md.meta["title"] == "T" and md.meta["year"] == 2020
    assert md.footnotes["1"].text == "First note, Mk 6,37." and md.footnotes["1"].page == "5"
    assert md.footnotes["2"].page == "6"
    text = (ws.md_dir / f"{h}.md").read_text(encoding="utf-8")
    assert "δηνάριον" in text and text.count("## Footnotes") == 1
    # unchanged input is skipped
    assert ingest.run(ws, quiet=True)["skipped"] == 1


def _docx(path: Path):
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    doc = (f'<?xml version="1.0" encoding="UTF-8"?><w:document {W}><w:body>'
           '<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr><w:r><w:t>Coins of Galilee</w:t></w:r></w:p>'
           '<w:p><w:r><w:t xml:space="preserve">The denarius (δηνάριον, דינר) in </w:t></w:r><w:r><w:rPr><w:i/></w:rPr><w:t>Matthew</w:t></w:r>'
           '<w:r><w:t xml:space="preserve"> 20:2.</w:t></w:r><w:r><w:footnoteReference w:id="1"/></w:r></w:p>'
           '<w:p><w:r><w:lastRenderedPageBreak/><w:t>Second page.</w:t></w:r></w:p></w:body></w:document>')
    fns = (f'<?xml version="1.0" encoding="UTF-8"?><w:footnotes {W}>'
           '<w:footnote w:type="separator" w:id="-1"><w:p/></w:footnote>'
           '<w:footnote w:id="1"><w:p><w:r><w:t>See Tac. Ann. 1.17.</w:t></w:r></w:p></w:footnote></w:footnotes>')
    core = ('<?xml version="1.0"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Coins of Galilee</dc:title><dc:creator>J. Smith</dc:creator></cp:coreProperties>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", doc)
        z.writestr("word/footnotes.xml", fns)
        z.writestr("docProps/core.xml", core)


def test_docx_footnotes_unicode(tmp_path):
    ws = _ws(tmp_path)
    _docx(ws.input / "smith.docx")
    ingest.run(ws, quiet=True)
    (h,) = read_json(ws.work / "manifest.json")
    md = ParsedMarkdown.load(ws.md_dir / f"{h}.md")
    body = (ws.md_dir / f"{h}.md").read_text(encoding="utf-8")
    assert "# Coins of Galilee" in body and "δηνάριον, דינר" in body and "_Matthew_" in body
    assert "[^1]" in body and md.footnotes["1"].text == "See Tac. Ann. 1.17."
    assert "<!-- page 2 -->" in body
    assert md.meta["authors"] == ["J. Smith"]


def test_pdf_layout_footnotes_printed_pages(tmp_path):
    ws = _ws(tmp_path)
    subprocess.run([sys.executable, str(HERE / "make_pdf.py"), str(ws.input / "j.pdf")], check=True)
    ingest.run(ws, quiet=True)
    (h,) = read_json(ws.work / "manifest.json")
    meta = read_json(ws.md_dir / f"{h}.meta.json")
    md = ParsedMarkdown.load(ws.md_dir / f"{h}.md")
    body = (ws.md_dir / f"{h}.md").read_text(encoding="utf-8")
    assert meta["extractor"].startswith("pymupdf4llm")
    assert not meta["quality"]["poor"]
    assert "δηνάριον" in body and "דינר" in body                  # Greek and Hebrew kept as Unicode
    assert "<!-- page 101 -->" in body and "<!-- page 102 -->" in body  # journal pagination
    assert "JOURNAL OF ANCIENT ECONOMIES" not in body.split("## Footnotes")[0]  # running head dropped
    assert set(md.footnotes) == {"1", "2", "3"}
    assert md.footnotes["3"].page == "102"
    assert "Tob 5:15" in md.footnotes["2"].text


def test_quality_heuristic_flags_poor_text():
    q = ingest.text_quality("(cid:12)(cid:13) x y z\n" * 40, 5)
    assert q["poor"] and q["reasons"]
    good = ingest.text_quality("The denarius was a common silver coin of the Roman world. " * 80, 1)
    assert not good["poor"]


def test_quote_verification():
    chunk = "The landowner “agreed with the labourers for a denarius for the day” (Matt 20:2).[^1] Ver-\nbatim."
    assert verify_quote('agreed with the labourers for a denarius for the day" (Matt 20:2)', chunk)[0] == "exact"
    assert verify_quote("Verbatim", chunk)  # hyphenation across a line break
    assert verify_quote("agreed with the workers for a denarius per day", chunk) is None  # paraphrase rejected
