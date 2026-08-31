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
FIX_JURISPRUDENCE_HOME = "tests/fixtures/jurisprudence_titulni_3-2026.html"

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
print("\n2) Jurisprudence – obsah čísla z titulní strany")
# =====================================================================
# Titulní strana nese obsah aktuálního čísla, ale sází ho jinak než archiv:
# `ul.articles-list-t1` a název v `<h3><a>`. Číslo v adrese není, je až
# v nadpisu „Aktuální číslo 3/2026".
home = BeautifulSoup(open(FIX_JURISPRUDENCE_HOME, encoding="utf-8").read(),
                     "html.parser")
h_items = s._jurisprudence_articles(home, "https://www.jurisprudence.cz/")

check("z titulní strany se přečte obsah čísla", len(h_items) == 8, str(len(h_items)))
check("číslo se vezme z nadpisu, ne z adresy",
      all("Jurisprudence 3/2026" in i["description"] for i in h_items))
check("autoři jsou i tady",
      all(i["authors"] for i in h_items),
      str([i["authors"] for i in h_items]))
check("rubrika se drží přes celý seznam",
      h_items[-1]["description"].endswith("Rubrika: Monitoring judikatury"),
      h_items[-1]["description"].splitlines()[-1])
check("guid má stejný tvar jako z archivu",
      h_items[-1]["guid"] == "Jurisprudence-1047", h_items[-1]["guid"])

# =====================================================================
print("\n3) Právník – autor ze stránky článku")
# =====================================================================
# Obsah čísla autora nenese, uvádí ho až detail článku. Stránka se stahuje
# kvůli AI shrnutí, tak se z ní bere i autor.
clanek = BeautifulSoup(
    open("tests/fixtures/pravnik_2026-9_clanek.html", encoding="utf-8").read(),
    "html.parser")
check("autor ze stránky článku", s.page_author(clanek) == "Jakub Handrlica",
      repr(s.page_author(clanek)))
check("stránka bez autora nevrátí nic",
      s.page_author(BeautifulSoup("<html><body><h1>x</h1></body></html>",
                                  "html.parser")) == "")

# =====================================================================
print("\n4) IIC – rozhodnutí soudů vedle článků")
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
print("\n5) Crossref – dvojí dotaz a datum vydání")
# =====================================================================
# from-created-date je datum uložení DOI záznamu. Když vydavatel deponuje
# dopředu (ahead of print), vyjde číslo později a z okna vypadne – tak
# zmizelo srpnové číslo QMJIP. Druhý dotaz jde na datum vydání.
_srpnove = {  # DOI z června, vyšlo v srpnu
    "DOI": "10.4337/qmjip.2026.03.01",
    "title": ["Trade marks and the metaverse"],
    "author": [{"given": "Jane", "family": "Doe"}],
    "created": {"date-time": "2026-06-12T00:00:00Z", "date-parts": [[2026, 6, 12]]},
    "published-online": {"date-parts": [[2026, 8, 20]]},
}
_cerstve = {
    "DOI": "10.4337/qmjip.2026.03.02",
    "title": ["Patents and AI"],
    "author": [{"given": "John", "family": "Roe"}],
    "created": {"date-time": "2026-08-25T00:00:00Z", "date-parts": [[2026, 8, 25]]},
    "published-online": {"date-parts": [[2026, 8, 25]]},
}
_odpovedi = {"from-created-date": [_cerstve], "from-pub-date": [_srpnove, _cerstve]}


class _CrossrefResp:
    def __init__(self, items):
        self.items = items

    def raise_for_status(self):
        pass

    def json(self):
        return {"message": {"items": self.items}}


s.requests.get = lambda url, params=None, **k: _CrossrefResp(
    _odpovedi[params["filter"].split(":")[0]])
cr = s.fetch_crossref_journal("2045-9815", "QMJIP", "QMJIP")

check("článek vydaný po deponování DOI se najde", len(cr) == 2, str(len(cr)))
check("stejný DOI se z obou dotazů nezdvojí",
      len({i["guid"] for i in cr}) == 2)
srpnovy = next((i for i in cr if "metaverse" in i["title"]), None)
check("datum je datum vydání, ne vzniku DOI záznamu",
      srpnovy and srpnovy["pub_date"].date().isoformat() == "2026-08-20",
      srpnovy and str(srpnovy["pub_date"]))

# =====================================================================
print("\n6) Stránka, která místo obsahu vrátí chybu, nesmí jít do AI")
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
