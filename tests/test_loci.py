import pytest

from corpus_graph.loci import default_parser

P = default_parser()


def names(text, lang="en"):
    return [l.canonical_name for l in P.parse(text, lang)]


@pytest.mark.parametrize("text,lang", [("Mk 6.37", "en"), ("Mark 6:37", "en"), ("Mc 6,37", "fr"), ("Mk 6,37", "de"), ("Mc 6,37", "it")])
def test_brief_example_resolves_to_mark_6_37(text, lang):
    loci = P.parse(text, lang)
    assert [l.canonical_name for l in loci] == ["Mark 6:37"]
    assert loci[0].node_id == "pt:mark.6.37"


@pytest.mark.parametrize("text,lang,expected", [
    ("Mt 20,1-16", "de", ["Matt 20:1–16"]),
    ("Mark 6:30–7:2", "en", ["Mark 6:30–7:2"]),
    ("Mk 6,37.39", "de", ["Mark 6:37", "Mark 6:39"]),          # German verse list
    ("Mark 6:37, 39", "en", ["Mark 6:37", "Mark 6:39"]),       # English verse list
    ("Mark 6:37; 12:15", "en", ["Mark 6:37", "Mark 12:15"]),   # book carried over
    ("Mark 6:37; 1 Cor 8:9", "en", ["Mark 6:37", "1 Cor 8:9"]),
    ("1. Kor 9,9", "de", ["1 Cor 9:9"]),
    ("First Corinthians 9:9", "en", ["1 Cor 9:9"]),
    ("II Kgs 4:1", "en", ["2 Kgs 4:1"]),
    ("Lk 15,8f.", "de", ["Luke 15:8–9"]),
    ("Matt 25:14ff.", "en", ["Matt 25:14ff."]),
    ("Phlm 9", "en", ["Phlm 9"]),
    ("Offb 6,6", "de", ["Rev 6:6"]),
    ("Gv 2,1-11", "it", ["John 2:1–11"]),
    ("3 John 7", "en", ["3 John 7"]),
    ("Mark 12", "en", ["Mark 12"]),
    ("Tac. Ann. 1.17", "en", ["Tacitus, Annals 1.17"]),
    ("Polyb. 6.39.12-13", "en", ["Polybius, Histories 6.39.12–13"]),
    ("Plin. nat. 33,44", "it", ["Pliny the Elder, Natural History 33.44"]),
    ("Cic. Off. 1,150", "de", ["Cicero, On Duties 1.150"]),
    ("m. B. Meṣ. 7:1", "en", ["Mishnah Bava Metzi'a 7:1"]),
    ("b. B. Meṣ. 83a", "en", ["Babylonian Talmud Bava Metzi'a 83a"]),
])
def test_conventions(text, lang, expected):
    assert names(text, lang) == expected


def test_language_decides_short_abbreviations():
    assert names("Gn 3,1", "fr") == ["Gen 3:1"]      # Genèse
    assert names("Gn 3,1", "it") == ["Jonah 3:1"]    # Giona (CEI)
    assert names("Ab 1,2", "it") == ["Hab 1:2"]      # Abacuc
    assert names("Ab 1", "fr") == ["Obad 1"]          # Abdias


def test_implausible_numbers_are_not_loci():
    assert names("Mark 2004") == []        # a year, not a chapter
    assert names("Jude 5-7") == ["Jude 5–7"]


def test_overlap_and_containment():
    chapter = P.parse("Mark 12")[0]
    verse = P.parse("Mark 12:15")[0]
    rng = P.parse("Mark 12:13-17")[0]
    other = P.parse("Mark 6:37")[0]
    assert chapter.contains(verse) and chapter.overlaps(rng) and rng.contains(verse)
    assert not chapter.overlaps(other)


def test_find_in_running_text():
    found = P.find("As Renner notes (Mt 20,2; cf. Tac. Ann. 1.17), the sum is conventional.", "de")
    assert [l.canonical_name for _, _, ls in found for l in ls] == ["Matt 20:2", "Tacitus, Annals 1.17"]
