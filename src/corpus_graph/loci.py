"""Deterministic parser for biblical and classical citations.

"Mk 6.37", "Mark 6:37", "Mc 6,37" and "Mt 20,1-16" resolve to canonical loci
(`Mark 6:37`, `Matt 20:1–16`); "Tac. Ann. 1.17" and "Polyb. 6.39.12-13" to
`Tacitus, Annals 1.17` and `Polybius, Histories 6.39.12–13`.

Separator conventions:
  * colon  ("6:37")  chapter:verse, commas separate verses   ("6:37, 39")
  * comma  ("6,37")  chapter,verse (DE/FR/IT), full stops separate verses ("6,37.39")
  * stop   ("6.37")  chapter.verse (British / classics), commas separate verses
  * ";" starts a new chapter reference in the same book ("Mark 6:37; 12:15")
Short abbreviations that differ between languages ("Gn" is Genesis in French,
Jonah in Italian) are decided by the document's language.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Iterable

import yaml

from .util import nfc, strip_marks

LANGS = ("en", "de", "fr", "it")
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}
_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "1st": 1, "2nd": 2, "3rd": 3, "4th": 4,
    "erster": 1, "zweiter": 2, "dritter": 3, "vierter": 4,
    "premier": 1, "premiere": 1, "deuxieme": 2, "troisieme": 3, "quatrieme": 4,
    "primo": 1, "prima": 1, "secondo": 2, "seconda": 2, "terzo": 3, "terza": 3, "quarto": 4,
}
_NUM_PREFIXES = {
    1: ["1", "I", "First", "1st", "Erster", "Premier", "Première", "Primo", "Prima"],
    2: ["2", "II", "Second", "2nd", "Zweiter", "Deuxième", "Secondo", "Seconda"],
    3: ["3", "III", "Third", "3rd", "Dritter", "Troisième", "Terzo", "Terza"],
    4: ["4", "IV", "Fourth", "4th", "Vierter", "Quatrième", "Quarto"],
}
_DASH = "-–—‐‑"
OPEN_RANGE = 2  # "6:37ff." is treated as 6:37–39 for overlap tests


# --------------------------------------------------------------------------- data model


@dataclass(frozen=True)
class Work:
    key: str
    kind: str  # "bible" | "classical"
    display: str
    author: str | None = None
    chapters: int | None = None  # bible only
    sep: str = "."  # classical display separator
    folio: bool = False  # Talmud-style folio references: 83a -> (83, 1), 83b -> (83, 2)


@dataclass(frozen=True)
class Locus:
    work: Work
    start: tuple[int, ...]  # bible: (chapter,) or (chapter, verse); classical: hierarchical
    end: tuple[int, ...]
    open_end: bool = False  # "ff."
    raw: str = field(default="", compare=False)

    # ------------------------------------------------------------ rendering
    def _fmt(self, sep_ch: str, sep_r: str, dash: str) -> str:
        if self.work.folio:
            return self._fmt_folio(dash)
        s, e = self.start, self.end
        head = sep_ch.join(map(str, s)) if self.work.kind == "bible" else sep_r.join(map(str, s))
        if self.work.kind == "bible" and self.work.chapters == 1 and len(s) == 2:
            head = str(s[1])
        if e != s:
            if self.work.kind == "bible":
                if len(s) == 2 and len(e) == 2 and e[0] == s[0]:
                    tail = str(e[1])
                elif self.work.chapters == 1 and len(e) == 2:
                    tail = str(e[1])
                else:
                    tail = sep_ch.join(map(str, e))
            else:
                n = 0
                while n < min(len(s), len(e)) - 1 and s[n] == e[n]:
                    n += 1
                tail = sep_r.join(map(str, e[n:])) if len(s) == len(e) else sep_r.join(map(str, e))
            head = f"{head}{dash}{tail}"
        if self.open_end:
            head += "ff." if dash == "–" else "ff"
        return head

    def _fmt_folio(self, dash: str) -> str:
        def f(t):
            return f"{t[0]}{'ab'[t[1] - 1]}" if len(t) > 1 else str(t[0])
        out = f(self.start)
        if self.end != self.start:
            out += dash + f(self.end)
        return out

    @property
    def canonical_name(self) -> str:
        sep = ":" if self.work.kind == "bible" else self.work.sep
        return f"{self.work.display} {self._fmt(':', sep, '–')}"

    @property
    def node_id(self) -> str:
        return f"pt:{self.work.key}.{self._fmt('.', '.', '-')}"

    def to_attrs(self) -> dict:
        return {
            "work": self.work.key,
            "work_display": self.work.display,
            "kind": self.work.kind,
            "start": list(self.start),
            "end": list(self.end),
            "open_end": self.open_end,
            "ancient_author": self.work.author,
        }

    # ------------------------------------------------------------ comparison
    def interval(self) -> tuple[tuple[int, ...], tuple[int, ...]]:
        if self.work.kind == "bible":
            s = self.start if len(self.start) == 2 else (self.start[0], 0)
            if len(self.end) == 2:
                e = (self.end[0], self.end[1] + (OPEN_RANGE if self.open_end else 0))
            else:
                e = (self.end[0], 10**6)
            return s, e
        pad = 4
        s = tuple(self.start) + (0,) * (pad - len(self.start))
        e = tuple(self.end) + (10**6,) * (pad - len(self.end))
        return s, e

    def overlaps(self, other: "Locus") -> bool:
        if self.work.key != other.work.key:
            return False
        (a0, a1), (b0, b1) = self.interval(), other.interval()
        return a0 <= b1 and b0 <= a1

    def contains(self, other: "Locus") -> bool:
        if self.work.key != other.work.key:
            return False
        (a0, a1), (b0, b1) = self.interval(), other.interval()
        return a0 <= b0 and b1 <= a1


def locus_from_attrs(attrs: dict, parser: "LocusParser") -> Locus | None:
    w = parser.works.get(attrs.get("work"))
    if not w:
        return None
    return Locus(w, tuple(attrs["start"]), tuple(attrs["end"]), bool(attrs.get("open_end")))


# --------------------------------------------------------------------------- alias normalisation


def _norm_alias(s: str, casefold: bool) -> str:
    s = strip_marks(nfc(s))
    s = s.replace(".", " ").replace(",", " ")
    s = re.sub(r"\s+", " ", s).strip()
    parts = s.split(" ")
    if len(parts) > 1:
        p0 = parts[0].casefold()
        if p0 in _ROMAN and parts[0].isupper():
            parts[0] = str(_ROMAN[p0])
        elif p0 in _ORDINALS:
            parts[0] = str(_ORDINALS[p0])
    s = " ".join(parts)
    s = re.sub(r"^(\d) ", r"\1", s)  # "1 cor" -> "1cor"
    s = s.replace(" ", "")
    return s.casefold() if casefold else s


def _alias_regex(alias: str) -> str:
    toks = [t for t in re.split(r"[\s.]+", alias) if t]
    return r"\.?\s*".join(re.escape(t) for t in toks)


def _numbered_regex(n: int, stem: str) -> str:
    prefixes = "|".join(re.escape(p) for p in sorted(_NUM_PREFIXES[n], key=len, reverse=True))
    return rf"(?:{prefixes})\.?\s*{_alias_regex(stem)}"


# --------------------------------------------------------------------------- parser


class LocusParser:
    """Finds and parses citations. Build once; reuse (construction compiles large regexes)."""

    def __init__(self, extra_works: Iterable[dict] = (), extra_book_aliases: dict | None = None):
        self.works: dict[str, Work] = {}
        # alias lookups: normalised alias -> {lang: work key}
        self._ci: dict[str, dict[str, str]] = {}
        self._cs: dict[str, dict[str, str]] = {}
        ci_pats: list[str] = []
        cs_pats: list[str] = []

        def add(alias: str, key: str, lang: str, pattern: str | None = None):
            short = len(re.sub(r"[\s.\d]", "", alias)) <= 3
            table, pats = (self._cs, cs_pats) if short else (self._ci, ci_pats)
            table.setdefault(_norm_alias(alias, not short), {}).setdefault(lang, key)
            pats.append(pattern or _alias_regex(alias))

        data = resources.files("corpus_graph.data")
        books = yaml.safe_load((data / "bible_books.yaml").read_text(encoding="utf-8"))
        for b in books:
            nums = b.get("numbered")
            if nums:
                chapters = b.get("chapters") or [None] * len(nums)
                for n, ch in zip(nums, chapters):
                    key = f"{n}{b['key'].replace('johnep', 'john')}"
                    self.works[key] = Work(key, "bible", f"{n} {b['sbl']}", chapters=ch)
                    for lang in LANGS:
                        for stem in b.get(lang, []):
                            for p in _NUM_PREFIXES[n]:
                                norm_short = len(stem) <= 3
                                t = self._cs if norm_short else self._ci
                                t.setdefault(_norm_alias(f"{p} {stem}", not norm_short), {}).setdefault(lang, key)
                            (cs_pats if len(stem) <= 3 else ci_pats).append(_numbered_regex(n, stem))
            else:
                key = b["key"]
                self.works[key] = Work(key, "bible", b["sbl"], chapters=b.get("chapters"))
                for lang in LANGS:
                    for alias in b.get(lang, []):
                        add(alias, key, lang)
                add(b["sbl"], key, "en")
        for alias, key in (extra_book_aliases or {}).items():
            add(alias, key, "en")

        classical = yaml.safe_load((data / "classical_works.yaml").read_text(encoding="utf-8"))
        for w in list(classical) + list(extra_works):
            key = w["key"]
            self.works[key] = Work(key, "classical", w["display"], w.get("author"), sep=w.get("sep", "."),
                                   folio=bool(w.get("folio")))
            for alias in list(w.get("abbr", [])) + [w["display"]]:
                add(alias, key, "*")

        def compile_alt(pats: list[str], flags: int) -> re.Pattern:
            uniq = sorted(set(pats), key=len, reverse=True)
            return re.compile(rf"(?<![\w])(?:{'|'.join(uniq)})\.?(?![^\W\d_])", flags)

        self._re_ci = compile_alt(ci_pats, re.IGNORECASE)
        self._re_cs = compile_alt(cs_pats, 0)

    # ------------------------------------------------------------ book lookup
    def _lookup(self, matched: str, lang: str) -> str | None:
        for table, cf in ((self._ci, True), (self._cs, False)):
            cand = table.get(_norm_alias(matched, cf))
            if cand:
                for lg in (lang, "*", "en", "de", "fr", "it"):
                    if lg in cand:
                        return cand[lg]
        return None

    def _book_matches(self, text: str) -> list[tuple[int, int, str]]:
        found = []
        for rx in (self._re_ci, self._re_cs):
            for m in rx.finditer(text):
                found.append((m.start(), m.end(), m.group(0)))
        # longest match wins at overlapping positions
        found.sort(key=lambda t: (t[0], -(t[1] - t[0])))
        out, last_end = [], -1
        for s, e, g in found:
            if s >= last_end:
                out.append((s, e, g))
                last_end = e
        return out

    # ------------------------------------------------------------ public API
    def find(self, text: str, lang: str = "en", allow_book_only: bool = False,
             allow_chapter_only: bool = True) -> list[tuple[int, int, list[Locus]]]:
        """Return (start, end, loci) for every citation in `text`."""
        text = nfc(text)
        results = []
        books = self._book_matches(text)
        book_starts = {s for s, _, _ in books}
        for s, e, g in books:
            key = self._lookup(g, lang)
            if not key:
                continue
            work = self.works[key]
            loci, end = _parse_refs(text, e, work, book_starts, allow_chapter_only)
            loci = [l for l in loci if _plausible(work, *l)]
            if loci:
                results.append((s, end, [Locus(work, a, b, o, raw=text[s:end].strip()) for a, b, o in loci]))
            elif allow_book_only and work.kind == "bible":
                results.append((s, e, [Locus(work, (1,), (work.chapters or 150,), raw=g)]))
        return results

    def parse(self, citation: str, lang: str = "en") -> list[Locus]:
        """Parse a citation string (e.g. an extracted PrimaryText name) into loci."""
        out: list[Locus] = []
        for _, _, loci in self.find(citation, lang):
            out.extend(loci)
        seen, uniq = set(), []
        for l in out:
            if l.node_id not in seen:
                seen.add(l.node_id)
                uniq.append(l)
        return uniq

    def work_of_author(self, author: str) -> list[Work]:
        return [w for w in self.works.values() if w.author == author]


@lru_cache(maxsize=1)
def default_parser() -> LocusParser:
    return LocusParser()


def _plausible(work: Work, start, end, open_end) -> bool:
    if any(n <= 0 for n in start) or any(n <= 0 for n in end):
        return False
    if work.kind == "bible":
        if work.chapters and (start[0] > work.chapters or end[0] > work.chapters):
            return False
        if len(start) == 2 and start[1] > 200:
            return False
        if end < start:
            return False
    return True


# --------------------------------------------------------------------------- reference grammar

_TOK = re.compile(r"\s*(?:(?P<num>\d+)(?P<suf>[a-e](?![a-z]))?|(?P<ff>ff?\.?)(?![a-z])|(?P<sep>[:,.;])|(?P<dash>[" + _DASH + "]))")


def _tokens(text: str, pos: int, limit: int = 120):
    """Tokenise a reference string starting at `pos` until something that is not part of it."""
    toks = []
    end = min(len(text), pos + limit)
    while pos < end:
        m = _TOK.match(text, pos)
        if not m or m.end() == pos:
            break
        if m.group("num") is not None:
            # verse parts (37a) collapse to the verse; folio sides are kept in the 5th slot
            toks.append(("num", int(m.group("num")), m.start("num"), m.end(), m.group("suf")))
        elif m.group("ff"):
            toks.append(("ff", 2 if m.group("ff").startswith("ff") else 1, m.start(), m.end(), None))
        elif m.group("sep"):
            toks.append(("sep", m.group("sep"), m.start("sep"), m.end(), None))
        else:
            toks.append(("dash", "-", m.start("dash"), m.end(), None))
        pos = m.end()
    return toks


def _parse_refs(text: str, pos: int, work: Work, book_starts: set[int], allow_chapter_only: bool):
    toks = _tokens(text, pos)
    if not toks or toks[0][0] != "num":
        return [], pos
    if work.folio:
        return _parse_folio(toks)
    if work.kind == "classical":
        return _parse_classical(toks, work)
    return _parse_bible(toks, work, book_starts, allow_chapter_only)


def _parse_classical(toks, work):
    """NUM (SEP NUM)* [ff] [DASH NUM (SEP NUM)*] ; ... ; ..."""
    loci, i, end_pos = [], 0, toks[0][2]
    while i < len(toks) and toks[i][0] == "num":
        start = [toks[i][1]]
        end_pos = toks[i][3]
        i += 1
        while i + 1 < len(toks) and toks[i][0] == "sep" and toks[i][1] in ".,:" and toks[i + 1][0] == "num":
            start.append(toks[i + 1][1])
            end_pos = toks[i + 1][3]
            i += 2
        open_end = False
        endt = list(start)
        if i < len(toks) and toks[i][0] == "ff":
            open_end = toks[i][1] == 2
            if toks[i][1] == 1:
                endt[-1] += 1
            end_pos = toks[i][3]
            i += 1
        elif i + 1 < len(toks) and toks[i][0] == "dash" and toks[i + 1][0] == "num":
            tail = [toks[i + 1][1]]
            end_pos = toks[i + 1][3]
            i += 2
            while i + 1 < len(toks) and toks[i][0] == "sep" and toks[i][1] in ".,:" and toks[i + 1][0] == "num":
                tail.append(toks[i + 1][1])
                end_pos = toks[i + 1][3]
                i += 2
            endt = start[: len(start) - len(tail)] + tail if len(tail) <= len(start) else tail
        loci.append((tuple(start), tuple(endt), open_end))
        if i + 1 < len(toks) and toks[i][0] == "sep" and toks[i][1] == ";" and toks[i + 1][0] == "num":
            i += 1
            continue
        break
    return loci, end_pos


def _parse_folio(toks):
    """Talmud folios: 83a, 83a-b, 83a-84b."""
    def side(t):
        return (t[1], {"a": 1, "b": 2}.get(t[4] or "", 1)) if t[4] in ("a", "b") else (t[1],)
    start = side(toks[0])
    end, end_pos = start, toks[0][3]
    if len(toks) > 2 and toks[1][0] == "dash" and toks[2][0] == "num":
        end, end_pos = side(toks[2]), toks[2][3]
    return [(start, end, False)], end_pos


def _parse_bible(toks, work, book_starts, allow_chapter_only):
    single = work.chapters == 1
    loci = []
    i = 0
    end_pos = toks[0][2]
    while i < len(toks) and toks[i][0] == "num":
        if toks[i][2] in book_starts:  # "Mark 6:37; 1 Cor 8:9" - the "1" starts a new book
            break
        ch = toks[i][1]
        ch_end_pos = toks[i][3]
        i += 1
        sep = toks[i][1] if i < len(toks) and toks[i][0] == "sep" and toks[i][1] in ":,." else None
        has_verse = sep is not None and i + 1 < len(toks) and toks[i + 1][0] == "num" and toks[i + 1][2] not in book_starts
        if single and not has_verse:
            # "Phlm 9", "Jude 5-7": the number is a verse
            v = ch
            loci_item, i, end_pos = _verse_item(toks, i, 1, v, ch_end_pos, ":", single=True)
            loci.append(loci_item)
        elif not has_verse:
            if not allow_chapter_only:
                break
            if i + 1 < len(toks) and toks[i][0] == "dash" and toks[i + 1][0] == "num":
                loci.append(((ch,), (toks[i + 1][1],), False))
                end_pos = toks[i + 1][3]
                i += 2
            elif i < len(toks) and toks[i][0] == "ff":
                loci.append(((ch,), (ch + 1,), False))
                end_pos = toks[i][3]
                i += 1
            else:
                loci.append(((ch,), (ch,), False))
                end_pos = ch_end_pos
        else:
            list_sep = "," if sep in ":." else "."
            i += 1  # consume ch/v separator
            first = True
            while i < len(toks) and toks[i][0] == "num":
                v = toks[i][1]
                item, i, end_pos = _verse_item(toks, i + 1, ch, v, toks[i][3], sep)
                loci.append(item)
                first = False
                # another verse in the same chapter?
                if (
                    i + 1 < len(toks)
                    and toks[i][0] == "sep"
                    and toks[i][1] == list_sep
                    and toks[i + 1][0] == "num"
                    and toks[i + 1][2] not in book_starts
                    and not _is_chapter_verse(toks, i + 1, sep)
                ):
                    i += 1
                    continue
                break
            if first:
                break
        # next chapter reference in the same book?
        if i + 1 < len(toks) and toks[i][0] == "sep" and toks[i][1] == ";" and toks[i + 1][0] == "num":
            i += 1
            continue
        # English style "Mark 6:37, 12:15" (comma then a new chapter:verse)
        if i + 1 < len(toks) and toks[i][0] == "sep" and toks[i][1] == "," and _is_chapter_verse(toks, i + 1, ":"):
            i += 1
            continue
        break
    return loci, end_pos


def _is_chapter_verse(toks, i, sep):
    return (
        i + 2 < len(toks)
        and toks[i][0] == "num"
        and toks[i + 1][0] == "sep"
        and toks[i + 1][1] == sep
        and sep == ":"
        and toks[i + 2][0] == "num"
    )


def _verse_item(toks, i, ch, v, end_pos, sep, single=False):
    """After `ch SEP v`: optional ff / f / dash (verse or chapter:verse)."""
    start = (ch, v)
    end = (ch, v)
    open_end = False
    if i < len(toks) and toks[i][0] == "ff":
        if toks[i][1] == 1:
            end = (ch, v + 1)
        else:
            open_end = True
        end_pos = toks[i][3]
        i += 1
    elif i + 1 < len(toks) and toks[i][0] == "dash" and toks[i + 1][0] == "num":
        n = toks[i + 1][1]
        end_pos = toks[i + 1][3]
        i += 2
        if (
            not single
            and i + 1 < len(toks)
            and toks[i][0] == "sep"
            and toks[i][1] == sep
            and toks[i + 1][0] == "num"
        ):
            end = (n, toks[i + 1][1])  # cross-chapter range 6:30-7:2
            end_pos = toks[i + 1][3]
            i += 2
        else:
            end = (ch, n)
    return (start, end, open_end), i, end_pos
