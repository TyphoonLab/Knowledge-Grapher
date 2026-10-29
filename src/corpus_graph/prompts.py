"""Extraction and merge-judging prompts plus their JSON schemas.

Bump PROMPT_VERSION whenever the prompt or schema changes in a way that should
invalidate cached extractions.
"""

from __future__ import annotations

from .schema import Schema

PROMPT_VERSION = "2026-10-29.1"

SYSTEM_TEMPLATE = """\
You are a research assistant building a citation-grade knowledge graph from scholarship in New Testament studies, classics and the ancient economy. You read one chunk of one article at a time and return JSON only.

{schema}

What to extract
- Scholar: every modern scholar who is an author of this document or whose work is cited or discussed. Give `name` as written in the fullest form available in the chunk (e.g. "Helena Marwick", or "Marwick" if only the surname appears), plus `surname`, `given_names` (full names or initials if known, else null), and `cited_years` (publication years attached to them in this chunk, e.g. "Marwick 2019, 44" -> [2019]). Set `is_document_author` true only for the document's own author(s).
- AncientAuthor: ancient writers named as authors (Pliny the Elder, Josephus, Philo, Tacitus). Use the English conventional name.
- PrimaryText: every cited locus of an ancient text. Put the citation exactly as written in `citation` ("Mt 20,2", "Tac. Ann. 1.17", "m. B. Meṣ. 7:1"). Resolve relative references from context: if the text says "v. 15" while discussing Mark 12, the citation is "Mark 12:15". If several loci are cited together ("Mk 6,37; 12,15"), emit one node per locus. Set `name` to the same string as `citation`.
- Object: coin types (denarius, drachma, lepton, quadrans, talent as a unit), inscriptions, papyri, ostraca and other artifacts. Use the conventional sigla for papyri and inscriptions (P.Oxy. 1.37, CIL VI 1585).
- Concept: themes, technical terms and analytical categories (subsistence wage, monetisation, patronage). Use a short English noun phrase, singular, lower case unless a proper noun. Put Greek, Hebrew, Latin, German, French or Italian forms of the same concept in `aliases`, in their original script (δηνάριον, דינר, Tagelohn); never transliterate.
- Place, Period: as named in the text, English conventional form; original-language forms in `aliases`.
- Argument: a distinct scholarly claim that someone advances or disputes. `name` must be a single declarative proposition in English, lower case, at most 15 words, phrased generically enough that the same claim made in another article would get the same wording (e.g. "the denarius was a day's wage for an unskilled labourer"). Do not make Arguments out of mere descriptions of a text.

Edges
- Use only the relations listed above, in the stated direction. "DOC" is the id of the document you are reading.
- Document -> Argument `advances` when the document's author makes the claim; Scholar -> Argument `argues` for whoever makes it (including the document's author); `disputes` for whoever rejects it.
- Scholar -> Scholar `disagrees_with` when one scholar contests another's view (signals: "pace", "contra", "against", "gegen", "contre", "contro", "rightly criticised"), `builds_on` when one explicitly develops another's work. The document's author is a Scholar node too.
- Argument -> PrimaryText `grounded_in` when the claim rests on that passage; Argument -> Concept/Object/Place/Period `about` for what the claim is about.
- Object -> PrimaryText `attested_in` when a passage mentions the object (e.g. the denarius in Matt 20:2).
- `related_to` between Concepts only as a last resort.
- `confidence` is "extracted" when the text states the relation and "inferred" when it is clearly implied but not stated.
- If a relation you need is not in the list, do not force it into one: add it to `schema_proposals`.

Provenance (mandatory)
- Every node and every edge needs `quote`: a verbatim span of at most 25 words copied exactly from the chunk or its footnotes (not from the document header) that supports it. Copy characters exactly, including Greek and Hebrew, punctuation and footnote numbers; do not paraphrase, translate, correct or join non-adjacent text. Items without a verifiable quote are discarded.
- Prefer the shortest span that supports the item. For PrimaryText the quote should contain the citation.

General
- Node `id`s are local to this chunk: n1, n2, ...; edges refer to them or to "DOC".
- `document_meta`: fill it only if the chunk shows the article's title, author(s), year or venue (usually the first chunk); otherwise null.
- `note`: optional, at most 20 words, for anything a curator should know (e.g. an ambiguous name). Otherwise null.
- Extract what the text says, not what you know. Do not add nodes or relations from background knowledge.
"""

USER_TEMPLATE = """\
<document_header>
{header}
</document_header>

<chunk id="{chunk_id}" pages="{pages}">
{text}
</chunk>
{footnotes}
Extract the nodes and edges from this chunk following the instructions. Return only the JSON object."""


def _nullable(t: dict) -> dict:
    return {"anyOf": [t, {"type": "null"}]}


def extraction_schema(schema: Schema) -> dict:
    node = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "type", "name", "aliases", "quote", "surname", "given_names", "cited_years",
                     "is_document_author", "citation", "note"],
        "properties": {
            "id": {"type": "string"},
            "type": {"type": "string", "enum": schema.extractable_node_types},
            "name": {"type": "string"},
            "aliases": {"type": "array", "items": {"type": "string"}},
            "quote": {"type": "string"},
            "surname": _nullable({"type": "string"}),
            "given_names": _nullable({"type": "string"}),
            "cited_years": {"type": "array", "items": {"type": "integer"}},
            "is_document_author": {"type": "boolean"},
            "citation": _nullable({"type": "string"}),
            "note": _nullable({"type": "string"}),
        },
    }
    edge = {
        "type": "object",
        "additionalProperties": False,
        "required": ["source", "target", "relation", "quote", "confidence"],
        "properties": {
            "source": {"type": "string"},
            "target": {"type": "string"},
            "relation": {"type": "string", "enum": schema.extractable_relations},
            "quote": {"type": "string"},
            "confidence": {"type": "string", "enum": ["extracted", "inferred"]},
        },
    }
    proposal = {
        "type": "object",
        "additionalProperties": False,
        "required": ["relation", "source", "target", "source_type", "target_type", "quote", "rationale"],
        "properties": {k: {"type": "string"} for k in
                       ["relation", "source", "target", "source_type", "target_type", "quote", "rationale"]},
    }
    meta = {
        "type": "object",
        "additionalProperties": False,
        "required": ["title", "authors", "year", "venue"],
        "properties": {
            "title": _nullable({"type": "string"}),
            "authors": {"type": "array", "items": {"type": "string"}},
            "year": _nullable({"type": "integer"}),
            "venue": _nullable({"type": "string"}),
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["document_meta", "nodes", "edges", "schema_proposals"],
        "properties": {
            "document_meta": _nullable(meta),
            "nodes": {"type": "array", "items": node},
            "edges": {"type": "array", "items": edge},
            "schema_proposals": {"type": "array", "items": proposal},
        },
    }


def system_prompt(schema: Schema) -> str:
    return SYSTEM_TEMPLATE.format(schema=schema.describe_for_prompt())


def user_prompt(header: str, chunk) -> str:
    fns = ""
    if chunk.footnotes:
        fns = "\n<footnotes_for_this_chunk>\n" + "\n".join(
            f"[^{f.label}]: {f.text}" for f in chunk.footnotes
        ) + "\n</footnotes_for_this_chunk>\n"
    pages = "-".join([chunk.pages[0], chunk.pages[-1]]) if len(chunk.pages) > 1 else (chunk.pages[0] if chunk.pages else "")
    return USER_TEMPLATE.format(header=header, chunk_id=chunk.id, pages=pages, text=chunk.text, footnotes=fns)


# --------------------------------------------------------------------------- merge judging

MERGE_SYSTEM = """\
You are curating entity resolution for a knowledge graph of scholarship on the New Testament, classics and the ancient economy. For each candidate pair, decide whether the two names refer to the same entity of the given type, using only the names, aliases and the short contexts supplied. Be conservative: "same" only when you are confident; different loci, different coins, different scholars with the same surname, or a general concept versus a specific claim are "different". Use "unsure" when the evidence does not settle it.
"""

MERGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "verdict", "reason"],
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["same", "different", "unsure"]},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}
