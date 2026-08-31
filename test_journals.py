#!/usr/bin/env python3
"""Testy scraperu časopisů.

Weby časopisů se mění a jejich HTML nejde z vývojového prostředí stáhnout,
takže se testuje nad uloženými kopiemi stránek (tests/fixtures).

Spuštění: python test_journals.py
"""

import sys

from bs4 import BeautifulSoup

import scraper_journals as s

FIX_JURISPRUDENCE = "tests/fixtures/jurisprudence_archiv_1-2026.html"

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("  OK   " if cond else "  CHYBA") + f" {name}"
          + (f" – {detail}" if detail and not cond else ""))


# =====================================================================
print("1) Jurisprudence – obsah čísla ze stránky archivu")
# =====================================================================
# Stránka archivu vypisuje celý strom ročníků a k tomu rozbalený obsah
# jednoho čísla. Dvě čísla z roku 2013 mají adresu ve tvaru článku, takže
# hledání podle tvaru adresy je vytáhlo mezi články.
soup = BeautifulSoup(open(FIX_JURISPRUDENCE, encoding="utf-8").read(), "html.parser")
items = s._jurisprudence_articles(
    soup, "https://www.jurisprudence.cz/cz/casopis/archiv/1-2026")

check("naparsovány všechny články čísla", len(items) == 11, str(len(items)))
check("čísla z jiných ročníků se nevydávají za články",
      not [i for i in items if "2013" in i["title"]],
      str([i["title"] for i in items if "2013" in i["title"]]))
check("žádný článek nemá jako název označení čísla",
      not [i for i in items if i["title"].startswith("[Jurisprudence] Číslo ")])

autori = [i["authors"] for i in items]
check("u každého článku je autor", all(autori), str(autori))
check("autoři sedí",
      autori[0] == "Aleš Gerloch" and autori[-1] == "Pavla Boučková",
      f"{autori[0]!r} … {autori[-1]!r}")

monitoring = next(i for i in items if i["title"].endswith("lidská práva"))
check("autor je i v popisu", "Autor: Pavla Boučková" in monitoring["description"])
check("rubrika je v popisu", "Rubrika: Monitoring judikatury" in monitoring["description"])
check("číslo je v popisu", "Jurisprudence 1/2026" in monitoring["description"])
check("guid drží stabilní id článku", monitoring["guid"] == "Jurisprudence-1011",
      monitoring["guid"])
check("odkaz míří na stránku článku",
      monitoring["link"].endswith("monitoring-judikatury-evropskeho-soudu-pro-lidska-prava.m-1011.html"),
      monitoring["link"])

# Prázdná stránka nesmí projít jako „číslo bez článků" – volající pak zkusí
# další stránku místo toho, aby vydal prázdný feed.
check("stránka bez obsahu čísla nevrátí nic",
      s._jurisprudence_articles(
          BeautifulSoup("<html><body><p>nic</p></body></html>", "html.parser"),
          "https://www.jurisprudence.cz/cz/casopis/archiv/1-2026") == [])

# =====================================================================
print("\n2) IIC – rozhodnutí soudů vedle článků")
# =====================================================================
# Crossref u rozhodnutí nese soud, datum a spisovou značku až v podtitulu.
check("rozhodnutí se pozná podle podtitulu",
      bool(s.ROZHODNUTI_RE.search(
          "Decision of the Federal Court of Justice of Germany "
          "(Bundesgerichtshof) 27 January 2026 – Case No. KZR 10/25")))
check("komentář k rozhodnutí se za rozhodnutí nepovažuje",
      not s.ROZHODNUTI_RE.search(
          "The FRAND Defence III Decision of the German Federal Court of Justice"))

# =====================================================================
print("\n3) Stránka, která místo obsahu vrátí chybu, nesmí jít do AI")
# =====================================================================


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def _page(html):
    s.requests.get = lambda *a, **k: _Resp(html)
    return s.fetch_page_text("https://example.test/x")


check("hláška o vypnutém JavaScriptu se zahodí",
      _page("<html><body><p>JavaScript is disabled for your browser.</p>"
            + "<p>x</p>" * 400 + "</body></html>") == "")
check("kontrola prohlížeče se zahodí",
      _page("<html><body>Just a moment... Checking your browser"
            + "<p>y</p>" * 400 + "</body></html>") == "")
check("krátká stránka se zahodí", _page("<html><body>Nenalezeno.</body></html>") == "")
check("skutečný text projde",
      len(_page("<html><body><p>" + "Rozhodnutí soudu ve věci FRAND. " * 60
                + "</p></body></html>")) > 600)

# =====================================================================
failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} testů prošlo")
if failed:
    print("Neprošlo:")
    for n in failed:
        print("  -", n)
sys.exit(1 if failed else 0)
