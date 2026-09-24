"""Soudní dvůr EU (Soudní dvůr a Tribunál): Cellar a InfoCuria.

Objevování jde přes SPARQL Cellaru (datové API Úřadu pro publikace EU):
  - rozhodnutí: díla judikatury (sektor 6, CELEX 6…) podle data dokumentu –
    rozsudky, usnesení a stanoviska generálních advokátů (typ zdroje JUDG,
    ORDER, OPIN_AG, VIEW_AG). Shrnutí, abstrakty a výtahy (ABSTRACT_JUR,
    SUM_JUR, JUDG_EXTRACT) ne. Rozsudek je v Cellaru už v den vyhlášení.
  - nové předběžné otázky: oznámení o nové věci v Úředním věstníku (CELEX
    typ CN) podle dne, kdy přibylo do Cellaru. Vychází dva až tři měsíce
    po podání, ale obsahuje položené otázky; datem dokumentu je den podání.
    Žaloby a kasační opravné prostředky se neberou (poznají se podle názvu).

Název věci a texty dokumentů dává InfoCuria podle čísla věci (česky, když
překlad už je). Čerstvý rozsudek bývá první dny jen v jazyce řízení
a francouzsky – pak text vezmeme z Cellaru (XHTML česky, anglicky,
francouzsky) a AI ho shrne česky i tak. Odkaz pro čtenáře vede na EUR-Lex
(český text, jakmile vyjde); z Actions je EUR-Lex pro stahování zavřený.

Tvar odpovědí odpovídá tomu, co stáhla sonda (tests/fixtures/sdeu/).
"""

import re
import time

import requests
from bs4 import BeautifulSoup

from feed_common import USER_AGENT
from judikatura import model
from judikatura.soudy.web import radky_html

SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"
CELLAR = "http://publications.europa.eu/resource/celex/{celex}"
EURLEX = "https://eur-lex.europa.eu/legal-content/CS/TXT/?uri=CELEX:{celex}"
INFOCURIA_APP = "https://infocuria.curia.europa.eu"
INFOCURIA = "https://infocuriaws.curia.europa.eu/elastic-connector/search"
TYP = "http://publications.europa.eu/resource/authority/resource-type/"
JAZYK = "http://publications.europa.eu/resource/authority/language/"

# Typ zdroje -> druh rozhodnutí na webu (a pro pokyn AI, viz analyza.pokyn).
DRUHY = {"JUDG": "rozsudek", "ORDER": "usnesení", "OPIN_AG": "stanovisko GA",
         "VIEW_AG": "stanovisko GA"}
DRUH_OTAZKA = "předběžná otázka"
# Název oznámení v ÚV: „Žádost o rozhodnutí o předběžné otázce podaná …"
OTAZKA_RE = re.compile(r"předběžn\w* otáz|preliminary ruling", re.I)
SOUDY = {"C": "Soudní dvůr", "T": "Tribunál"}
TEXT_JAZYKY = ("ces", "eng", "fra")
MIN_TEXT = 500
HLAVICKY = {"User-Agent": USER_AGENT}
HLAVICKY_CURIA = {
    "User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json", "Origin": INFOCURIA_APP, "Referer": INFOCURIA_APP + "/",
}

_PREFIXY = ("PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
            "PREFIX cmr: <http://publications.europa.eu/ontology/cdm/cmr#>\n"
            "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n")


def dotaz_rozhodnuti(od, do):
    """SPARQL: rozhodnutí a stanoviska s datem dokumentu od–do."""
    typy = ", ".join(f"<{TYP}{t}>" for t in DRUHY)
    return _PREFIXY + f"""SELECT DISTINCT ?celex ?datum ?typ ?ecli WHERE {{
  ?dilo cdm:resource_legal_id_celex ?celex ;
        cdm:work_date_document ?datum ;
        cdm:work_has_resource-type ?typ .
  OPTIONAL {{ ?dilo cdm:case-law_ecli ?ecli }}
  FILTER(STRSTARTS(STR(?celex), "6"))
  FILTER(?typ IN ({typy}))
  FILTER(?datum >= "{od.isoformat()}"^^xsd:date && ?datum <= "{do.isoformat()}"^^xsd:date)
}} ORDER BY DESC(?datum) LIMIT 2000"""


def dotaz_oznameni(od):
    """SPARQL: oznámení o nových věcech (CN) vložená do Cellaru od `od`,
    s českým a anglickým názvem."""
    return _PREFIXY + f"""SELECT DISTINCT ?celex ?podano ?vlozeno ?nazev_cs ?nazev_en WHERE {{
  ?dilo cdm:resource_legal_id_celex ?celex ;
        cdm:resource_legal_type "CN"^^xsd:string ;
        cmr:creationDate ?vlozeno .
  OPTIONAL {{ ?dilo cdm:work_date_document ?podano }}
  OPTIONAL {{ ?v1 cdm:expression_belongs_to_work ?dilo ;
                 cdm:expression_uses_language <{JAZYK}CES> ; cdm:expression_title ?nazev_cs }}
  OPTIONAL {{ ?v2 cdm:expression_belongs_to_work ?dilo ;
                 cdm:expression_uses_language <{JAZYK}ENG> ; cdm:expression_title ?nazev_en }}
  FILTER(?vlozeno >= "{od.isoformat()}T00:00:00"^^xsd:dateTime)
}} ORDER BY DESC(?vlozeno) LIMIT 2000"""


def cislo_veci(celex):
    """CELEX 62025CJ0151 -> „C-151/25" (T… = Tribunál); '' když to nejde."""
    m = re.match(r"^6(\d{4})([CT])[A-Z]{1,2}(\d{4})", celex or "")
    return f"{m.group(2)}-{int(m.group(3))}/{m.group(1)[2:]}" if m else ""


def _hodnoty(odpoved):
    """Řádky výsledku SPARQL jako slovníky {proměnná: hodnota}."""
    try:
        radky = odpoved["results"]["bindings"]
    except (KeyError, TypeError):
        raise RuntimeError("SDEU: odpověď SPARQL nemá výsledky")
    return [{k: v.get("value", "") for k, v in r.items()} for r in radky]


def zaznamy_rozhodnuti(odpoved):
    out = {}
    for r in _hodnoty(odpoved):
        celex, typ = r.get("celex", ""), r.get("typ", "").rsplit("/", 1)[-1]
        vec = cislo_veci(celex)
        if not vec or typ not in DRUHY or celex in out:
            continue
        out[celex] = model.novy_zaznam(
            "sdeu", f"sdeu:{celex}", spz=vec, ecli=r.get("ecli", ""), druh=DRUHY[typ],
            datum=r.get("datum", "")[:10], zverejneno=r.get("datum", "")[:10],
            url=EURLEX.format(celex=celex), rejstrik=vec[0],
            meta={"soud_eu": SOUDY.get(vec[0], ""), "celex": celex},
        )
    return list(out.values())


def _z_nazvu_oznameni(nazev):
    """Z názvu oznámení účastníka (název věci) a předkládající soud:
    „Věc C-630/26, Rada Miasta Krakowa: Žádost o rozhodnutí o předběžné
    otázce, kterou podal Naczelny Sąd Administracyjny (Polsko) dne …"."""
    nazev = " ".join((nazev or "").split())
    vec = re.match(r"^Věc\s+[^,:]+,\s*(.+?):", nazev)
    soud = re.search(r"(?:kterou|který)\s+podal[ao]?\s+(.+?)\s+dne\s+\d", nazev)
    return (soud.group(1) if soud else ""), (vec.group(1) if vec else "")


def zaznamy_oznameni(odpoved):
    """Oznámení o nových předběžných otázkách (žaloby a opravné prostředky ne)."""
    out = {}
    for r in _hodnoty(odpoved):
        celex = r.get("celex", "")
        vec = cislo_veci(celex)
        # Názvy mají nezlomitelné mezery – split() je srovná na obyčejné.
        nazev = " ".join((r.get("nazev_cs") or r.get("nazev_en") or "").split())
        if not vec or celex in out or not OTAZKA_RE.search(nazev):
            continue
        soud, strany = _z_nazvu_oznameni(r.get("nazev_cs") or "")
        out[celex] = model.novy_zaznam(
            "sdeu", f"sdeu:{celex}", spz=vec, druh=DRUH_OTAZKA, nazev=strany,
            datum=r.get("podano", "")[:10], zverejneno=r.get("vlozeno", "")[:10],
            url=EURLEX.format(celex=celex), rejstrik=vec[0],
            meta={"soud_eu": SOUDY.get(vec[0], ""), "celex": celex, "predkladajici_soud": soud,
                  "nazev_oznameni": nazev},
        )
    return list(out.values())


def dotaz_infocuria(vec):
    """Tělo hledání věci v InfoCurii podle čísla (přesně, česky)."""
    return {
        "searchTerm": f'"{vec}"', "multiSearchTerms": [],
        "sortTermList": [{"sortDirection": "DESC", "sortTerm": "SCORE"}],
        "pagination": {"pageNumber": 0, "pageSize": 20, "from": 1, "to": 20},
        "language": "CS", "tabName": "affair", "isAllTabsRequest": True, "ecli": "",
        "publishedId": vec, "usualName": "", "logicDocId": "", "repJurExpand": True,
        "filtersValue": [], "advancedFiltersValue": [], "isSearchExact": True,
        "searchSources": ["document", "metadata"],
    }


def vec_z_infocurie(odpoved, vec):
    """(název věci, {celex: text dokumentu}) z odpovědi InfoCurie."""
    for hit in (odpoved or {}).get("searchHits") or []:
        c = hit.get("content") or {}
        # „C-1028/26 (PPU)", „C-1036/26 P" – číslo věci je první slovo.
        if vec not in {(c.get(k) or "").split(" ")[0] for k in ("publishedId", "publishedAffId")}:
            continue
        nazvy = {k: v for m in c.get("usualNameML") or [] if isinstance(m, dict) for k, v in m.items() if v}
        texty = {}
        for d in ((hit.get("innerHits") or {}).get("document") or {}).get("searchHits") or []:
            dc = d.get("content") or {}
            jazyky = {k: v for m in dc.get("contentML") or [] if isinstance(m, dict)
                      for k, v in m.items() if v}
            text = jazyky.get("cs") or jazyky.get("en") or jazyky.get("fr") or ""
            if dc.get("celex") and text:
                texty[dc["celex"]] = text.strip()
        return (nazvy.get("cs") or nazvy.get("en") or nazvy.get("fr") or ""), texty
    return "", {}


def text_z_cellaru(html):
    """Text dokumentu z XHTML Cellaru (bez skriptů, stylů a hlavičky)."""
    soup = BeautifulSoup(html or "", "html.parser")
    for smeti in soup(["head", "script", "style"]):
        smeti.decompose()
    telo = soup.body or soup
    return radky_html(telo.decode_contents())


class SDEU:
    soud = "sdeu"
    pauza = 0.3   # mezi dotazy na InfoCurii a Cellar (testy ji nulují)

    def __init__(self, session_factory=requests.Session):
        self._session_factory = session_factory
        self._relace = None
        self._texty = {}     # celex -> text z InfoCurie (ať se věc nehledá dvakrát)

    def _s(self):
        if self._relace is None:
            self._relace = self._session_factory()
        return self._relace

    def _sparql(self, dotaz):
        r = self._s().post(SPARQL, data={"query": dotaz, "format": "application/sparql-results+json"},
                           headers=dict(HLAVICKY, Accept="application/sparql-results+json"),
                           timeout=90)
        r.raise_for_status()
        return r.json()

    def objev(self, od, do):
        """Rozhodnutí s datem od `od` a oznámení o předběžných otázkách, která
        přibyla od `od`."""
        rozhodnuti = zaznamy_rozhodnuti(self._sparql(dotaz_rozhodnuti(od, do)))
        try:
            otazky = zaznamy_oznameni(self._sparql(dotaz_oznameni(od)))
        except Exception as e:   # oznámení počkají, rozhodnutí ne
            print(f"    [sdeu] oznámení o nových věcech nedostupná ({type(e).__name__})")
            otazky = []
        return rozhodnuti + otazky

    def _infocuria(self, z):
        time.sleep(self.pauza)
        r = self._s().post(INFOCURIA, json=dotaz_infocuria(z["spz"]), headers=HLAVICKY_CURIA,
                           timeout=60)
        r.raise_for_status()
        return vec_z_infocurie(r.json(), z["spz"])

    def doplnit(self, z):
        """Název věci z InfoCurie; text dokumentu si schová pro AI."""
        nazev, texty = self._infocuria(z)
        if nazev and not z.get("nazev"):
            z["nazev"] = nazev
        celex = (z.get("meta") or {}).get("celex", "")
        if texty.get(celex):
            self._texty[celex] = texty[celex]

    def text(self, z):
        """Text: InfoCuria (česky, jinak anglicky či francouzsky), jinak
        Cellar v češtině, angličtině, francouzštině."""
        celex = (z.get("meta") or {}).get("celex", "")
        text = self._texty.pop(celex, "")
        if not text:
            try:
                text = self._infocuria(z)[1].get(celex, "")
            except Exception as e:
                print(f"    [sdeu] {z['spz']}: InfoCuria nedostupná ({type(e).__name__})")
        if len(text) >= MIN_TEXT:
            return {"text": text, "zdroj": "infocuria"}
        for jazyk in TEXT_JAZYKY:
            try:
                r = self._s().get(CELLAR.format(celex=celex), timeout=60, headers=dict(
                    HLAVICKY, Accept="application/xhtml+xml, text/html;q=0.9",
                    **{"Accept-Language": jazyk}))
            except Exception as e:
                print(f"    [sdeu] {z['spz']}: Cellar nedostupný ({type(e).__name__})")
                return {}
            if r.status_code == 404:   # v tom jazyce ještě není
                continue
            if r.status_code >= 400:
                print(f"    [sdeu] {z['spz']}: Cellar {r.status_code}")
                return {}
            text = text_z_cellaru(r.text)
            if len(text) >= MIN_TEXT:
                return {"text": text, "zdroj": f"cellar-{jazyk}"}
        return {}
