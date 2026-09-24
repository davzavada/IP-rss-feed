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

Doplňkově ipcuria.eu (soudy/ipcuria.py): předběžné otázky z duševního
vlastnictví a ochrany údajů podané za poslední měsíc – dva až tři měsíce
před oznámením v ÚV. Text k nim je žádost o rozhodnutí o předběžné otázce
v InfoCurii (dokument DDP), oznámení v Cellaru, nebo otázky na ipcuria.eu.

Název věci a texty dokumentů dává InfoCuria podle čísla věci (česky, když
překlad už je). Čerstvý rozsudek bývá první dny jen v jazyce řízení
a francouzsky – pak text vezmeme z Cellaru (XHTML česky, anglicky,
francouzsky) a AI ho shrne česky i tak. Odkaz pro čtenáře vede na věc na
webu Soudního dvora (curia.europa.eu – všechny dokumenty věci); z Actions
je EUR-Lex pro stahování zavřený.

Tvar odpovědí odpovídá tomu, co stáhla sonda (tests/fixtures/sdeu/).
"""

import re
import time
from datetime import timedelta

import requests
from bs4 import BeautifulSoup

from feed_common import USER_AGENT
from judikatura import model
from judikatura.soudy import ipcuria
from judikatura.soudy.web import radky_html

SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"
CELLAR = "http://publications.europa.eu/resource/celex/{celex}"
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
IPCURIA_DNI = 30     # z ipcuria jen otázky podané za poslední měsíc (okno SDEU)
# U předběžné otázky z ipcuria: žádost, pak oznámení o ní (dokumenty InfoCurie).
DOKUMENTY_OTAZKY = ("DDP", "DDP_COMM")
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
            url=ipcuria.CURIA.format(vec=vec), rejstrik=vec[0],
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
            url=ipcuria.CURIA.format(vec=vec), rejstrik=vec[0],
            meta={"soud_eu": SOUDY.get(vec[0], ""), "celex": celex, "predkladajici_soud": soud,
                  "nazev_oznameni": nazev},
        )
    return list(out.values())


def zaznamy_ipcurie(seznam, od):
    """Předběžné otázky z ipcuria.eu podané od `od`. Datum zveřejnění zůstává
    prázdné – první výskyt je den, kdy je Owl uviděl."""
    out = []
    for r in seznam:
        if not r["podano"] or r["podano"] < od.isoformat():
            continue
        out.append(model.novy_zaznam(
            "sdeu", ipcuria.PREFIX + r["vec"], spz=r["vec"], druh=DRUH_OTAZKA, nazev=r["nazev"],
            datum=r["podano"], url=ipcuria.CURIA.format(vec=r["vec"]), rejstrik="C",
            meta={"soud_eu": SOUDY["C"], "celex": ipcuria.celex_oznameni(r["vec"]),
                  "ipcuria": ipcuria.VEC.format(vec=r["vec"]),
                  "kategorie_ipcuria": "; ".join(r["kategorie"])},
            oblasti_meta=ipcuria.oblasti(r["kategorie"]),
        ))
    return out


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


def _text_dokumentu(dc):
    """Text dokumentu: česky, anglicky, francouzsky, jinak nejdelší jazyk
    (čerstvá žádost bývá jen v jazyce řízení – AI ji shrne česky i tak)."""
    jazyky = {k: v for m in dc.get("contentML") or [] if isinstance(m, dict)
              for k, v in m.items() if v}
    text = jazyky.get("cs") or jazyky.get("en") or jazyky.get("fr") or \
        max(jazyky.values(), key=len, default="")
    # InfoCuria dává před text někdy doslova „null".
    return re.sub(r"^\s*null\s+", "", text).strip()


def dokumenty_z_infocurie(odpoved, vec):
    """(název věci, [{typ, celex, text}]) z odpovědi InfoCurie."""
    for hit in (odpoved or {}).get("searchHits") or []:
        c = hit.get("content") or {}
        # „C-1028/26 (PPU)", „C-1036/26 P" – číslo věci je první slovo.
        if vec not in {(c.get(k) or "").split(" ")[0] for k in ("publishedId", "publishedAffId")}:
            continue
        nazvy = {k: v for m in c.get("usualNameML") or [] if isinstance(m, dict) for k, v in m.items() if v}
        dokumenty = []
        for d in ((hit.get("innerHits") or {}).get("document") or {}).get("searchHits") or []:
            dc = d.get("content") or {}
            text = _text_dokumentu(dc)
            if text:
                dokumenty.append({"typ": dc.get("docTypeCode") or "", "celex": dc.get("celex") or "",
                                  "text": text})
        return (nazvy.get("cs") or nazvy.get("en") or nazvy.get("fr") or ""), dokumenty
    return "", []


def vec_z_infocurie(odpoved, vec):
    """(název věci, {celex: text dokumentu}) z odpovědi InfoCurie."""
    nazev, dokumenty = dokumenty_z_infocurie(odpoved, vec)
    return nazev, {d["celex"]: d["text"] for d in dokumenty if d["celex"]}


def text_zaznamu(z, dokumenty):
    """Text dokumentu, ke kterému záznam patří: u rozhodnutí podle CELEX,
    u předběžné otázky z ipcuria žádost (DDP), jinak oznámení o ní."""
    if z["id"].startswith(ipcuria.PREFIX):
        for typ in DOKUMENTY_OTAZKY:
            texty = [d["text"] for d in dokumenty if d["typ"] == typ]
            if texty:
                return max(texty, key=len)
        return ""
    celex = (z.get("meta") or {}).get("celex", "")
    return next((d["text"] for d in dokumenty if celex and d["celex"] == celex), "")


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
        self._texty = {}     # id záznamu -> text z InfoCurie (ať se věc nehledá dvakrát)

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
        """Rozhodnutí s datem od `od`, oznámení o předběžných otázkách, která
        přibyla od `od`, a z ipcuria otázky podané za poslední měsíc (web je
        ukazuje až s odstupem, proto delší lhůta než u ostatních)."""
        rozhodnuti = zaznamy_rozhodnuti(self._sparql(dotaz_rozhodnuti(od, do)))
        try:
            otazky = zaznamy_oznameni(self._sparql(dotaz_oznameni(od)))
        except Exception as e:   # oznámení počkají, rozhodnutí ne
            print(f"    [sdeu] oznámení o nových věcech nedostupná ({type(e).__name__})")
            otazky = []
        try:
            r = self._s().get(ipcuria.SEZNAM, headers=HLAVICKY, timeout=60)
            r.raise_for_status()
            rane = zaznamy_ipcurie(ipcuria.parse_seznam(r.text), do - timedelta(days=IPCURIA_DNI))
        except Exception as e:   # doplňkový zdroj – bez něj se jede dál
            print(f"    [sdeu] ipcuria.eu nedostupná ({type(e).__name__})")
            rane = []
        return rozhodnuti + otazky + rane

    def _infocuria(self, z):
        time.sleep(self.pauza)
        r = self._s().post(INFOCURIA, json=dotaz_infocuria(z["spz"]), headers=HLAVICKY_CURIA,
                           timeout=60)
        r.raise_for_status()
        return dokumenty_z_infocurie(r.json(), z["spz"])

    def doplnit(self, z):
        """Název věci z InfoCurie; text dokumentu si schová pro AI."""
        nazev, dokumenty = self._infocuria(z)
        if nazev and not z.get("nazev"):
            z["nazev"] = nazev
        text = text_zaznamu(z, dokumenty)
        if text:
            self._texty[z["id"]] = text

    def text(self, z):
        """Text: InfoCuria (česky, jinak anglicky či francouzsky), jinak
        Cellar v češtině, angličtině, francouzštině; u předběžné otázky
        z ipcuria nakonec otázky z její stránky."""
        celex = (z.get("meta") or {}).get("celex", "")
        text = self._texty.pop(z["id"], "")
        if not text:
            try:
                text = text_zaznamu(z, self._infocuria(z)[1])
            except Exception as e:
                print(f"    [sdeu] {z['spz']}: InfoCuria nedostupná ({type(e).__name__})")
        if len(text) >= MIN_TEXT:
            return {"text": text, "zdroj": "infocuria"}
        obsah = self._text_cellar(z, celex)
        if obsah or not z["id"].startswith(ipcuria.PREFIX):
            return obsah
        try:
            r = self._s().get(z["meta"]["ipcuria"], headers=HLAVICKY, timeout=60)
            r.raise_for_status()
        except Exception as e:
            print(f"    [sdeu] {z['spz']}: ipcuria.eu nedostupná ({type(e).__name__})")
            return {}
        text = ipcuria.text_vec(r.text)
        return {"text": text, "zdroj": "ipcuria"} if len(text) >= MIN_TEXT else {}

    def _text_cellar(self, z, celex):
        if not celex:
            return {}
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
