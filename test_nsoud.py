#!/usr/bin/env python3
"""Testy scraperu rozhodnutí Nejvyššího soudu.

Web NS jde z vývojového prostředí stáhnout jen stěží, takže se stránky
simulují – jde o to, co scraper s odpovědí udělá:

  1. Že se shrnutí udělá i z textu na stránce rozhodnutí, ne jen z PDF.
     NS přikládá PDF s odstupem i pár dní; čekat na něj by znamenalo
     nechat čerstvá rozhodnutí bez shrnutí zrovna ve dnech, kdy jsou nová.
  2. Že se za text rozhodnutí nevezme hlavička s metadaty ani navigace.
  3. Že odkaz na PDF jde do feedu, jen když PDF opravdu je.

Spuštění: python test_nsoud.py
"""

import os
import sys
import tempfile
from datetime import datetime, timezone
from xml.etree.ElementTree import tostring

import feed_common as fc
import scraper as s

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("  OK   " if cond else "  CHYBA") + f" {name}"
          + (f" – {detail}" if detail and not cond else ""))


class _Resp:
    def __init__(self, content=b"", text="", ct="text/html"):
        self.content = content
        self.text = text
        self.headers = {"content-type": ct}

    def raise_for_status(self):
        pass


class _Session:
    """Vrací připravené odpovědi podle adresy; co nezná, ohlásí jako chybu
    spojení – ať se pozná, že scraper sáhl někam, kam neměl."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url not in self.routes:
            raise OSError(f"neočekávaná adresa {url}")
        return self.routes[url]


class _Requests:
    def __init__(self, session):
        self.session = session

    def Session(self):
        return self.session


def stranka(telo, metadata="<tr><td class='left-part'>Heslo</td>"
                          "<td class='right-part'>Smlouva</td></tr>"):
    """Detail rozhodnutí: tabulka #tabl s metadaty a vedle ní text."""
    return ("<html><body><nav>Judikatura Vyhledávání</nav>"
            f"<table id='tabl'>{metadata}</table>"
            f"<div>{telo}</div>"
            "<footer>Nejvyšší soud, Burešova 20, Brno</footer>"
            "</body></html>")


ODUVODNENI = "Dovolací soud přezkoumal napadené rozhodnutí. " * 80
DETAIL = "https://rozhodnuti.nsoud.cz/detail/U1?openDocument"
PDF = "https://rozhodnuti.nsoud.cz/soubor/U1.pdf?openElement"


def polozka(**kw):
    zaklad = {
        "case_number": "23 Cdo 4/2026", "unid": "U1", "detail_url": DETAIL,
        "pdf_url": "", "typ": "USNESENÍ", "heslo": "Smlouva", "decided": "2026-09-01",
        "date": "", "category": "E", "summary": "", "tag": "",
        "pub_dt": datetime(2026, 9, 20, tzinfo=timezone.utc),
    }
    zaklad.update(kw)
    return zaklad


def spust(polozky, routes):
    """Pustí enrich_summaries nad čerstvou cache a vrátí (položky, session)."""
    sess = _Session(routes)
    puvodni = (s.requests, s.META_FILE)
    s.requests = _Requests(sess)
    s.META_FILE = os.path.join(tempfile.mkdtemp(), "meta.json")
    try:
        return s.enrich_summaries(polozky), sess
    finally:
        s.requests, s.META_FILE = puvodni


# AI je zapnutá v obou jmenných prostorech – scraper si ji z feed_common
# importuje jménem, summarize_with_cache se ptá té své.
fc.gemini_enabled = s.gemini_enabled = lambda: True
z_pdf, z_textu = [], []
s.gemini_summarize_pdf = lambda data, prompt: (
    z_pdf.append(len(data)), ("Shrnutí z PDF.", "Smlouva"))[1]
s.gemini_summarize_text = lambda text, prompt: (
    z_textu.append(text), ("Shrnutí ze stránky.", "Smlouva"))[1]

# =====================================================================
print("1) Text rozhodnutí ze stránky detailu")
# =====================================================================
sess = _Session({DETAIL: _Resp(text=stranka(ODUVODNENI))})
text = s._text_rozhodnuti(sess, polozka())
check("text rozhodnutí ze stránky projde", len(text) > s.MIN_TEXT_ROZHODNUTI, str(len(text)))
check("metadata z hlavičky do shrnutí nejdou", "Heslo" not in text, text[:120])

# Hlavička s metadaty a patička samy o sobě rozhodnutí nejsou – shrnovat
# by se z nich daly leda údaje, které stejně máme z detailu.
sess = _Session({DETAIL: _Resp(text=stranka("Text rozhodnutí není k dispozici."))})
check("stránka bez odůvodnění se zahodí", s._text_rozhodnuti(sess, polozka()) == "")

# Bez detailu (rozhodnutí z úřední desky) není kam sáhnout.
sess = _Session({})
check("bez adresy detailu se nestahuje nic",
      s._text_rozhodnuti(sess, polozka(detail_url="")) == "" and not sess.calls)

# =====================================================================
print("\n2) Shrnutí ze stránky, když PDF ještě není")
# =====================================================================
z_pdf.clear(), z_textu.clear()
polozky, sess = spust([polozka()], {DETAIL: _Resp(text=stranka(ODUVODNENI))})
check("bez PDF se shrne ze stránky",
      polozky[0]["summary"] == "Shrnutí ze stránky." and len(z_textu) == 1,
      str(polozky[0]["summary"]))
check("bez PDF se o žádné nežádá", not z_pdf and PDF not in sess.calls, str(sess.calls))
check("shrnutá položka poznámku nemá", not polozky[0]["note"], str(polozky[0]["note"]))

# =====================================================================
print("\n3) PDF má přednost, stránka je záloha")
# =====================================================================
z_pdf.clear(), z_textu.clear()
polozky, sess = spust(
    [polozka(pdf_url=PDF)],
    {PDF: _Resp(content=b"%PDF-1.7 ...", ct="application/pdf"),
     DETAIL: _Resp(text=stranka(ODUVODNENI))},
)
check("s PDF se shrne z PDF", polozky[0]["summary"] == "Shrnutí z PDF.", str(z_pdf))
check("stránka se pak nestahuje", DETAIL not in sess.calls, str(sess.calls))

# Na .pdf adrese může přijít chybová stránka; tu do AI posílat nemá smysl,
# zato text na stránce rozhodnutí pořád být může.
z_pdf.clear(), z_textu.clear()
polozky, _ = spust(
    [polozka(pdf_url=PDF)],
    {PDF: _Resp(content=b"<html>Chyba</html>"),
     DETAIL: _Resp(text=stranka(ODUVODNENI))},
)
check("když PDF není PDF, zaskočí stránka",
      polozky[0]["summary"] == "Shrnutí ze stránky." and not z_pdf, str(z_pdf))

# Když nedá nic ani jedno, shrnutí se nevymýšlí a zůstane poznámka.
z_pdf.clear(), z_textu.clear()
polozky, _ = spust([polozka()], {DETAIL: _Resp(text=stranka("Nic."))})
check("bez podkladu zůstane poznámka",
      polozky[0]["note"] == s.BEZ_SHRNUTI_NOTE and not polozky[0]["summary"],
      str(polozky[0]))
check("poznámka netvrdí, že soud nezveřejnil",
      "nezveřejn" not in s.BEZ_SHRNUTI_NOTE, s.BEZ_SHRNUTI_NOTE)

# =====================================================================
print("\n4) Odkaz na PDF jen když PDF je")
# =====================================================================
feed = tostring(s.build_rss([polozka(summary="S.", note="")]), encoding="unicode")
check("bez PDF vede odkaz na stránku rozhodnutí", f"<link>{DETAIL}</link>" in feed, feed[:300])
check("bez PDF se odkaz na PDF nenabízí", "<document-url>" not in feed, feed[:300])

feed = tostring(s.build_rss([polozka(pdf_url=PDF, summary="S.", note="")]), encoding="unicode")
check("s PDF vede odkaz pořád na stránku", f"<link>{DETAIL}</link>" in feed, feed[:300])
check("s PDF se přidá odkaz na soubor", f"<document-url>{PDF}</document-url>" in feed, feed[:300])

# Rozhodnutí z úřední desky stránku detailu nemá – tam odkazuje soubor sám
# a druhý odkaz na totéž by byl navíc.
feed = tostring(s.build_rss([polozka(detail_url="", pdf_url=PDF, summary="S.", note="")]),
                encoding="unicode")
check("bez stránky odkazuje rovnou soubor", f"<link>{PDF}</link>" in feed, feed[:300])
check("odkaz se nezdvojí", "<document-url>" not in feed, feed[:300])

# =====================================================================
failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} testů prošlo")
if failed:
    print("Neprošlo:")
    for n in failed:
        print("  -", n)
sys.exit(1 if failed else 0)
