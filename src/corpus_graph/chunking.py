"""Section-aware chunking and quote location in the ingested Markdown.

A chunk is a run of whole sections (split at paragraph boundaries only when a
single section exceeds the budget). Each chunk carries the footnotes it
references, so a claim in the body and the citation in its note are seen
together; for PDFs whose note markers were lost, the footnotes of the pages the
chunk covers are attached instead.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml
from rapidfuzz import fuzz

from .ingest import FOOTNOTE_HEADING, PAGE_MARK_RE
from .util import estimate_tokens, match_text

DEFAULT_CHUNK_TOKENS = 6000
_HEADING = re.compile(r"^(#{1,3})\s+\S")
_FN_DEF = re.compile(r"^\[\^([^\]]+)\]:\s?(.*)$")
_FN_REF = re.compile(r"\[\^([^\]]+)\](?!:)")
ABSTRACT_HEADINGS = re.compile(r"(?i)^#+\s*(abstract|summary|zusammenfassung|résumé|resume|riassunto|sommario)\b")


@dataclass
class Footnote:
    label: str
    text: str
    page: str | None
    line: int


@dataclass
class Chunk:
    id: str
    index: int
    text: str
    footnotes: list[Footnote] = field(default_factory=list)
    line_start: int = 0
    line_end: int = 0
    pages: list[str] = field(default_factory=list)
    tokens: int = 0

    def verification_text(self) -> str:
        return self.text + "\n" + "\n".join(f.text for f in self.footnotes)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ParsedMarkdown:
    meta: dict
    lines: list[str]  # the whole file, frontmatter included (1-based line numbers index into it)
    body_start: int  # 0-based index of first body line
    footnote_start: int | None
    footnotes: dict[str, Footnote]
    page_at: list[str | None]  # page in force at each 0-based line

    @classmethod
    def load(cls, path: Path) -> "ParsedMarkdown":
        text = path.read_text(encoding="utf-8")
        lines = text.split("\n")
        meta, body_start = {}, 0
        if lines and lines[0].strip() == "---":
            for i in range(1, len(lines)):
                if lines[i].strip() == "---":
                    meta = yaml.safe_load("\n".join(lines[1:i])) or {}
                    body_start = i + 1
                    break
        page_at: list[str | None] = []
        page = None
        fn_start = None
        for i, line in enumerate(lines):
            m = PAGE_MARK_RE.search(line)
            if m:
                page = m.group(1)
            if line.strip() == FOOTNOTE_HEADING and fn_start is None:
                fn_start = i
                page = None  # footnote section carries its own page markers
            page_at.append(page)
        footnotes: dict[str, Footnote] = {}
        if fn_start is not None:
            for i in range(fn_start + 1, len(lines)):
                m = _FN_DEF.match(lines[i])
                if m:
                    footnotes[m.group(1)] = Footnote(m.group(1), m.group(2), page_at[i], i + 1)
        return cls(meta, lines, body_start, fn_start, footnotes, page_at)

    # ------------------------------------------------------------ chunking
    def header(self, max_abstract_words: int = 150) -> str:
        m = self.meta
        authors = m.get("authors") or []
        if isinstance(authors, str):
            authors = [authors]
        parts = [f"Title: {m.get('title') or 'unknown'}"]
        if authors:
            parts.append(f"Authors: {'; '.join(authors)}")
        if m.get("year"):
            parts.append(f"Year: {m['year']}")
        if m.get("venue"):
            parts.append(f"Published in: {m['venue']}")
        abstract = self.abstract(max_abstract_words)
        if abstract:
            parts.append(f"Abstract: {abstract}")
        return "\n".join(parts)

    def abstract(self, max_words: int) -> str:
        end = self.footnote_start if self.footnote_start is not None else len(self.lines)
        body = self.lines[self.body_start:end]
        start = None
        for i, l in enumerate(body):
            if ABSTRACT_HEADINGS.match(l):
                start = i + 1
                break
        paras, cur = [], []
        for l in body[start or 0:]:
            if PAGE_MARK_RE.search(l):
                continue
            if l.startswith("#"):
                if cur:
                    paras.append(" ".join(cur))
                    cur = []
                if start is not None and paras:
                    break
                continue
            if not l.strip():
                if cur:
                    paras.append(" ".join(cur))
                    cur = []
                    if start is not None or len(" ".join(paras).split()) > 40:
                        break
                continue
            cur.append(l.strip())
        if cur:
            paras.append(" ".join(cur))
        words = _FN_REF.sub("", " ".join(paras)).split()
        return " ".join(words[:max_words]) + (" …" if len(words) > max_words else "")

    def chunks(self, max_tokens: int = DEFAULT_CHUNK_TOKENS) -> list[Chunk]:
        end = self.footnote_start if self.footnote_start is not None else len(self.lines)
        # sections: list of (start_line_idx, end_line_idx) half-open
        sections: list[tuple[int, int]] = []
        cur = self.body_start
        for i in range(self.body_start, end):
            if _HEADING.match(self.lines[i]) and i > cur:
                sections.append((cur, i))
                cur = i
        if cur < end:
            sections.append((cur, end))

        def cost(a: int, b: int) -> int:
            text = "\n".join(self.lines[a:b])
            fn = self._footnotes_for(a, b, text)
            return estimate_tokens(text) + sum(estimate_tokens(f.text) for f in fn)

        pieces: list[tuple[int, int]] = []
        for a, b in sections:
            if cost(a, b) <= max_tokens:
                pieces.append((a, b))
                continue
            # split an oversized section at blank lines
            start = a
            last_break = None
            for i in range(a, b):
                if not self.lines[i].strip():
                    if cost(start, i) > max_tokens and last_break and last_break > start:
                        pieces.append((start, last_break))
                        start = last_break
                    last_break = i
            pieces.append((start, b))

        packed: list[tuple[int, int]] = []
        for a, b in pieces:
            if packed and cost(packed[-1][0], b) <= max_tokens:
                packed[-1] = (packed[-1][0], b)
            else:
                packed.append((a, b))

        out = []
        for a, b in packed:
            text = "\n".join(self.lines[a:b]).strip()
            if not re.sub(r"<!--.*?-->|\s", "", text):
                continue
            fns = self._footnotes_for(a, b, text)
            pages = []
            for i in range(a, b):
                p = self.page_at[i]
                if p is not None and (not pages or pages[-1] != p):
                    pages.append(p)
            ch = Chunk(f"c{len(out):03d}", len(out), text, fns, a + 1, b, pages)
            ch.tokens = estimate_tokens(text) + sum(estimate_tokens(f.text) for f in fns)
            out.append(ch)
        return out

    def _footnotes_for(self, a: int, b: int, text: str) -> list[Footnote]:
        refs = _FN_REF.findall(text)
        if refs:
            seen, out = set(), []
            for r in refs:
                if r in self.footnotes and r not in seen:
                    seen.add(r)
                    out.append(self.footnotes[r])
            return out
        pages = {self.page_at[i] for i in range(a, b)} - {None}
        return [f for f in self.footnotes.values() if f.page in pages] if pages else []


# --------------------------------------------------------------------------- quote location


class QuoteLocator:
    """Find a quote in a Markdown file and report its 1-based line and page."""

    def __init__(self, md: ParsedMarkdown):
        self.md = md
        self.norm_parts: list[str] = []
        self.offsets: list[int] = []
        pos = 0
        for line in md.lines:
            n = match_text(line)
            self.offsets.append(pos)
            self.norm_parts.append(n)
            pos += len(n) + 1
        self.norm = " ".join(self.norm_parts)

    def locate(self, quote: str) -> tuple[int, str | None] | None:
        q = match_text(quote)
        if not q:
            return None
        body_off = self.offsets[self.md.body_start] if self.md.body_start < len(self.offsets) else 0
        i = self.norm.find(q, body_off)  # prefer the body over the frontmatter
        if i < 0:
            i = self.norm.find(q)
        if i < 0:
            return None
        li = bisect.bisect_right(self.offsets, i) - 1
        return li + 1, self.md.page_at[li]


def verify_quote(quote: str, chunk_text: str, fuzzy_threshold: float = 92.0) -> tuple[str, str] | None:
    """Return (status, matched_text) if the quote occurs in the chunk, else None.

    status is "exact" (after whitespace/punctuation normalisation) or "fuzzy"
    (OCR-level noise only; the threshold rejects paraphrase).
    """
    q = match_text(quote)
    if not q:
        return None
    hay = match_text(chunk_text)
    if q in hay:
        return "exact", quote
    if len(q) < 12:
        return None
    al = fuzz.partial_ratio_alignment(q, hay, score_cutoff=fuzzy_threshold)
    if al is None:
        return None
    return "fuzzy", hay[al.dest_start:al.dest_end]
