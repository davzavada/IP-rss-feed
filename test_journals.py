#!/usr/bin/env python3
"""Testy scraperu časopisů.

Weby časopisů se mění a jejich HTML nejde z vývojového prostředí stáhnout,
takže se testuje nad uloženými kopiemi stránek (tests/fixtures).

Spuštění: python test_journals.py
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

import feed_common as fc
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
print("\n5) RSS vydavatele – OUP (JIPLP) a Wiley")
# =====================================================================
# OUP vydává feed aktuálního čísla. DOI v něm nemusí být ve zvláštním poli –
# podle časopisu je v <dc:identifier>, nebo jen v adrese článku. Guid musí
# vyjít stejně jako z Crossrefu, jinak by se článek při přepnutí zdroje
# označil podruhé jako nový.
_OUP_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">
<channel>
<title>Journal of Intellectual Property Law &amp; Practice Current Issue</title>
<item>
  <title>Trade mark use in the metaverse</title>
  <link>https://academic.oup.com/jiplp/article/21/8/588/8215678?rss=1</link>
  <description>&lt;span class="paragraphSection"&gt;Abstract Soud se zabýval…&lt;/span&gt;</description>
  <dc:creator>Jane Doe</dc:creator>
  <dc:identifier>doi:10.1093/jiplp/jpaf089</dc:identifier>
  <pubDate>Tue, 25 Aug 2026 00:00:00 GMT</pubDate>
</item>
<item>
  <title>Editorial</title>
  <link>https://academic.oup.com/jiplp/article/21/8/585/8215671?rss=1</link>
  <description>Úvodník čísla.</description>
  <pubDate>Tue, 25 Aug 2026 00:00:00 GMT</pubDate>
</item>
<item>
  <title>Designs after the reform</title>
  <link>https://academic.oup.com/jiplp/article/21/8/590/8215680?rss=1</link>
  <description>Abstract Nové nařízení…</description>
  <dc:identifier>doi:10.1093/jiplp/jpaf090</dc:identifier>
  <pubDate>Tue, 25 Aug 2026 00:00:00 GMT</pubDate>
</item>
<item>
  <title>Loňský článek</title>
  <link>https://academic.oup.com/jiplp/article/20/1/1/1?rss=1</link>
  <prism:doi>10.1093/jiplp/jpz001</prism:doi>
  <pubDate>Mon, 06 Jan 2020 00:00:00 GMT</pubDate>
</item>
</channel></rss>"""


class _FeedResp:
    def __init__(self, content="", data=None):
        self.content = content.encode("utf-8")
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


_puvodni_get = s.requests.get
_hlavicky = {}
_dotazy_doi = []


def _feed_get(url, headers=None, params=None, **k):
    # Feed autora nemá – u článku s DOI se dotáhne z Crossrefu.
    if url.startswith(s.CROSSREF_DILO.format(doi="")):
        _dotazy_doi.append((url, params))
        return _FeedResp(data={"message": {"author": [
            {"given": "Ann", "family": "Smith"}, {"given": "Bo", "family": "Král"}]}})
    _hlavicky.update(headers or {})
    return _FeedResp(_OUP_FEED)


s.time.sleep = lambda *a: None
s.requests.get = _feed_get
rss = s.fetch_publisher_rss(s.JIPLP_FEED, s.JIPLP_LABEL, s.JIPLP_NAME,
                            "https://academic.oup.com/jiplp")
s.requests.get = _puvodni_get

check("z feedu přijdou články aktuálního čísla", len(rss) == 3, str(len(rss)))
check("loňský článek se do novinek nepočítá",
      not [i for i in rss if "Loňský" in i["title"]])
check("guid je z DOI, stejný jako z Crossrefu",
      rss[0]["guid"] == "JIPLP-10.1093/jiplp/jpaf089", rss[0]["guid"])
check("odkaz míří na DOI", rss[0]["link"] == "https://doi.org/10.1093/jiplp/jpaf089",
      rss[0]["link"])
check("autor i abstrakt jsou v popisu",
      "Autor: Jane Doe" in rss[0]["description"]
      and "Soud se zabýval" in rss[0]["description"],
      rss[0]["description"])
check("úvodní slovo „Abstract“ se z popisu zahodí",
      "Abstract" not in rss[0]["description"], rss[0]["description"])
check("článek bez DOI drží guid na adrese bez ?rss=1",
      rss[1]["guid"] == "JIPLP-https://academic.oup.com/jiplp/article/21/8/585/8215671",
      rss[1]["guid"])
bez_autora = next(i for i in rss if "Designs" in i["title"])
# Autor se dotahuje až u položek v okně (feed vypisuje i rok staré články
# a ptát se na ně každý běh by bylo zbytečné) a ukládá se do cache.
check("feed sám na Crossref nechodí", not _dotazy_doi and not bez_autora["authors"]
      and bez_autora["doi_autor"] == "10.1093/jiplp/jpaf090", str(_dotazy_doi))
autori_meta = os.path.join(tempfile.mkdtemp(prefix="autori-"), "meta.json")
s.requests.get = _feed_get
s.doplnit_autory(rss, autori_meta)
s.requests.get = _puvodni_get
check("autor, který ve feedu není, se dotáhne z Crossrefu podle DOI",
      bez_autora["authors"] == "Ann Smith, Bo Král", bez_autora["authors"])
check("dotažený autor je i v popisu",
      "Autor: Ann Smith, Bo Král" in bez_autora["description"]
      and bez_autora["description"].startswith("Designs after the reform\n"), bez_autora["description"])
check("na Crossref se chodí jen kvůli chybějícím autorům",
      len(_dotazy_doi) == 1, str(_dotazy_doi))
# Crossref odpoví na `select` u jednoho DOI chybou 400 – posílá se holý dotaz.
check("dotaz na jeden DOI jde bez parametrů",
      _dotazy_doi and _dotazy_doi[0][1] is None, str(_dotazy_doi))
check("dotažený autor je v cache",
      json.load(open(autori_meta, encoding="utf-8"))[bez_autora["guid"]] == {"authors": "Ann Smith, Bo Král"})
s.requests.get = _feed_get
znovu = s.fetch_publisher_rss(s.JIPLP_FEED, s.JIPLP_LABEL, s.JIPLP_NAME)
s.doplnit_autory(znovu, autori_meta)
s.requests.get = _puvodni_get
check("další běh vezme autora z cache, bez dotazu",
      len(_dotazy_doi) == 1 and next(i for i in znovu if "Designs" in i["title"])["authors"]
      == "Ann Smith, Bo Král", str(_dotazy_doi))

# Strop dotazů: nejnovější články jdou první, zbytek přijde na řadu příště.
_dotazy_doi.clear()
mnoho = [{"title": f"[IJLIT] Článek {n}", "guid": f"IJLIT-10.1/{n}", "doi_autor": f"10.1/{n}",
          "authors": "", "pub_date": datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(days=n),
          "popis_casti": (f"Článek {n}", "IJLIT", "")} for n in range(s.CROSSREF_MAX_DOTAZU + 2)]
s.requests.get = _feed_get
s.doplnit_autory(mnoho, autori_meta)
s.requests.get = _puvodni_get
check("strop dotazů na časopis, nejnovější první",
      len(_dotazy_doi) == s.CROSSREF_MAX_DOTAZU and not mnoho[0]["authors"] and not mnoho[1]["authors"]
      and mnoho[-1]["authors"], str(len(_dotazy_doi)))

check("feed se hlásí jako prohlížeč (Wiley i OUP jinak vracely 403)",
      "application/rss+xml" in _hlavicky.get("Accept", "")
      and _hlavicky.get("Referer") == "https://academic.oup.com/jiplp",
      str(_hlavicky))

# RSS 1.0 (RDF, Taylor & Francis) a Atom (Kluwer): položky i pole jsou ve
# vlastním jmenném prostoru, datum je ISO 8601, odkaz v Atomu v atributu.
_nedavno = (s.datetime.now(s.timezone.utc) - s.timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
_TF_FEED = f"""<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
  xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">
<channel><title>Journal of Private International Law</title></channel>
<item rdf:about="https://www.tandfonline.com/doi/full/10.1080/17441048.2026.2555555?af=R">
  <title>Party autonomy in cross-border succession</title>
  <link>https://www.tandfonline.com/doi/full/10.1080/17441048.2026.2555555?af=R</link>
  <description>Abstract The article examines choice of law.</description>
  <dc:creator>Maria Rossi</dc:creator>
  <dc:date>{_nedavno}</dc:date>
  <prism:doi>10.1080/17441048.2026.2555555</prism:doi>
</item></rdf:RDF>"""
_ATOM_FEED = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Common Market Law Review</title>
<entry><title>UPCYCLING AND TRADE MARK USE UNDER EU LAW [pre-publication]</title>
  <link rel="alternate" href="https://kluwerlawonline.com/journalarticle/Common+Market+Law+Review/61.5/COLA2026055"/>
  <author><name>Peter Novak</name></author>
  <published>{_nedavno}</published>
  <summary>Case note on the conditionality regulation.</summary>
</entry></feed>"""
s.requests.get = lambda url, headers=None, **k: _FeedResp(_TF_FEED if "tandfonline" in url else _ATOM_FEED)
tf = s.fetch_publisher_rss("https://www.tandfonline.com/feed/rss/rpil20", "JPIL",
                           "Journal of Private International Law")
atom = s.fetch_publisher_rss("https://kluwerlawonline.com/feeds/COLA", "CMLRev", "Common Market Law Review")
s.requests.get = _puvodni_get
check("RSS 1.0 (T&F): článek s DOI, autorem a datem",
      len(tf) == 1 and tf[0]["guid"] == "JPIL-10.1080/17441048.2026.2555555"
      and tf[0]["authors"] == "Maria Rossi" and not tf[0]["pub_date_odhad"]
      and "choice of law" in tf[0]["description"], str(tf))
check("Kluwer: bez „[pre-publication]“ a bez verzálek",
      atom and atom[0]["title"] == "[CMLRev] Upcycling and Trade Mark Use under EU Law", atom and atom[0]["title"])
check("Atom (Kluwer): odkaz z atributu, autor, datum a anotace",
      len(atom) == 1 and atom[0]["link"].startswith("https://kluwerlawonline.com/journalarticle/")
      and atom[0]["authors"] == "Peter Novak" and not atom[0]["pub_date_odhad"]
      and "conditionality regulation" in atom[0]["description"], str(atom))
_KLUWER = """<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>
<title>KluwerLawOnline.com - Common Market Law Review</title>
<pubDate>Fri, 25 Sep 2026 00:01:06 GMT</pubDate><lastBuildDate>Fri, 25 Sep 2026 00:01:06 GMT</lastBuildDate>
<item><title>UPCYCLING AND TRADE MARK USE UNDER EU LAW [pre-publication]</title>
<link>https://kluwerlawonline.com/JournalArticle/Common+Market+Law+Review/63.5 [pre-publication]/COLA2026073</link>
<description>&lt;p&gt;&lt;i&gt;The article examines upcycling.&lt;/i&gt;&lt;/p&gt;Volume 63 Online ISSN 0165-0750</description>
<pubDate>Fri, 25 Sep 2026 00:01:06 GMT</pubDate></item></channel></rss>"""
s.requests.get = lambda url, headers=None, **k: _FeedResp(_KLUWER)
kl = s.fetch_publisher_rss("https://kluwerlawonline.com/feeds/COLA", "CMLRev", "Common Market Law Review")
s.requests.get = _puvodni_get
check("Kluwer: guid z kódu článku, přežije vydání čísla", kl and kl[0]["guid"] == "CMLRev-COLA2026073", kl and kl[0]["guid"])
check("Kluwer: čas sestavení feedu není datum článku", kl and kl[0]["pub_date_odhad"])
check("Kluwer: bez patičky s ISSN a bez mezer v odkazu",
      kl and "ISSN" not in kl[0]["description"] and "upcycling" in kl[0]["description"]
      and " " not in kl[0]["link"], kl and kl[0]["description"])
check("nové časopisy jsou v registru i ve zdrojích",
      {l for l, *_ in s.DALSI_FEEDY} == {"IJLIT", "JPIL", "CMLRev", "ELJ"}
      and {l for l, *_ in s.DALSI_FEEDY} <= {c["zkratka"] for c in s.CASOPISY})

# =====================================================================
print("\n6) Crossref – dvojí dotaz a datum vydání")
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

# U rozhodnutí se shrnuje ze stránky vydavatele, protože ta nese právní věty
# i odůvodnění. Springer ji z GitHub Actions nepustí, takže anotace, když ji
# Crossref přece jen nese, zůstane jako záloha. Samotný název zálohou není –
# soud, datum i značka z něj jsou i v popisu položky.
_rozhodnuti = {
    "DOI": "10.1007/s40319-026-01772-z",
    "title": ["“LUFFMAN”"],
    "subtitle": ["Decision of the High People’s Court in Hanoi of Vietnam "
                 "6 June 2025 – Case No. 376/2025/HC-PT"],
    "created": {"date-time": "2026-09-02T00:00:00Z", "date-parts": [[2026, 9, 2]]},
    "published-online": {"date-parts": [[2026, 9, 2]]},
}
_s_anotaci = dict(_rozhodnuti, DOI="10.1007/s40319-026-01772-y",
                  abstract="<p>Soud zrušil ochrannou známku LUFFMAN.</p>")
_odpovedi = {"from-created-date": [_rozhodnuti, _s_anotaci], "from-pub-date": []}
iic = {i["guid"]: i for i in s.fetch_crossref_journal("0018-9855", "IIC", "IIC")}
check("rozhodnutí bez anotace nemá pro AI žádnou zálohu",
      iic["IIC-10.1007/s40319-026-01772-z"]["ai_text"] == "",
      iic["IIC-10.1007/s40319-026-01772-z"]["ai_text"])
check("anotace rozhodnutí se pro AI schová jako záloha",
      "Soud zrušil ochrannou známku LUFFMAN."
      in iic["IIC-10.1007/s40319-026-01772-y"]["ai_text"],
      iic["IIC-10.1007/s40319-026-01772-y"]["ai_text"])
check("záloha nese i název se soudem a značkou",
      "LUFFMAN" in iic["IIC-10.1007/s40319-026-01772-y"]["ai_text"]
      and "Hanoi" in iic["IIC-10.1007/s40319-026-01772-y"]["ai_text"],
      iic["IIC-10.1007/s40319-026-01772-y"]["ai_text"])

# =====================================================================
print("\n7) Stránka, která místo obsahu vrátí chybu, nesmí jít do AI")
# =====================================================================


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def _page(html):
    s.requests.get = lambda *a, **k: _Resp(html)
    return s.fetch_page("https://example.test/x")[1]


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
print("\n8) Když není z čeho shrnovat")
# =====================================================================
# Springer z GitHub Actions vrací kontrolu prohlížeče, takže u rozhodnutí
# otištěných v IIC se plný text nestáhne. Anotace z Crossrefu, když tam
# nějaká je, pak zaskočí – lepší než prázdné políčko ve výpisu.
# Shrnování obstarává AI, tady nasimulovaná – zapnutá musí být v obou
# jmenných prostorech, scraper si ji z feed_common importuje jménem.
fc.gemini_enabled = s.gemini_enabled = lambda: True
s.requests.get = lambda *a, **k: _Resp(
    "<html><body>Just a moment... Checking your browser</body></html>")
poslano = []


def _ai(text, prompt):
    poslano.append(text)
    return "Soud v Hanoji známku zrušil.", "Ochranná známka"


s.gemini_summarize_text = _ai
meta_dir = tempfile.mkdtemp()

s.META_FILE = os.path.join(meta_dir, "rozhodnuti.json")
rozhodnuti = {
    "guid": "IIC-10.1007/x",
    "title": "[IIC] “LUFFMAN” – Decision of the High People’s Court in Hanoi",
    "link": "https://doi.org/10.1007/x",
    "description": "LUFFMAN",
    "pub_date": datetime(2026, 9, 2, tzinfo=timezone.utc),
    "ai_source": "page",
    "ai_prompt": s.JOURNAL_DECISION_PROMPT,
    "ai_fallback_tag": "Rozhodnutí",
    "ai_text": "Soud v Hanoji zrušil ochrannou známku LUFFMAN.",
}
s.enrich_summaries([rozhodnuti])
check("za nedostupnou stránku zaskočí anotace z výpisu",
      poslano == ["Soud v Hanoji zrušil ochrannou známku LUFFMAN."], str(poslano))
check("shrnutí z anotace se do položky dostane",
      rozhodnuti["summary"] == "Soud v Hanoji známku zrušil."
      and not rozhodnuti["note"], str(rozhodnuti))

# Když nedá nic ani stránka, ani výpis (zprávy ze seminářů anotaci nemají),
# shrnutí se nevymýšlí. Do feedu jde místo něj poznámka, ať je poznat, že
# tam shrnutí nechybí omylem.
poslano.clear()
s.META_FILE = os.path.join(meta_dir, "zprava.json")
zprava = {
    "guid": "Pravnik-2026-2026-9-4064",
    "title": "[Právník] Zpráva ze semináře",
    "link": "https://www.ilaw.test/2026-9.html?a=4064",
    "description": "Zpráva ze semináře",
    "pub_date": datetime(2026, 9, 1, tzinfo=timezone.utc),
    "ai_source": "page",
}
s.enrich_summaries([zprava])
check("bez podkladu se AI vůbec nevolá", poslano == [], str(poslano))
check("bez podkladu dostane položka poznámku",
      zprava["note"] == s.BEZ_PODKLADU_NOTE and not zprava["summary"], str(zprava))

# =====================================================================
print("\nVýstup pro web (docs/data/casopisy.json)")
# =====================================================================
zprava["title"] = "[Právník] Zpráva ze semináře"
zprava["guid"] = "pravnik-4064"
zprava["first_seen"] = datetime(2026, 9, 2, 6, 30, tzinfo=timezone.utc)
rozhodnuti.setdefault("pub_date", datetime(2026, 9, 3, tzinfo=timezone.utc))
j_zprava, j_rozh = s.polozka_json(zprava), s.polozka_json(rozhodnuti)
check("časopis podle zkratky v titulku, titulek bez ní",
      j_zprava["casopis"] == "pravnik" and j_zprava["nazev"] == "Zpráva ze semináře"
      and j_zprava["first_seen"] == "2026-09-02T06:30:00Z" and j_zprava["datum"] == "2026-09-01", str(j_zprava))
check("bez shrnutí poznámka a popis pro přehled",
      j_zprava["poznamka"] == s.BEZ_PODKLADU_NOTE and j_zprava["popis"] == "Zpráva ze semináře"
      and "shrnuti" not in j_zprava, str(j_zprava))
check("položka se shrnutím poznámku ani popis nemá",
      j_rozh.get("shrnuti") and "poznamka" not in j_rozh and "popis" not in j_rozh, str(j_rozh))
check("každá zkratka ze scraperů je v registru",
      {c["zkratka"] for c in s.CASOPISY} >= {"DV", "EP", "Právník", "Jurisprudence", "TLQ", "IIC", "GRUR Int",
                                            "QMJIP", "JWIP", "JIPLP",
                                            "IJLIT", "JPIL", "CMLRev", "ELJ"}
      | {lab for _, lab, _ in s.OJS_SOURCES} | {lab for _, lab, _ in s.CROSSREF_JOURNALS}
      and len({c["id"] for c in s.CASOPISY}) == len(s.CASOPISY))
cesta = os.path.join(tempfile.mkdtemp(prefix="casopisy-"), "casopisy.json")
t1 = datetime(2026, 9, 24, 5, 0, tzinfo=timezone.utc)
check("první zápis okna", s.zapis_json([zprava, rozhodnuti], cesta, t1))
with open(cesta, encoding="utf-8") as f:
    okno = json.load(f)
check("okno: čas, dny, registr, položky",
      okno["generated"] == "2026-09-24T05:00:00Z" and okno["okno_dni"] == 31
      and okno["casopisy"] == s.CASOPISY and [p["id"] for p in okno["polozky"]] == ["pravnik-4064", rozhodnuti["guid"]])
check("beze změny obsahu se nepřepisuje (ani čas)",
      not s.zapis_json([zprava, rozhodnuti], cesta, t1 + timedelta(hours=6)))

# =====================================================================
print("\nArchiv (data/casopisy/RRRR-MM.jsonl)")
# =====================================================================
archiv_dir = tempfile.mkdtemp(prefix="archiv-casopisu-")
rozhodnuti["first_seen"] = datetime(2026, 8, 30, 6, 0, tzinfo=timezone.utc)


def nacti_archiv():
    out = {}
    for jmeno in sorted(os.listdir(archiv_dir)):
        with open(os.path.join(archiv_dir, jmeno), encoding="utf-8") as f:
            out[jmeno] = [json.loads(r) for r in f if r.strip()]
    return out


check("první archivace: obě položky nové", s.archivuj([zprava, rozhodnuti], archiv_dir) == 2)
arch = nacti_archiv()
check("měsíc podle prvního výskytu, záznam jako v okně",
      sorted(arch) == ["2026-08.jsonl", "2026-09.jsonl"]
      and arch["2026-09.jsonl"] == [s.polozka_json(zprava)]
      and arch["2026-08.jsonl"] == [s.polozka_json(rozhodnuti)], str(arch))
mtime = os.path.getmtime(os.path.join(archiv_dir, "2026-08.jsonl"))
check("znovu nic nového", s.archivuj([zprava, rozhodnuti], archiv_dir) == 0
      and os.path.getmtime(os.path.join(archiv_dir, "2026-08.jsonl")) == mtime)
zprava["summary"], zprava["tag"] = "Doplněné shrnutí.", "Seminář"
s.archivuj([zprava], archiv_dir)
check("doplněné shrnutí se do archivu propíše",
      nacti_archiv()["2026-09.jsonl"][0].get("shrnuti") == "Doplněné shrnutí.")

# =====================================================================
print("\nStav prvního výskytu a cache")
# =====================================================================
stav = os.path.join(tempfile.mkdtemp(prefix="stav-"), "seen.json")
davno = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
nedavno = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
with open(stav, "w", encoding="utf-8") as f:
    json.dump({"stary": davno, "novy": nedavno}, f)
drzene = fc.filter_by_first_seen([{"guid": "stary"}, {"guid": "novy"}], lambda i: i["guid"], stav,
                                 prune_days=None)
with open(stav, encoding="utf-8") as f:
    ulozeny = json.load(f)
check("bez prořezávání stav drží i rok staré položky, okno jen měsíc",
      sorted(ulozeny) == ["novy", "stary"] and [i["guid"] for i in drzene] == ["novy"], str(ulozeny))
check("cache shrnutí se prořízne podle stáří, ne podle přítomnosti ve stavu",
      fc.prune_meta({"stary": {}, "novy": {}}, stav) == {"novy": {}})
stav2 = os.path.join(tempfile.mkdtemp(prefix="stav-"), "seen.json")
posunuto = fc.filter_by_first_seen(
    [{"guid": "a"}, {"guid": "b"}], lambda i: i["guid"], stav2, prune_days=None,
    first_seen_of=lambda it, ted: ted - timedelta(days=15) if it["guid"] == "a" else None)
check("first_seen_of: neznámé položce jde zapsat dřívější první výskyt",
      [i["is_new"] for i in posunuto] == [False, True]
      and (posunuto[1]["first_seen"] - posunuto[0]["first_seen"]).days == 15, str(posunuto))

# =====================================================================
print("\nBez abstraktu se AI nevolá (ze samotného názvu by si obsah domyslela)")
# =====================================================================
_bez_abstraktu = {
    "DOI": "10.1093/grurint/ikag092",
    "title": ["Selective Distribution of Luxury Goods"],
    "created": {"date-parts": [[2026, 9, 10]]},
    "published-online": {"date-parts": [[2026, 9, 10]]},
}
_odpovedi = {"from-created-date": [_bez_abstraktu], "from-pub-date": []}
s.requests.get = lambda url, params=None, **k: _CrossrefResp(_odpovedi[params["filter"].split(":")[0]])
clanek = s.fetch_crossref_journal("2632-8550", "GRUR Int", "GRUR International")[0]
check("Crossref bez abstraktu: ai_text prázdný", clanek["ai_source"] == "article"
      and clanek["ai_text"] == "", repr(clanek["ai_text"]))
poslano.clear()
s.META_FILE = os.path.join(meta_dir, "bez_abstraktu.json")
s.enrich_summaries([clanek])
check("AI se nezavolá, položka dostane poznámku a cache zůstane prázdná",
      poslano == [] and clanek["note"] == s.BEZ_PODKLADU_NOTE and not clanek["summary"]
      and not (json.load(open(s.META_FILE, encoding="utf-8")).get(clanek["guid"]) or {}).get("summary"),
      str(poslano))
_odpovedi = {"from-created-date": [dict(_bez_abstraktu, abstract="<jats:p>Analýza selektivní distribuce.</jats:p>")],
             "from-pub-date": []}
clanek = s.fetch_crossref_journal("2632-8550", "GRUR Int", "GRUR International")[0]
s.enrich_summaries([clanek])
check("až abstrakt přibude, shrnutí vznikne",
      len(poslano) == 1 and "Analýza selektivní distribuce." in poslano[0] and clanek["summary"], str(poslano))

_OUP_BEZ = """<?xml version="1.0"?><rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
<item><title>Correction to: Round-up</title><link>https://academic.oup.com/jiplp/article/1?rss=1</link>
<dc:identifier>doi:10.1093/jiplp/jpag080</dc:identifier><pubDate>Tue, 22 Sep 2026 00:00:00 GMT</pubDate></item>
</channel></rss>"""
s.requests.get = lambda url, headers=None, **k: _FeedResp(_OUP_BEZ)
oprava = s.fetch_publisher_rss(s.JIPLP_FEED, s.JIPLP_LABEL, s.JIPLP_NAME)[0]
_OJS_BEZ = """<?xml version="1.0"?><rss version="2.0"><channel><item><title>Recenze</title>
<link>https://journals.muni.cz/revue/article/view/1</link><description></description>
<pubDate>Tue, 22 Sep 2026 00:00:00 GMT</pubDate></item></channel></rss>"""
s.requests.get = lambda url, headers=None, **k: _FeedResp(_OJS_BEZ)
recenze = s.fetch_ojs_rss("https://journals.muni.cz/revue/x", "RPT", "Revue")[0]
s.requests.get = _puvodni_get
check("feed vydavatele i OJS bez anotace: ai_text prázdný",
      oprava["ai_text"] == "" and recenze["ai_text"] == "", f"{oprava['ai_text']!r} {recenze['ai_text']!r}")
check("poznámka mluví jen za nás, ne za zdroj",
      "zdroj" not in s.BEZ_PODKLADU_NOTE.lower() and "Shrnutí zatím není" in s.BEZ_PODKLADU_NOTE)

# Výpadek Crossrefu není „žádné nové články" – main() ho musí vidět.
def _crossref_dole(*a, **k):
    raise s.requests.ConnectionError("proxy")


s.requests.get = _crossref_dole
try:
    s.fetch_crossref_journal("2632-8550", "GRUR Int", "GRUR International")
    vypadek = False
except RuntimeError:
    vypadek = True
s.requests.get = _puvodni_get
check("Crossref: když neprojde žádný dotaz, je to výpadek (výjimka)", vypadek)

# =====================================================================
print("\nÚPV: datum čísla z prvního výskytu, ne vymyšlený začátek čtvrtletí")
# =====================================================================
s.requests.get = lambda *a, **k: _Resp(
    "<html><body><h3>2026</h3>"
    "<a href='/files/dusevni_vlastnictvi_2026-03.pdf'>DV 3/2026</a>"
    "<a href='/files/dusevni_vlastnictvi_2026-02.pdf'>DV 2/2026</a>"
    "<a href='/files/evropske_pravo_2026-02.pdf'>EP 2/2026</a></body></html>")
upv = s.scrape_upv()
s.requests.get = _puvodni_get
check("ÚPV: poslední číslo každého časopisu, datum jako odhad",
      sorted(i["guid"] for i in upv) == ["Duševní vlastnictví-03-2026", "Evropské právo-02-2026"]
      and all(i["pub_date_odhad"] for i in upv), str([(i["guid"], i.get("pub_date_odhad")) for i in upv]))

# =====================================================================
print("\nOkno z archivu, náběh nového časopisu a výpadek zdrojů (main)")
# =====================================================================
fc.gemini_enabled = s.gemini_enabled = lambda: False
ted = datetime.now(timezone.utc)
beh_dir = tempfile.mkdtemp(prefix="beh-")
s.STATE_FILE = os.path.join(beh_dir, "seen.json")
s.META_FILE = os.path.join(beh_dir, "meta.json")
s.ARCHIV_DIR = os.path.join(beh_dir, "archiv")
s.OUTPUT = os.path.join(beh_dir, "casopisy.json")
s.ZAVEDENI = {}


def pravnik(cislo, n):
    return {"title": f"[Právník] Článek {cislo}-{n}", "journal_name": "Právník",
            "link": f"https://ilaw.test/{cislo}-{n}", "description": f"Článek {cislo}-{n}",
            "guid": f"Pravnik-2026-2026-{cislo}-{n}", "pub_date": ted, "pub_date_odhad": True,
            "ai_source": "page"}


# Číslo 8 je v okně 10 dní, zdroj už vypisuje jen číslo 9.
pred_10 = ted - timedelta(days=10)
stare = [pravnik(8, n) for n in (1, 2, 3)]
for it in stare:
    it["first_seen"] = it["pub_date"] = pred_10
    it.pop("pub_date_odhad")
stare_mimo = dict(pravnik(7, 1), first_seen=ted - timedelta(days=40), pub_date=ted - timedelta(days=40))
vyrazena = dict(pravnik(8, 9), first_seen=pred_10, pub_date=pred_10)   # ve stavu není
s.archivuj(stare + [stare_mimo, vyrazena], s.ARCHIV_DIR)
with open(s.STATE_FILE, "w", encoding="utf-8") as f:
    json.dump({it["guid"]: it["first_seen"].isoformat() for it in stare + [stare_mimo]}
              | {"IIC-10.1007/stary": (ted - timedelta(days=60)).isoformat()}, f)
# Vymyšlené shrnutí jednoho článku z cache zmizelo (zahozené) – z okna taky.
with open(s.META_FILE, "w", encoding="utf-8") as f:
    json.dump({"Pravnik-2026-2026-8-2": {}, "Pravnik-2026-2026-8-3": {"summary": "Nové shrnutí.", "tag": "T"}}, f)
arch = os.path.join(s.ARCHIV_DIR, pred_10.strftime("%Y-%m") + ".jsonl")
radky = [json.loads(r) for r in open(arch, encoding="utf-8")]
for r in radky:
    if r["id"] == "Pravnik-2026-2026-8-2":
        r.update(shrnuti="Vymyšlené z názvu.", heslo="X")
        r.pop("popis", None)
with open(arch, "w", encoding="utf-8") as f:
    f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in radky))

zdroje = {}


def _vrat(klic):
    def fn(*a, **k):
        v = zdroje.get(klic, [])
        if isinstance(v, Exception):
            raise v
        return [dict(i) for i in v]
    return fn


s.scrape_upv, s.scrape_pravnik = _vrat("upv"), _vrat("pravnik")
s.scrape_jurisprudence, s.scrape_tlq = _vrat("jurisprudence"), _vrat("tlq")
s.fetch_ojs_rss = lambda f, l, n: _vrat(l)()
s.fetch_crossref_journal = lambda i, l, n: _vrat(l)()
s._rss_nebo_crossref = lambda f, r, i, l, n, v: _vrat(l)()
s.scrape_jwip, s.scrape_jiplp = _vrat("JWIP"), _vrat("JIPLP")


def spust():
    try:
        s.main()
        return 0
    except SystemExit as e:
        return e.code


def okno_ids():
    with open(s.OUTPUT, encoding="utf-8") as f:
        return {p["id"]: p for p in json.load(f)["polozky"]}


def novy(label, n, dni, odhad=False):
    return {"title": f"[{label}] Článek {n}", "journal_name": label, "link": f"https://x.test/{label}/{n}",
            "description": f"Článek {n}", "guid": f"{label}-10.1/{n}", "pub_date": ted - timedelta(days=dni),
            "pub_date_odhad": odhad, "ai_source": "article", "ai_text": ""}


zdroje.update(
    pravnik=[pravnik(9, 4), pravnik(9, 5)], upv=[], jurisprudence=[], tlq=[],
    # JPIL stav ještě nezná: náběh. IIC ho zná: běžný nový článek.
    JPIL=[novy("JPIL", 1, 90), novy("JPIL", 2, 0, odhad=True), novy("JPIL", 3, 1)],
    IIC=[novy("IIC", 1, 40)],
)
kod = spust()
okno = okno_ids()
check("okno drží i předchozí číslo, které zdroj už nevypisuje",
      {"Pravnik-2026-2026-8-1", "Pravnik-2026-2026-8-2", "Pravnik-2026-2026-8-3",
       "Pravnik-2026-2026-9-4", "Pravnik-2026-2026-9-5"} <= set(okno), str(sorted(okno)))
check("z archivu jen to, co je ve stavu a v okně",
      "Pravnik-2026-2026-7-1" not in okno and "Pravnik-2026-2026-8-9" not in okno, str(sorted(okno)))
check("doplněný záznam má shrnutí z cache, zahozené shrnutí zmizí",
      okno["Pravnik-2026-2026-8-3"].get("shrnuti") == "Nové shrnutí."
      and "shrnuti" not in okno["Pravnik-2026-2026-8-2"] and okno["Pravnik-2026-2026-8-2"].get("popis"),
      str(okno["Pravnik-2026-2026-8-2"]))
check("okno je seřazené podle data",
      [p["datum"] for p in okno.values()] == sorted((p["datum"] for p in okno.values()), reverse=True))
with open(s.STATE_FILE, encoding="utf-8") as f:
    stav_po = {k: datetime.fromisoformat(v) for k, v in json.load(f).items()}
check("náběh: starý článek i článek s odhadnutým datem jdou o 15 dní zpět",
      all(abs((ted - stav_po[g]).days - s.NABEH_POSUN_DNI) <= 1 for g in ("JPIL-10.1/1", "JPIL-10.1/2"))
      and not okno["JPIL-10.1/1"].get("first_seen", "").startswith(ted.date().isoformat()),
      str({g: stav_po[g] for g in stav_po if g.startswith("JPIL")}))
check("náběh: čerstvě vydaný článek je novinka",
      (ted - stav_po["JPIL-10.1/3"]).total_seconds() < 3600)
check("časopis, který stav zná, jede postaru (první výskyt = teď)",
      (ted - stav_po["IIC-10.1/1"]).total_seconds() < 3600)
check("běh bez výpadku skončí 0 (prázdné weby jsou tu výpadek, ale menšina)", kod == 0, str(kod))

# Výpadek všech zdrojů: okno se drží z archivu a běh skončí chybou.
vypadek = s.requests.ConnectionError("síť dole")
for k in ["upv", "pravnik", "jurisprudence", "tlq", "JWIP", "JIPLP", "IIC", "JPIL"] \
        + [l for _, l, _ in s.OJS_SOURCES] + [l for _, l, _ in s.CROSSREF_JOURNALS] + [l for l, *_ in s.DALSI_FEEDY]:
    zdroje[k] = vypadek
kod = spust()
okno2 = okno_ids()
check("výpadek všech zdrojů: okno se nevyprázdní, drží ho archiv",
      {"Pravnik-2026-2026-8-1", "Pravnik-2026-2026-9-4", "JPIL-10.1/3"} <= set(okno2), str(sorted(okno2)))
check("výpadek většiny zdrojů: běh skončí chybou", kod == 1, str(kod))
selhane = []
check("web čísla bez položek je výpadek, feed bez položek ne",
      s._zdroj("Právník", lambda: [], selhane, prazdny_je_chyba=True) == []
      and s._zdroj("QMJIP", lambda: [], selhane) == [] and selhane == ["Právník"], str(selhane))

nyni = datetime(2026, 9, 26, 5, 0, tzinfo=timezone.utc)
s.ZAVEDENI = {"IJLIT": "2026-09-25"}
fs = s.nabeh_first_seen({"IJLIT-10.1/x": "2026-09-26T04:00:00+00:00", "IIC-1": "x"}, nyni)
check("náběh podle data zavedení platí i pro druhý běh (feed napoprvé nevyšel)",
      fs({"title": "[IJLIT] A", "pub_date": nyni - timedelta(days=30)}, nyni) == nyni - timedelta(days=15)
      and fs({"title": "[IIC] A", "pub_date": nyni - timedelta(days=30)}, nyni) is None)
fs = s.nabeh_first_seen({"IJLIT-10.1/x": "2026-09-26T04:00:00+00:00", "IIC-1": "x"},
                        nyni + timedelta(days=s.NABEH_DNI))
check("po náběhu se jede postaru",
      fs({"title": "[IJLIT] A", "pub_date": nyni - timedelta(days=30)}, nyni) is None)
with open("journals_seen.json", encoding="utf-8") as f:
    realny_stav = json.load(f)
check("každý guid ve stavu patří časopisu z registru (prefix pro náběh sedí)",
      all(any(g.startswith(s.guid_prefix(z)) for z in s.CASOPIS_PODLE_ZKRATKY) for g in realny_stav))

# =====================================================================
failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} testů prošlo")
if failed:
    print("Neprošlo:")
    for n in failed:
        print("  -", n)
sys.exit(1 if failed else 0)
