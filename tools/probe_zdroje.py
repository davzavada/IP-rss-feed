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
from bs4 import BeautifulSoup

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


def formular_pole(html, selektor, tlacitko=None):
    """Pole formuláře tak, jak by je odeslal prohlížeč: [(název, hodnota)].

    Zaškrtávátka a přepínače jen zaškrtnuté, u výběru zvolená (jinak první)
    možnost, zakázaná pole a tlačítka ne – z tlačítek jen `tlacitko`
    (název, hodnota), tedy to, kterým se formulář odesílá. Vrací (form, pole);
    form je None, když formulář na stránce není."""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.select_one(selektor)
    if form is None:
        return None, []
    pole = []
    for el in form.find_all(["input", "select", "textarea"]):
        nazev = el.get("name")
        if not nazev or el.has_attr("disabled"):
            continue
        if el.name == "input":
            typ = (el.get("type") or "text").lower()
            if typ in ("submit", "image", "button", "reset", "file"):
                continue
            if typ in ("checkbox", "radio"):
                if el.has_attr("checked"):
                    pole.append((nazev, el.get("value") or "on"))
                continue
            pole.append((nazev, el.get("value") or ""))
        elif el.name == "select":
            moznosti = el.find_all("option")
            zvolene = [o for o in moznosti if o.has_attr("selected")] or moznosti[:1]
            pole += [(nazev, o.get("value", o.get_text())) for o in zvolene]
        else:
            pole.append((nazev, el.get_text()))
    if tlacitko:
        pole.append(tlacitko)
    return form, pole


def nastav(pole, nazev, hodnota):
    """Přepíše hodnotu pole (první výskyt), nebo pole přidá."""
    for i, (n, _) in enumerate(pole):
        if n == nazev:
            pole[i] = (nazev, hodnota)
            return pole
    pole.append((nazev, hodnota))
    return pole


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

NSS_SKRIPTY = ("/js/comboTreemin.js", "/js/site.js", "/js/infiniteScroll.js")


def nss_predpona(pole, technicky_nazev):
    """Předpona hodnoty podmínky podle technického názvu pole („aktualizovano"
    = Datum zpřístupnění): vyhledavaciSekce[i].vyhledavaciPodminka[j].
    vyhledavaciPodminkaHodnota[k]. – pořadí sekcí se může změnit."""
    for n, v in pole:
        m = re.match(r"^(vyhledavaciSekce\[\d+\]\.vyhledavaciPodminka\[\d+\]\."
                     r"vyhledavaciPodminkaHodnota\[\d+\]\.)TechnickyNazev$", n)
        if m and v == technicky_nazev:
            return m.group(1)
    return None


def sonda_nss(s, den):
    """Formulář vyhledávače, jeho skripty (výběr soudu, nekonečné
    stránkování), hledání podle data zpřístupnění a jeden dokument."""
    r = s.stahni("nss_formular", NSS_HOST + "/")
    s.stahni("nss_detail", NSS_HOST + "/DokumentDetail/Index/785607")
    s.stahni("nss_text", NSS_HOST + "/DokumentOriginal/Html/785607")
    for cesta in NSS_SKRIPTY:
        s.stahni("nss_js_" + cesta.rsplit("/", 1)[-1].split(".")[0], NSS_HOST + cesta)
    if r is None or not r.ok:
        return
    form, pole = formular_pole(r.text, "form#findform", ("btSubmit", ""))
    if form is None:
        print("  formulář findform na stránce není")
        return
    akce = urljoin(r.url, form.get("action") or r.url)
    predpona = nss_predpona(pole, "aktualizovano")
    if not predpona:
        print("  pole aktualizovano (Datum zpřístupnění) ve formuláři není")
        return
    hlav = {"Referer": r.url, "Origin": NSS_HOST}

    def hledej(nazev, od, do, url=akce, soud=None):
        p = list(pole)
        nastav(p, predpona + "HodnotaDatumACasOd", od.strftime("%d.%m.%Y"))
        nastav(p, predpona + "HodnotaDatumACasDo", do.strftime("%d.%m.%Y"))
        if soud is not None:
            ps = nss_predpona(p, "soudsenat")
            if ps:
                nastav(p, ps + "HodnotaCiselnikPolozky", soud)
        return s.stahni(nazev, url, metoda="POST", data=p, headers=hlav)

    vysledky = hledej("nss_hledat_den", den, den)
    hledej("nss_hledat_tyden", den - timedelta(days=6), den)
    hledej("nss_hledat_den_soud", den, den, soud="278")
    hledej("nss_hledat_den_ecli", den, den,
           url=urljoin(NSS_HOST, "/Home/Index?formular=1&zobrazeniVysledkuVolba=5"))
    hledej("nss_hledat_den_export", den, den, url=urljoin(NSS_HOST, "/Home/Export"))
    # Jeden dokument z výsledků (detail i text) – ukáže, jak vypadá čerstvý.
    if vysledky is not None and vysledky.ok:
        m = re.search(r"/DokumentDetail/Index/(\d+)", vysledky.text)
        if m:
            s.stahni("nss_detail_novy", f"{NSS_HOST}/DokumentDetail/Index/{m.group(1)}")
            s.stahni("nss_text_novy", f"{NSS_HOST}/DokumentOriginal/Html/{m.group(1)}")
        # Další stránka výsledků: odkazy nebo adresa pro nekonečné stránkování.
        for i, href in enumerate(sorted(set(re.findall(
                r'(?:href|data-url|data-next)="([^"]*(?:[Ss]trank|[Pp]age|[Dd]alsi|[Nn]ext)[^"]*)"',
                vysledky.text)))[:3]):
            s.stahni(f"nss_dalsi_{i}", urljoin(NSS_HOST, href.replace("&amp;", "&")),
                     headers=hlav)


# --- Ústavní soud ---

US_HLEDANI = US_HOST + "/Search/Search.aspx"
US_POLE = "ctl00$MainContent$"


def sonda_us(s, den):
    """NALUS: formulář (viewstate), hledání podle data zpřístupnění ve dvou
    zápisech data a „jen přírůstky za N dní", výsledky, detail a text."""
    s.stahni("us_text", US_HOST + "/Search/GetText.aspx?sz=1-1029-26_1")

    def hledej(nazev, uprav):
        r = s.stahni(nazev + "_formular", US_HLEDANI)
        if r is None or not r.ok:
            return None
        form, pole = formular_pole(r.text, "form#aspnetForm",
                                   (US_POLE + "but_search", "Vyhledat"))
        if form is None:
            print("  formulář aspnetForm na stránce není")
            return None
        nastav(pole, US_POLE + "razeni", "20")            # data zpřístupnění sestupně
        nastav(pole, US_POLE + "resultsPageSize", "80")
        uprav(pole)
        return s.stahni(nazev, urljoin(r.url, form.get("action") or r.url), metoda="POST",
                        data=pole, headers={"Referer": r.url, "Origin": US_HOST})

    def rozsah(od, do, fmt):
        def uprav(pole):
            nastav(pole, US_POLE + "availableFrom", fmt(od))
            nastav(pole, US_POLE + "availableTo", fmt(do))
        return uprav

    kratke = lambda d: f"{d.day}.{d.month}.{d.year}"     # noqa: E731
    dlouhe = lambda d: d.strftime("%d.%m.%Y")            # noqa: E731
    vysledky = hledej("us_hledat_den", rozsah(den, den, kratke))
    hledej("us_hledat_den_dlouhe", rozsah(den, den, dlouhe))
    hledej("us_hledat_tyden", rozsah(den - timedelta(days=6), den, kratke))

    def prirustky(pole):
        nastav(pole, US_POLE + "dle_data_zpristupneni", "on")
        nastav(pole, US_POLE + "zpristupneno_pred", "7")
    hledej("us_hledat_prirustky", prirustky)

    if vysledky is None or not vysledky.ok:
        return
    html = vysledky.text.replace("&amp;", "&")
    m = re.search(r'(?:href|onclick)="[^"]*?(ResultDetail\.aspx\?[^"\']+)', html)
    if m:
        s.stahni("us_detail", urljoin(US_HOST + "/Search/", m.group(1)))
    m = re.search(r"GetText\.aspx\?sz=([^\"'&]+)", html)
    if m:
        s.stahni("us_text_novy", f"{US_HOST}/Search/GetText.aspx?sz={m.group(1)}")
    # Stránkování: odkazy na další stránky výsledků.
    for i, href in enumerate(sorted(set(re.findall(
            r'href="([^"]*Results\.aspx\?[^"]*)"', html)))[:3]):
        s.stahni(f"us_stranka_{i}", urljoin(US_HOST + "/Search/", href))


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
