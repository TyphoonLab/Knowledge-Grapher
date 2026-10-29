"""Stage 1: convert PDF / DOCX / Markdown sources into clean Markdown + metadata sidecars.

Output, per source file with SHA256 `h`:
  work/md/<h>.md         Markdown with YAML frontmatter, `<!-- page N -->` markers,
                         and footnotes gathered into a final "## Footnotes" section
  work/md/<h>.meta.json  title, authors, year, venue, pages, source path, extractor, quality

Greek and Hebrew are kept as Unicode (NFC); nothing is transliterated.
"""

from __future__ import annotations

import re
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

import yaml

from .util import (
    Workspace,
    detect_language,
    eprint,
    iter_sources,
    nfc,
    read_json,
    read_yaml,
    sha256_file,
    write_json,
    write_text,
)

INGEST_VERSION = 3
PAGE_MARK = "<!-- page {} -->"
PAGE_MARK_RE = re.compile(r"<!--\s*page\s+([\w.-]+)\s*-->")
FOOTNOTE_HEADING = "## Footnotes"

# Text-yield heuristic for falling back to marker-pdf (see DECISIONS.md)
MIN_CHARS_PER_PAGE = 400
MIN_WORDLIKE_RATIO = 0.55
MAX_BAD_GLYPH_RATIO = 0.005
MAX_SHORT_LINE_RATIO = 0.6


# =========================================================================== entry point


def run(ws: Workspace, force: bool = False, use_ocr: bool = False, quiet: bool = False) -> dict:
    ws.ensure()
    manifest: dict[str, dict] = {}
    overrides = read_yaml(ws.canon / "documents.yaml", {}) or {}
    stats = Counter()
    for src in iter_sources(ws.input):
        h = sha256_file(src)
        md_path = ws.md_dir / f"{h}.md"
        meta_path = ws.md_dir / f"{h}.meta.json"
        rel = str(src.relative_to(ws.input))
        old = read_json(meta_path)
        if not force and old and old.get("ingest_version") == INGEST_VERSION and md_path.exists():
            manifest[h] = {"source": rel, "md": md_path.name}
            stats["skipped"] += 1
            continue
        try:
            body, footnotes, meta = convert(src, use_ocr=use_ocr)
        except Exception as exc:  # one bad file must not stop the run
            eprint(f"  ! {rel}: {type(exc).__name__}: {exc}")
            stats["failed"] += 1
            continue
        meta.update({"sha256": h, "source_path": rel, "ingest_version": INGEST_VERSION})
        ov = overrides.get(rel) or overrides.get(src.name) or overrides.get(h)
        if ov:
            meta.update({k: v for k, v in ov.items() if v is not None})
            meta["meta_source"] = "canon/documents.yaml"
        meta["language"] = meta.get("language") or detect_language(body)
        write_text(md_path, render_markdown(body, footnotes, meta))
        write_json(meta_path, meta)
        manifest[h] = {"source": rel, "md": md_path.name}
        stats["converted"] += 1
        if not quiet:
            q = meta.get("quality", {})
            flag = "  (poor text yield: see meta.quality)" if q.get("poor") else ""
            eprint(f"  + {rel} -> md/{h[:12]}….md  [{meta['extractor']}]{flag}")
    write_json(ws.work / "manifest.json", manifest)
    if not quiet:
        eprint(f"ingest: {stats['converted']} converted, {stats['skipped']} unchanged, {stats['failed']} failed")
    return dict(stats)


def convert(src: Path, use_ocr: bool = False) -> tuple[str, list[tuple[str, str, str | None]], dict]:
    """Return (body_markdown, footnotes[(label, text, page)], meta)."""
    suffix = src.suffix.lower()
    if suffix == ".pdf":
        return convert_pdf(src, use_ocr=use_ocr)
    if suffix == ".docx":
        return convert_docx(src)
    return convert_md(src)


def render_markdown(body: str, footnotes: list[tuple[str, str, str | None]], meta: dict) -> str:
    front = {
        k: meta.get(k)
        for k in ("title", "authors", "year", "venue", "pages", "language", "source_path", "sha256", "extractor")
        if meta.get(k) not in (None, "", [])
    }
    out = ["---", yaml.safe_dump(front, allow_unicode=True, sort_keys=False).strip(), "---", "", body.strip(), ""]
    if footnotes:
        out += ["", FOOTNOTE_HEADING, ""]
        last_page = None
        for label, text, page in footnotes:
            if page is not None and page != last_page:
                out += [PAGE_MARK.format(page), ""]
                last_page = page
            out.append(f"[^{label}]: {text.strip()}")
            out.append("")
    return nfc("\n".join(out).rstrip() + "\n")


# =========================================================================== Markdown

_FN_DEF = re.compile(r"^\[\^([^\]]+)\]:\s?(.*)$")


def convert_md(src: Path):
    text = nfc(src.read_text(encoding="utf-8"))
    meta: dict = {"extractor": "markdown"}
    if text.startswith("---"):
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
        if m:
            fm = yaml.safe_load(m.group(1)) or {}
            for k in ("title", "authors", "year", "venue", "pages", "language", "abstract"):
                if fm.get(k) is not None:
                    meta[k] = fm[k]
            if isinstance(meta.get("authors"), str):
                meta["authors"] = [a.strip() for a in re.split(r";| and ", meta["authors"]) if a.strip()]
            text = text[m.end():]
    body_lines, footnotes = [], []
    lines = text.splitlines()
    page = None
    in_fn_section = False
    i = 0
    while i < len(lines):
        line = lines[i]
        pm = PAGE_MARK_RE.search(line)
        if pm:
            page = pm.group(1)
            if in_fn_section:  # page markers of the notes section belong to the notes, not the body
                i += 1
                continue
        m = _FN_DEF.match(line)
        if m:
            label, parts = m.group(1), [m.group(2)]
            i += 1
            while i < len(lines) and (lines[i].startswith(("    ", "\t")) or (lines[i].strip() and not _FN_DEF.match(lines[i]) and not lines[i].startswith("#") and parts and lines[i - 1].strip())):
                if PAGE_MARK_RE.search(lines[i]):
                    break
                parts.append(lines[i].strip())
                i += 1
            footnotes.append((label, " ".join(p for p in parts if p), page))
            continue
        if line.strip() == FOOTNOTE_HEADING:
            in_fn_section = True
            i += 1
            continue
        if in_fn_section and not line.strip():
            i += 1
            continue
        body_lines.append(line)
        i += 1
    body = "\n".join(body_lines).strip()
    if not meta.get("title"):
        h1 = re.search(r"^#\s+(.+)$", body, re.M)
        meta["title"] = h1.group(1).strip() if h1 else src.stem
    pages = [p for p in PAGE_MARK_RE.findall(body)]
    if pages and not meta.get("pages"):
        meta["pages"] = f"{pages[0]}–{pages[-1]}" if len(pages) > 1 else pages[0]
    meta["quality"] = text_quality(body, max(1, len(pages)), judge=False)
    return body, footnotes, meta


# =========================================================================== DOCX

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_runs(p, footnote_refs: bool = True) -> str:
    out = []
    for node in p.iter():
        tag = node.tag
        if tag == f"{W}r":
            rpr = node.find(f"{W}rPr")
            italic = rpr is not None and rpr.find(f"{W}i") is not None and rpr.find(f"{W}i").get(f"{W}val") not in ("0", "false")
            vert = rpr.find(f"{W}vertAlign") if rpr is not None else None
            sup = vert is not None and vert.get(f"{W}val") == "superscript"
            text = "".join(
                (t.text or "") if t.tag == f"{W}t" else ("\t" if t.tag == f"{W}tab" else "")
                for t in node
                if t.tag in (f"{W}t", f"{W}tab")
            )
            ref = node.find(f"{W}footnoteReference")
            if ref is not None and footnote_refs:
                out.append(f"[^{ref.get(f'{W}id')}]")
                continue
            if node.find(f"{W}lastRenderedPageBreak") is not None:
                out.append("\x00PAGEBREAK\x00")
            if not text:
                continue
            if sup and text.strip().isdigit():
                out.append(f"<sup>{text}</sup>")
            elif italic and text.strip():
                lead, core, trail = re.match(r"^(\s*)(.*?)(\s*)$", text, re.S).groups()
                out.append(f"{lead}_{core}_{trail}")
            else:
                out.append(text)
    return "".join(out)


def convert_docx(src: Path):
    with zipfile.ZipFile(src) as z:
        doc = ET.fromstring(z.read("word/document.xml"))
        fn_xml = z.read("word/footnotes.xml") if "word/footnotes.xml" in z.namelist() else None
        core = z.read("docProps/core.xml") if "docProps/core.xml" in z.namelist() else None
    meta: dict = {"extractor": "docx-xml"}
    if core is not None:
        c = ET.fromstring(core)
        ns = {"dc": "http://purl.org/dc/elements/1.1/", "dcterms": "http://purl.org/dc/terms/"}
        t = c.find("dc:title", ns)
        a = c.find("dc:creator", ns)
        d = c.find("dcterms:created", ns)
        if t is not None and t.text:
            meta["title"] = t.text.strip()
        if a is not None and a.text:
            meta["authors"] = [x.strip() for x in re.split(r";|,| and ", a.text) if x.strip()]
        if d is not None and d.text and re.match(r"\d{4}", d.text):
            meta["file_year"] = int(d.text[:4])
    lines: list[str] = []
    page = 1
    page_of_fn: dict[str, int] = {}
    lines.append(PAGE_MARK.format(page))
    body = doc.find(f"{W}body")
    for p in body.iter(f"{W}p"):
        ppr = p.find(f"{W}pPr")
        style = ""
        if ppr is not None and ppr.find(f"{W}pStyle") is not None:
            style = ppr.find(f"{W}pStyle").get(f"{W}val", "")
        text = _docx_runs(p)
        if "\x00PAGEBREAK\x00" in text:
            before, _, after = text.partition("\x00PAGEBREAK\x00")
            text = before + after.replace("\x00PAGEBREAK\x00", "")
            page += 1
            lines.append("")
            lines.append(PAGE_MARK.format(page))
        for ref in re.findall(r"\[\^(\d+)\]", text):
            page_of_fn.setdefault(ref, page)
        if not text.strip():
            continue
        m = re.match(r"(?i)heading\s?(\d)", style) or re.match(r"(?i)berschrift(\d)", style)
        if style.lower() == "title":
            lines.append(f"# {text.strip()}")
            meta.setdefault("title", text.strip())
        elif m:
            lines.append("#" * min(6, int(m.group(1)) + 1) + " " + text.strip())
        else:
            lines.append(text.strip())
        lines.append("")
    footnotes = []
    if fn_xml is not None:
        fx = ET.fromstring(fn_xml)
        for fn in fx.findall(f"{W}footnote"):
            fid = fn.get(f"{W}id")
            if fn.get(f"{W}type") in ("separator", "continuationSeparator", "continuationNotice") or fid in ("-1", "0"):
                continue
            txt = " ".join(_docx_runs(p, footnote_refs=False).strip() for p in fn.iter(f"{W}p")).strip()
            if txt:
                footnotes.append((fid, txt, str(page_of_fn.get(fid, "")) or None))
    body_md = nfc("\n".join(lines))
    meta.setdefault("title", src.stem)
    meta["pages"] = page
    meta["quality"] = text_quality(body_md, page, judge=False)
    return body_md, footnotes, meta


# =========================================================================== PDF


def convert_pdf(src: Path, use_ocr: bool = False):
    import pymupdf

    doc = pymupdf.open(src)
    try:
        body, footnotes, meta = _pdf_layout(doc, use_ocr)
    except Exception as exc:  # layout engine unavailable or failed: plain pymupdf4llm
        eprint(f"    layout extraction failed ({exc}); using pymupdf4llm without footnote separation")
        body, footnotes, meta = _pdf_plain(doc)
    meta.update(_pdf_meta(doc, body))
    meta["quality"] = text_quality(body, doc.page_count)
    if meta["quality"]["poor"]:
        alt = _try_marker(src)
        if alt is not None:
            alt_body, alt_meta = alt
            q = text_quality(alt_body, doc.page_count)
            if q["score"] > meta["quality"]["score"]:
                body, footnotes = _split_inline_footnotes(alt_body)
                meta["extractor"] = alt_meta["extractor"]
                meta["quality"] = q
                meta["quality"]["fallback_from"] = "pymupdf4llm"
        else:
            meta["quality"]["note"] = (
                "Poor text yield and marker-pdf is not installed. Install with `uv sync --extra marker` "
                "and re-run `corpus-graph ingest --force`."
            )
    return body, footnotes, meta


def _pdf_layout(doc, use_ocr: bool):
    """pymupdf4llm layout path: classify boxes, keep footnotes apart, drop running heads/feet."""
    import dataclasses

    import pymupdf4llm  # noqa: F401  (activates pymupdf.layout)
    from pymupdf4llm.helpers import document_layout as dl

    parsed = dl.parse_document(doc, use_ocr=use_ocr, force_text=True, show_progress=False)
    printed = _printed_page_numbers(parsed)
    body_parts, footnotes = [], []
    for idx, page in enumerate(parsed.pages):
        pno = printed[idx]
        fn_ids = {id(b) for b in page.boxes if b.boxclass == "footnote" or _looks_like_footnote(b, page)}
        fn_boxes = [b for b in page.boxes if id(b) in fn_ids]
        other = [b for b in page.boxes if id(b) not in fn_ids]
        page_doc = dataclasses.replace(parsed, pages=[dataclasses.replace(page, boxes=other)])
        md = page_doc.to_markdown(header=False, footer=False, page_chunks=False)
        body_parts.append(PAGE_MARK.format(pno) + "\n\n" + _clean_pdf_md(md))
        for b in fn_boxes:
            if b.textlines:
                footnotes.extend(_parse_footnote_md(dl.footnote_to_md(b.textlines), str(pno)))
    body = "\n\n".join(body_parts)
    footnotes = _dedupe_labels(footnotes)
    return body, footnotes, {"extractor": "pymupdf4llm-layout", "page_numbering": "printed" if printed != list(range(1, len(printed) + 1)) else "pdf"}


def _looks_like_footnote(box, page) -> bool:
    """Backstop for the layout model: small type, bottom third of the page, starts with a note number."""
    if box.boxclass not in ("text", "list-item") or not box.textlines:
        return False
    sizes = [
        s["size"]
        for b in page.boxes
        if b.boxclass == "text" and b.textlines
        for l in b.textlines
        for s in l["spans"]
        for _ in range(max(1, len(s["text"]) // 20))
    ]
    if not sizes:
        return False
    body_size = sorted(sizes)[len(sizes) // 2]
    own = max(s["size"] for l in box.textlines for s in l["spans"])
    first = "".join(s["text"] for s in box.textlines[0]["spans"]).lstrip()
    return box.y0 > page.height * 0.66 and own < body_size * 0.85 and bool(re.match(r"^\d{1,3}\s", first))


def _pdf_plain(doc):
    import pymupdf4llm

    chunks = pymupdf4llm.to_markdown(doc, page_chunks=True, show_progress=False)
    parts = []
    for i, ch in enumerate(chunks):
        parts.append(PAGE_MARK.format(i + 1) + "\n\n" + _clean_pdf_md(ch.get("text", "")))
    body, footnotes = _split_inline_footnotes("\n\n".join(parts))
    return body, footnotes, {"extractor": "pymupdf4llm"}


def _printed_page_numbers(parsed) -> list[int]:
    """Use journal pagination when headers/footers carry a consistent page number."""
    n = len(parsed.pages)
    offsets = Counter()
    for idx, page in enumerate(parsed.pages):
        for b in page.boxes:
            if b.boxclass in ("page-header", "page-footer") and b.textlines:
                txt = " ".join(s["text"] for l in b.textlines for s in l["spans"])
                for num in re.findall(r"(?<![\d.])(\d{1,4})(?![\d.])", txt):
                    offsets[int(num) - (idx + 1)] += 1
    if offsets:
        off, cnt = offsets.most_common(1)[0]
        if cnt >= max(2, n // 2) and off >= 0:
            return [i + 1 + off for i in range(n)]
    return list(range(1, n + 1))


def _clean_pdf_md(md: str) -> str:
    md = nfc(md)
    md = re.sub(r"<sup>\s*(\d{1,3})\s*</sup>", r"[^\1]", md)  # superscript footnote references
    md = re.sub(r"(\w)-\n(\w)", r"\1\2", md)  # hyphenation across lines
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def _parse_footnote_md(md: str, page: str) -> list[tuple[str, str, str]]:
    out = []
    for block in re.split(r"\n\s*\n", md):
        block = block.strip()
        if not block:
            continue
        block = re.sub(r"^>\s?", "", block, flags=re.M).strip()
        block = re.sub(r"<sup>\s*(\d+)\s*</sup>", r"\1 ", block, count=1)
        m = re.match(r"^\[?(\d{1,3})\]?[.)]?\s+(.*)$", block, re.S)
        if m:
            out.append((m.group(1), _clean_pdf_md(m.group(2)).replace("\n", " "), page))
        elif out:  # continuation of the previous footnote
            lab, txt, pg = out[-1]
            out[-1] = (lab, txt + " " + block.replace("\n", " "), pg)
        else:
            out.append((f"p{page}", block.replace("\n", " "), page))
    return out


def _dedupe_labels(footnotes):
    seen: Counter = Counter()
    out = []
    for lab, txt, pg in footnotes:
        seen[lab] += 1
        out.append((lab if seen[lab] == 1 else f"{lab}-{seen[lab]}", txt, pg))
    return out


def _split_inline_footnotes(md: str):
    """For extractors that leave footnotes inline: move `[^n]: ...` lines out of the body."""
    body, fns, page = [], [], None
    for line in md.splitlines():
        pm = PAGE_MARK_RE.search(line)
        if pm:
            page = pm.group(1)
        m = _FN_DEF.match(line.strip())
        if m:
            fns.append((m.group(1), m.group(2), page))
        else:
            body.append(line)
    return "\n".join(body), fns


def _pdf_meta(doc, body: str) -> dict:
    md = doc.metadata or {}
    meta: dict = {"pages": doc.page_count}
    title = (md.get("title") or "").strip()
    if not title or re.match(r"(?i)^(untitled|microsoft word|document\d*|.*\.docx?)$", title):
        h = re.search(r"^#{1,2}\s+(.+)$", body, re.M)
        title = h.group(1).strip("*_ ") if h else ""
    if title:
        meta["title"] = title
    if md.get("author"):
        meta["authors"] = [a.strip() for a in re.split(r";| and |,", md["author"]) if a.strip()]
    first = body[:4000]
    years = [int(y) for y in re.findall(r"\b(1[89]\d\d|20[0-4]\d)\b", first)]
    if years:
        meta["year_guess"] = Counter(years).most_common(1)[0][0]
    return meta


def _try_marker(src: Path):
    try:
        from marker.converters.pdf import PdfConverter
        from marker.models import create_model_dict
        from marker.output import text_from_rendered
    except ImportError:
        return None
    eprint(f"    poor text yield; retrying {src.name} with marker-pdf")
    conv = PdfConverter(artifact_dict=create_model_dict(), config={"paginate_output": True})
    text, _, _ = text_from_rendered(conv(str(src)))
    # marker paginates as "{N}----" lines; turn them into our page markers (N is 0-based)
    text = re.sub(r"^\{(\d+)\}-{6,}\s*$", lambda m: PAGE_MARK.format(int(m.group(1)) + 1), text, flags=re.M)
    return nfc(text), {"extractor": "marker-pdf"}


# =========================================================================== quality

_WORDLIKE = re.compile(
    r"^[(\[\"'«„“‘]*"
    r"(?:[A-Za-zÀ-ÖØ-öø-ÿĀ-žͰ-Ͽἀ-῿֐-׿'’\-]{2,}|\d+[\d:.,–\-]*[a-z]?)"
    r"[)\]\"'»”’.,;:!?*_]*$"
)


def text_quality(text: str, n_pages: int, judge: bool = True) -> dict:
    """Text-yield statistics; `judge` applies the poor-yield thresholds (PDF only)."""
    stripped = PAGE_MARK_RE.sub("", text)
    chars = len(re.sub(r"\s", "", stripped))
    toks = re.findall(r"\S+", re.sub(r"[#>*_`|]", " ", stripped))
    wordlike = sum(1 for t in toks if _WORDLIKE.match(t)) / max(1, len(toks))
    bad = stripped.count("�") + stripped.count("(cid:") + sum(1 for c in stripped if "" <= c <= "")
    lines = [l for l in stripped.splitlines() if l.strip()]
    short = sum(1 for l in lines if len(l.strip()) < 20) / max(1, len(lines))
    cpp = chars / max(1, n_pages)
    reasons = []
    if cpp < MIN_CHARS_PER_PAGE:
        reasons.append(f"{cpp:.0f} chars/page < {MIN_CHARS_PER_PAGE}")
    if wordlike < MIN_WORDLIKE_RATIO:
        reasons.append(f"word-like token ratio {wordlike:.2f} < {MIN_WORDLIKE_RATIO}")
    if bad / max(1, chars) > MAX_BAD_GLYPH_RATIO:
        reasons.append("undecodable glyphs (cid/U+FFFD/private-use)")
    if short > MAX_SHORT_LINE_RATIO and len(lines) > 20:
        reasons.append(f"fragmented lines ({short:.0%} under 20 chars): likely column interleaving")
    score = min(1.0, cpp / 2000) * 0.4 + wordlike * 0.4 + (1 - min(1.0, short)) * 0.2
    return {
        "chars_per_page": round(cpp),
        "wordlike_ratio": round(wordlike, 3),
        "bad_glyphs": bad,
        "short_line_ratio": round(short, 3),
        "score": round(score, 3),
        "poor": bool(reasons) and judge,
        "reasons": reasons if judge else [],
    }
