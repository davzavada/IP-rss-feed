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
from judikatura.soudy.web import formular_pole, nastav  # noqa: E402

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

    # „Do" je půlnoc – den se tak zadává jako od–do+1.
    vysledky = hledej("nss_hledat_den", den, den + timedelta(days=1))
    tyden = hledej("nss_hledat_tyden", den - timedelta(days=6), den + timedelta(days=1))
    # Nekonečné stránkování: další řádky vrací POST na moreRowsUrl s parametry
    # hledání, které stránka vypíše do skriptu.
    if tyden is not None and tyden.ok:
        m_url = re.search(r"var moreRowsUrl = '([^']+)'", tyden.text)
        m_par = re.search(r"var currParams = '(.*?)';\s*$", tyden.text, re.M)
        m_view = re.search(r"var currViewId = '([^']*)'", tyden.text)
        m_sort = re.search(r"var currSort = '([^']*)'", tyden.text)
        if m_url and m_par:
            parametry = json.loads('"' + m_par.group(1) + '"')
            for strana in (1, 2, 50):
                s.stahni(f"nss_dalsi_{strana}", urljoin(NSS_HOST, m_url.group(1)), metoda="POST",
                         data={"vyhledavaciPodminky": parametry,
                               "zobrazeniVysledkuId": m_view.group(1) if m_view else "1",
                               "pageNum": str(strana),
                               "resultOrder": m_sort.group(1) if m_sort else ""},
                         headers=dict(hlav, **{"X-Requested-With": "XMLHttpRequest"}))
    # Dokumenty z výsledků: prostý text, originál a detaily různých výroků.
    for id_ in ("785707", "785620", "785703"):
        s.stahni(f"nss_detail_{id_}", f"{NSS_HOST}/DokumentDetail/Index/{id_}")
    s.stahni("nss_text_prosty", f"{NSS_HOST}/DokumentOriginal/Text/785707")
    s.stahni("nss_original", f"{NSS_HOST}/DokumentOriginal/Index/785707")
    if vysledky is not None and vysledky.ok:
        print("  " + " ".join(re.findall(r"Počet nalezených záznamů: \d+", vysledky.text)))


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
    hledej("us_hledat_den", rozsah(den, den, kratke))

    # Stránkování: měsíc po deseti výsledcích.
    def mesic(pole):
        rozsah(den - timedelta(days=30), den, kratke)(pole)
        nastav(pole, US_POLE + "resultsPageSize", "10")
    vysledky = hledej("us_hledat_mesic", mesic)
    if vysledky is None or not vysledky.ok:
        return
    html = vysledky.text.replace("&amp;", "&")
    print("  " + " ".join(re.findall(r"Výsledky \d+ - \d+ z celkem \d+", html)[:1]))
    m = re.search(r"(ResultDetail\.aspx\?[^\"']+)", html)
    if m:
        s.stahni("us_detail", urljoin(US_HOST + "/Search/", m.group(1)))
    # Odkazy a postbacky, které vypadají jako přechod na další stránku.
    for i, href in enumerate(sorted(set(re.findall(
            r"href=[\"']([^\"']*Results\.aspx\?[^\"']*)[\"']", html)))[:3]):
        s.stahni(f"us_stranka_odkaz_{i}", urljoin(US_HOST + "/Search/", href))
    postbacky = [(t, a) for t, a in re.findall(r"__doPostBack\('([^']+)','([^']*)'\)", html)
                 if re.search(r"[Pp]age|[Nn]ext|[Dd]alsi|[Ss]tr|\$\d|^\d", t + "|" + a)
                 and not re.search(r"Selected|PrintVersion|Export", t)]
    print(f"  postbacky stránkování: {postbacky[:6]}")
    if postbacky:
        form, pole = formular_pole(vysledky.text, "form")
        if form is not None:
            nastav(pole, "__EVENTTARGET", postbacky[0][0])
            nastav(pole, "__EVENTARGUMENT", postbacky[0][1])
            s.stahni("us_stranka_postback", urljoin(vysledky.url, form.get("action") or ""),
                     metoda="POST", data=pole, headers={"Referer": vysledky.url})
    for zkouska in ("Results.aspx?page=1", "Results.aspx?page=2"):
        s.stahni("us_" + zkouska.replace(".aspx?", "_").replace("=", ""),
                 urljoin(US_HOST + "/Search/", zkouska))


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


CDM = ("PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
       "PREFIX cmr: <http://publications.europa.eu/ontology/cdm/cmr#>\n"
       "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n")
CELLAR_CELEX = "http://publications.europa.eu/resource/celex/{celex}"
EURLEX_HTML = "https://eur-lex.europa.eu/legal-content/{jazyk}/TXT/HTML/?uri=CELEX:{celex}"


def _sparql(s, nazev, dotaz):
    return s.stahni(nazev, SPARQL, metoda="POST",
                    data={"query": CDM + dotaz, "format": "application/sparql-results+json"},
                    headers={"Accept": "application/sparql-results+json"})


def cislo_veci(celex):
    """CELEX 62025CJ0151 -> „C-151/25" (T… = Tribunál); '' když to nejde."""
    m = re.match(r"^6(\d{4})([CT])[A-Z](\d{4})", celex or "")
    return f"{m.group(2)}-{int(m.group(3))}/{m.group(1)[2:]}" if m else ""


def sonda_sdeu(s, den):
    """Přesně ty dotazy, které posílá adaptér (judikatura/soudy/sdeu.py):
    SPARQL rozhodnutí a oznámení za 14 dní, InfoCuria pro rozsudek SD,
    rozsudek Tribunálu a stanovisko GA, Cellar XHTML pro čerstvý rozsudek
    a pro oznámení o předběžné otázce."""
    from judikatura.soudy import sdeu
    od = den - timedelta(days=14)
    hlav = {"Accept": "application/sparql-results+json"}
    rozh = s.stahni("sdeu_sparql_rozhodnuti", SPARQL, metoda="POST", headers=hlav,
                    data={"query": sdeu.dotaz_rozhodnuti(od, den),
                          "format": "application/sparql-results+json"})
    ozn = s.stahni("sdeu_sparql_oznameni", SPARQL, metoda="POST", headers=hlav,
                   data={"query": sdeu.dotaz_oznameni(od),
                         "format": "application/sparql-results+json"})
    zaznamy = []
    for r, prevod in ((rozh, sdeu.zaznamy_rozhodnuti), (ozn, sdeu.zaznamy_oznameni)):
        try:
            zaznamy += prevod(r.json())
        except Exception as e:
            print(f"  převod selhal: {type(e).__name__}: {e}")
    print(f"  záznamů: {len(zaznamy)}")
    priklady = []
    for druh, soud in (("rozsudek", "C"), ("rozsudek", "T"), ("stanovisko GA", "C"),
                       ("usnesení", "T"), (sdeu.DRUH_OTAZKA, "C")):
        z = next((z for z in zaznamy if z["druh"] == druh and z["spz"].startswith(soud)), None)
        if z:
            priklady.append(z)
    for z in priklady:
        celex = z["meta"]["celex"]
        s.stahni(f"sdeu_infocuria_{celex}", sdeu.INFOCURIA, metoda="POST",
                 json=sdeu.dotaz_infocuria(z["spz"]), headers=sdeu.HLAVICKY_CURIA)
    # Text: čerstvý rozsudek SD a oznámení o předběžné otázce.
    for z in [z for z in priklady if z["druh"] in ("rozsudek", sdeu.DRUH_OTAZKA)][:1] + \
            [z for z in priklady if z["druh"] == sdeu.DRUH_OTAZKA]:
        celex = z["meta"]["celex"]
        for jazyk in sdeu.TEXT_JAZYKY:
            r = s.stahni(f"sdeu_cellar_{celex}_{jazyk}", sdeu.CELLAR.format(celex=celex),
                         headers={"Accept": "application/xhtml+xml, text/html;q=0.9",
                                  "Accept-Language": jazyk})
            if r is not None and r.ok:
                break


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
