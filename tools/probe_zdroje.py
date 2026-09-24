#!/usr/bin/env python3
"""Sonda zdrojů judikatury – stáhne syrové odpovědi webů soudů a uloží je.

K čemu: vývoj scraperů běží v prostředí, odkud na weby soudů není vidět,
a dvě z hledání (NSS, InfoCuria) navíc chtějí POST s nezdokumentovaným
tvarem. Sonda proto v GitHub Actions stáhne, co odpovídají formuláře, výpisy
a detaily, a uloží to jako artefakt (případně rovnou jako fixtures do větve).
Ze staženého se pak píšou parsery i testy.

Jen čte: posílá GET a pár vyhledávacích POSTů, nic nemění a nepotřebuje
žádné klíče.

Použití (lokálně i v probe.yml):
    python tools/probe_zdroje.py                   # všechny soudy, den = včera
    python tools/probe_zdroje.py --soudy ns,sdeu --datum 2026-09-22
    python tools/probe_zdroje.py --url https://vyhledavac.nssoud.cz/...

Parametry jdou zadat i proměnnými PROBE_SOUDY, PROBE_DATUM a PROBE_URL –
workflow je tak nemusí vkládat do příkazové řádky.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from urllib.parse import quote, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from feed_common import USER_AGENT  # noqa: E402

PRAHA = ZoneInfo("Europe/Prague")
SOUDY = ("ns", "nss", "us", "sdeu")
MAX_TELO = 10 * 1024 * 1024   # větší odpověď se ořízne (hlídá velikost artefaktu)
TIMEOUT = 60

# Vlastní adresy (--url) jen z hostů zdrojů – sonda nemá sloužit k ničemu jinému.
POVOLENE_HOSTY = (
    "nsoud.cz", "nssoud.cz", "usoud.cz", "curia.europa.eu",
    "publications.europa.eu", "eur-lex.europa.eu", "justice.cz",
)

HLAVICKY = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
}

NS_HOST = "https://rozhodnuti.nsoud.cz"
NS_HLEDANI = NS_HOST + "/Judikatura/judikatura_ns.nsf/$$WebSearch1"
# Rejstříky NS, které se objevují ve výpisu zveřejněných rozhodnutí. Dotaz
# jen podle data server shazuje na 500 („Field is too large (32K)"), takže
# se ptáme po rejstřících a sonda ukáže, které projdou.
NS_REJSTRIKY = ("cdo", "icdo", "nscr", "nd", "ncu", "tdo", "tz", "td", "tcu", "ntd")

NSS_HOST = "https://vyhledavac.nssoud.cz"
US_HOST = "https://nalus.usoud.cz"
CURIA_APP = "https://infocuria.curia.europa.eu"
CURIA_HLEDANI = "https://infocuriaws.curia.europa.eu/elastic-connector/search"
SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"


def domino_datum(d):
    """Datum ve tvaru, který bere dotaz Domina (24.9.2026)."""
    return f"{d.day}.{d.month}.{d.year}"


class Sonda:
    """Stahuje odpovědi do adresáře a vede si souhrn."""

    def __init__(self, vystup):
        self.vystup = vystup
        self.souhrn = []
        self.session = requests.Session()
        os.makedirs(vystup, exist_ok=True)

    def stahni(self, nazev, url, metoda="GET", **kw):
        """Stáhne jednu odpověď, uloží tělo a zapíše řádek do souhrnu.

        Vrací Response, nebo None, když požadavek vůbec neprošel."""
        hlavicky = dict(HLAVICKY, **kw.pop("headers", {}))
        zaznam = {"nazev": nazev, "metoda": metoda, "url": url}
        start = time.monotonic()
        try:
            r = self.session.request(metoda, url, headers=hlavicky, timeout=TIMEOUT, **kw)
        except Exception as e:   # sonda má doběhnout i přes výpadek zdroje
            zaznam.update(chyba=f"{type(e).__name__}: {e}"[:300],
                          trvani_s=round(time.monotonic() - start, 1))
            self.souhrn.append(zaznam)
            print(f"  {nazev:28} CHYBA {zaznam['chyba'][:120]}")
            return None
        telo = r.content[:MAX_TELO]
        typ = r.headers.get("Content-Type", "")
        pripona = (".json" if "json" in typ else ".pdf" if "pdf" in typ
                   else ".js" if "javascript" in typ else ".xml" if "xml" in typ
                   and "html" not in typ else ".html")
        soubor = nazev + pripona
        with open(os.path.join(self.vystup, soubor), "wb") as f:
            f.write(telo)
        zaznam.update(
            status=r.status_code, konecna_url=r.url, typ=typ, soubor=soubor,
            velikost=len(r.content), orizlo=len(r.content) > MAX_TELO,
            trvani_s=round(time.monotonic() - start, 1),
        )
        self.souhrn.append(zaznam)
        print(f"  {nazev:28} {r.status_code}  {len(r.content):>9} B  "
              f"{zaznam['trvani_s']:>5}s  {typ[:40]}")
        return r

    def uloz_souhrn(self):
        with open(os.path.join(self.vystup, "souhrn.json"), "w", encoding="utf-8") as f:
            json.dump(self.souhrn, f, ensure_ascii=False, indent=2)


# --- Nejvyšší soud ---

def sonda_ns(s, den):
    """Výpisy podle rejstříků za jeden den, jeden detail a úřední desky."""
    s.stahni("ns_uvod", NS_HOST + "/")   # Domino chce session cookie
    d = domino_datum(den)
    dotazy = {"ns_den": f"[datum_predani_na_web]={d}"}
    for rej in NS_REJSTRIKY:
        dotazy[f"ns_{rej}"] = f"[spzn2]={rej} AND [datum_predani_na_web]={d}"
    # Rozsah ≥ místo = – tak dotazuje dnešní scraper; porovnání ukáže rozdíl.
    dotazy["ns_cdo_rozsah"] = f"[spzn2]=cdo AND [datum_predani_na_web]>={d}"
    prvni_detail = ""
    for nazev, dotaz in dotazy.items():
        url = (f"{NS_HLEDANI}?SearchView&Query={quote(dotaz)}"
               f"&SearchMax=1000&SearchOrder=4&Start=0&Count=200&pohled=1")
        r = s.stahni(nazev, url, headers={"Referer": NS_HOST + "/"})
        if r is not None and r.ok and not prvni_detail:
            m = re.search(r'<a class="odk" href="([^"]+openDocument)"', r.text)
            if m:
                prvni_detail = urljoin(NS_HOST, m.group(1))
    if prvni_detail:
        s.stahni("ns_detail", prvni_detail, headers={"Referer": NS_HOST + "/"})
    s.stahni("ns_deska_civilni", "https://www.nsoud.cz/uredni-deska/"
             "obcanskopravni-a-obchodni-kolegium/vyhlasovana-rozhodnuti")
    s.stahni("ns_deska_trestni", "https://www.nsoud.cz/uredni-deska/"
             "trestni-kolegium/vyhlasovana-rozhodnuti")


# --- Nejvyšší správní soud ---

def sonda_nss(s, den):
    """Formulář vyhledávače (názvy polí, antiforgery token) a jeden dokument."""
    r = s.stahni("nss_formular", NSS_HOST + "/")
    s.stahni("nss_detail", NSS_HOST + "/DokumentDetail/Index/785607")
    s.stahni("nss_text", NSS_HOST + "/DokumentOriginal/Html/785607")
    # Skripty stránky prozradí, kam a v jakém tvaru formulář posílá.
    if r is not None and r.ok:
        for i, src in enumerate(re.findall(r'<script[^>]+src="([^"]+)"', r.text)[:6]):
            if src.startswith("http") and NSS_HOST not in src:
                continue
            s.stahni(f"nss_skript_{i}", urljoin(NSS_HOST + "/", src))


# --- Ústavní soud ---

def sonda_us(s, den):
    """Vyhledávací formulář NALUS (viewstate, pole data zpřístupnění) a text."""
    s.stahni("us_formular", US_HOST + "/Search/Search.aspx")
    s.stahni("us_text", US_HOST + "/Search/GetText.aspx?sz=1-1029-26_1")


# --- Soudní dvůr EU ---

def _curia_dotaz(hledat, razeni, zalozka):
    """Tělo dotazu InfoCurie ve tvaru, který dnes posílá scraper_ipcuria."""
    return {
        "searchTerm": hledat, "multiSearchTerms": [],
        "sortTermList": [{"sortDirection": "DESC", "sortTerm": razeni}],
        "pagination": {"pageNumber": 0, "pageSize": 20, "from": 1, "to": 20},
        "language": "EN", "tabName": zalozka, "isAllTabsRequest": True, "ecli": "",
        "publishedId": "", "usualName": "", "logicDocId": "", "repJurExpand": True,
        "advancedFiltersValue": [], "isSearchExact": False,
        "searchSources": ["document", "metadata"],
    }


def sonda_sdeu(s, den):
    """Varianty hledání v InfoCurii bez hledaného slova, SPARQL Cellaru
    a hlavní skript aplikace InfoCurie (v něm jsou názvy filtrů a řazení)."""
    hlav = {"Content-Type": "application/json", "Accept": "application/json, text/plain, */*",
            "Origin": CURIA_APP, "Referer": CURIA_APP + "/"}
    varianty = {
        "sdeu_hledat_prazdne_datum": _curia_dotaz("", "DATE", "document"),
        "sdeu_hledat_hvezda_datum": _curia_dotaz("*", "DATE", "document"),
        "sdeu_hledat_prazdne_affair": _curia_dotaz("", "DATE", "affair"),
        "sdeu_hledat_score": _curia_dotaz("*", "SCORE", "document"),
    }
    for nazev, telo in varianty.items():
        s.stahni(nazev, CURIA_HLEDANI, metoda="POST", json=telo, headers=hlav)

    r = s.stahni("sdeu_aplikace", CURIA_APP + "/")
    if r is not None and r.ok:
        for i, src in enumerate(re.findall(r'src="(main[^"]*\.js)"', r.text)[:2]):
            s.stahni(f"sdeu_aplikace_js_{i}", urljoin(CURIA_APP + "/", src))

    od = den - timedelta(days=14)
    dotaz = (
        "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
        "SELECT DISTINCT ?celex ?datum WHERE {\n"
        "  ?dilo cdm:resource_legal_id_celex ?celex ;\n"
        "        cdm:work_date_document ?datum .\n"
        f'  FILTER(?datum >= "{od.isoformat()}"^^xsd:date)\n'
        '  FILTER(STRSTARTS(STR(?celex), "6"))\n'
        "} ORDER BY DESC(?datum) LIMIT 200"
    )
    s.stahni("sdeu_sparql", SPARQL, metoda="POST",
             data={"query": dotaz, "format": "application/sparql-results+json"},
             headers={"Accept": "application/sparql-results+json"})
    s.stahni("sdeu_cellar_celex", "http://publications.europa.eu/resource/celex/62025CJ0602",
             headers={"Accept": "application/xhtml+xml", "Accept-Language": "ces"})


SONDY = {"ns": sonda_ns, "nss": sonda_nss, "us": sonda_us, "sdeu": sonda_sdeu}


def vychozi_den():
    """Včerejšek v pražském čase – ten den už má soud zveřejněný celý."""
    return (datetime.now(PRAHA) - timedelta(days=1)).date()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--soudy", default=os.environ.get("PROBE_SOUDY") or ",".join(SOUDY))
    ap.add_argument("--datum", default=os.environ.get("PROBE_DATUM") or "")
    ap.add_argument("--url", nargs="*", default=(os.environ.get("PROBE_URL") or "").split())
    ap.add_argument("--vystup", default="build/probe")
    args = ap.parse_args()

    den = date.fromisoformat(args.datum) if args.datum.strip() else vychozi_den()
    soudy = [x.strip() for x in args.soudy.split(",") if x.strip()]
    nezname = [x for x in soudy if x not in SONDY]
    if nezname:
        sys.exit(f"Neznámé zdroje: {', '.join(nezname)} (znám {', '.join(SONDY)})")

    s = Sonda(args.vystup)
    print(f"Sonda zdrojů, den {den.isoformat()}, výstup {args.vystup}")
    for soud in soudy:
        print(f"[{soud}]")
        SONDY[soud](s, den)
    for i, url in enumerate(u for u in args.url if u):
        host = urlparse(url).hostname or ""
        povoleny = any(host == h or host.endswith("." + h) for h in POVOLENE_HOSTY)
        if url.startswith("https://") and povoleny:
            s.stahni(f"url_{i}", url)
        else:
            print(f"  přeskakuji {url} – jen https a hosty zdrojů ({', '.join(POVOLENE_HOSTY)})")
    s.uloz_souhrn()

    chyby = [z for z in s.souhrn if z.get("chyba") or z.get("status", 200) >= 400]
    print(f"Hotovo: {len(s.souhrn)} odpovědí, z toho {len(chyby)} s chybou "
          f"(u sondy běžné – právě to se zjišťuje).")


if __name__ == "__main__":
    main()
