"""Generate a small two-page journal-style PDF (running heads, footnotes, Greek) for ingest tests."""
import sys
import pymupdf

BODY = [
    "<h1>Coins and Wages in Galilee</h1><p><i>Helena Marwick</i></p>"
    "<p>The parable of the labourers (Matt 20:1–16) fixes the wage at one denarius (δηνάριον) for a day's work.<sup>1</sup> "
    "Mark 6:37 imagines two hundred denarii worth of bread.<sup>2</sup> " + "The economic setting matters. " * 30 + "</p>",
    "<h2>The Tribute Coin</h2><p>In Mark 12:15 Jesus asks for a denarius, and the Hebrew term דינר appears in later rabbinic texts.<sup>3</sup> "
    + "Coinage circulated unevenly across the region. " * 30 + "</p>",
]
NOTES = [
    ["<sup>1</sup> See Renner 2021, 44–47, who disputes the equation of the denarius with a subsistence wage.",
     "<sup>2</sup> Cf. Tob 5:15, where a drachma a day is promised."],
    ["<sup>3</sup> m. B. Meṣ. 4:1 discusses the exchange of coins."],
]

def main(path, first_page=101):
    doc = pymupdf.open()
    for i, (body, notes) in enumerate(zip(BODY, NOTES)):
        page = doc.new_page(width=595, height=842)
        page.insert_htmlbox(pymupdf.Rect(60, 30, 535, 50), f"<p style='font-size:8pt'>JOURNAL OF ANCIENT ECONOMIES 12 (2019)&nbsp;&nbsp;&nbsp;&nbsp;{first_page + i}</p>")
        page.insert_htmlbox(pymupdf.Rect(60, 70, 535, 680), f"<div style='font-size:11pt'>{body}</div>")
        page.draw_line((60, 700), (200, 700))
        page.insert_htmlbox(pymupdf.Rect(60, 705, 535, 800), "<div style='font-size:8pt'>" + "".join(f"<p>{n}</p>" for n in notes) + "</div>")
    doc.set_metadata({"title": "Coins and Wages in Galilee", "author": "Helena Marwick"})
    doc.save(path)

if __name__ == "__main__":
    main(sys.argv[1])
