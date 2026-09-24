"""Nejvyšší soud: databáze rozhodnutí (Lotus Domino) a úřední deska.

Databáze nemá API, jen vyhledávací pohled vracející HTML. Dotaz jen podle
data zveřejnění server shazuje na 500 („Field is too large (32K)" nebo „Item
value exceeds maximum allowable size"), a to i podle toho, co se na server
zrovna posílalo předtím. Proto:
  - dotaz jde po rejstřících (Cdo, Tdo, NSČR…) a vždy s čerstvou session
    (úvodní stránka nastaví cookie, jako to dělá prohlížeč);
  - když rejstřík spadne i napodruhé, dotaz se rozdělí po senátech;
  - co nejde ani po senátech, se zapíše do logu a zkusí příští běh.

Úřední deska (civilní i trestní kolegium) ohlašuje vyhlášené rozsudky dřív,
než je databáze zveřejní; záznam z desky pak převezme záznam z databáze se
stejnou spisovou značkou (orchestr._prevezmi_desku).

Tvar stránek odpovídá odpovědím, které stáhla sonda (tests/fixtures/ns/).
"""

import io
import re
import time
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from feed_common import USER_AGENT
from judikatura import model

HOST = "https://rozhodnuti.nsoud.cz"
HLEDANI = HOST + "/Judikatura/judikatura_ns.nsf/$$WebSearch1"
DETAIL = HOST + "/Judikatura/judikatura_ns.nsf/WebSearch/{unid}?openDocument"
DESKY = (
    "https://www.nsoud.cz/uredni-deska/obcanskopravni-a-obchodni-kolegium/vyhlasovana-rozhodnuti",
    "https://www.nsoud.cz/uredni-deska/trestni-kolegium/vyhlasovana-rozhodnuti",
)
NSOUD_HOST = "https://www.nsoud.cz"
HLAVICKY = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
    "Referer": HOST + "/",
}

# Rejstříky, pod kterými NS zveřejňuje rozhodnutí. Civilní: dovolání (Cdo),
# insolvenční (NSČR, ICdo), přikázání a příslušnost (Nd), uznání cizích
# rozhodnutí (Ncu); trestní: dovolání (Tdo), stížnost pro porušení zákona
# (Tz), příslušnost (Td, Ntd), uznání (Tcu), vazba (Tvo).
REJSTRIKY_CIVILNI = ("cdo", "icdo", "nscr", "nd", "ncu")
REJSTRIKY_TRESTNI = ("tdo", "tz", "td", "ntd", "tcu", "tvo")
# Téměř vždy jen procesní rozhodnutí (přikázání věci, určení příslušnosti).
PROCESNI_REJSTRIKY = {"nd", "td", "ntd"}
# Senáty pro dělení dotazu, když celý rejstřík spadne.
SENATY_CIVILNI = tuple(range(20, 34))
SENATY_TRESTNI = (3, 4, 5, 6, 7, 8, 11, 15)
POCET = 200          # položek na stránku výsledků
MIN_TEXT = 2000      # kratší zbytek stránky je jen hlavička, ne odůvodnění
BEZ_VYSLEDKU = "Nebyly nalezeny žádné výsledky"   # prázdné hledání, žádná chyba
# Poučení o citaci, které NS dává nad každé rozhodnutí.
CITACE_RE = re.compile(r"^Citace rozhodnutí Nejvyššího soudu by měla.*?www\.nsoud\.cz\s*\.?\s*",
                       re.S)


def domino_datum(d):
    return d.strftime("%d.%m.%Y")


def cz_datum(text):
    """„20. 5. 2026" -> „2026-05-20"; '' když to nejde."""
    m = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})", text or "")
    if not m:
        return ""
    den, mes, rok = (int(x) for x in m.groups())
    return f"{rok:04d}-{mes:02d}-{den:02d}"


def abs_url(href, host=HOST):
    """Doplní host k relativní cestě a zakóduje mezery (syrový výstup Domina)."""
    if not href:
        return ""
    if href.startswith("/"):
        href = host + href
    return href.replace(" ", "%20")


def parse_seznam(html):
    """Řádky výsledků hledání -> záznamy (jen Nejvyšší soud, ne nižší soudy
    ze Sbírky, které databáze také drží)."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for row in soup.select("table#tabl tr"):
        odkaz = row.select_one("a.odk")
        if not odkaz:
            continue
        soud = row.select_one("td.td-short-wrap")
        if soud and "Nejvyšší soud" not in soud.get_text(" ", strip=True):
            continue
        spz = odkaz.get_text(" ", strip=True)
        ids = row.select_one("input[name='ids']")
        unid = (ids.get("value") if ids else "") or ""
        if not unid:
            m = re.search(r"/WebSearch/([0-9A-F]{32})", odkaz.get("href", ""), re.I)
            unid = m.group(1) if m else ""
        if not unid:
            continue
        pdf = ""
        for a in row.select("td.icons a[href]"):
            h = a["href"]
            if ".pdf?openelement" in h.lower() or h.lower().endswith(".pdf"):
                pdf = abs_url(h)
                break
        kategorie = row.select_one("td.category")
        senat, rejstrik = model.senat_z_spz(spz)
        out.append(model.novy_zaznam(
            "ns", f"ns:{unid.upper()}", spz=spz, url=abs_url(odkaz.get("href", "")) or
            DETAIL.format(unid=unid), pdf=pdf, senat=senat, rejstrik=rejstrik,
            meta={"kategorie": kategorie.get_text(strip=True) if kategorie else ""},
            procesni_meta=(rejstrik or "").lower() in PROCESNI_REJSTRIKY,
        ))
    return out


def parse_detail(html):
    """Metadata z tabulky detailu a text rozhodnutí pod ní."""
    soup = BeautifulSoup(html, "html.parser")
    info = {}
    for row in soup.select("table#tabl tr"):
        vlevo, vpravo = row.select_one("td.left-part"), row.select_one("td.right-part")
        if not vlevo or not vpravo:
            continue
        stitek = re.sub(r"\s+", " ", vlevo.get_text(" ", strip=True)).strip().rstrip(":").strip()
        radky = [r.strip() for r in vpravo.get_text("\n", strip=True).split("\n") if r.strip()]
        if stitek:
            info[stitek] = radky
    for smeti in soup(["head", "script", "style", "nav", "header", "footer", "form"]):
        smeti.decompose()
    # Tabulka metadat a nadpis stránky („Zpět na list", „Nové hledání").
    for smeti in soup.select("table#tabl, div.list-intro-heading"):
        smeti.decompose()
    telo = soup.select_one("div.main_detail") or soup
    text = re.sub(r"\n{3,}", "\n\n", telo.get_text("\n", strip=True))
    text = CITACE_RE.sub("", text)

    def jedno(k):
        return " ".join(info.get(k, [])).strip()

    druh = jedno("Typ rozhodnutí").lower()
    return {
        "soud": jedno("Soud"),
        "datum": cz_datum(jedno("Datum rozhodnutí")),
        "zverejneno": cz_datum(jedno("Zveřejněno na webu")),
        "druh": druh,
        "ecli": jedno("ECLI"),
        "meta": {
            "heslo_ns": "; ".join(info.get("Heslo", [])),
            "predpisy": "; ".join(info.get("Dotčené předpisy", [])),
            "kategorie": jedno("Kategorie rozhodnutí"),
            "pravni_veta": jedno("Právní věta"),
        },
        "text": text if len(text) >= MIN_TEXT else "",
    }


def parse_deska(html):
    """Úřední deska: vyhlášená rozhodnutí všech civilních senátů."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for row in soup.select("table tr"):
        bunky = row.find_all("td")
        if len(bunky) < 2:
            continue
        spz = model.normalizuj_spz(bunky[0].get_text(" ", strip=True))
        senat, rejstrik = model.senat_z_spz(spz)
        if senat is None:
            continue
        odkaz = row.find("a", href=True)
        pdf = abs_url(odkaz["href"], NSOUD_HOST) if odkaz else ""
        klic, cast = model.spz_klic(spz)
        # Datum vyhlášení je u rozsudku zároveň datem rozhodnutí.
        vyhlaseno = cz_datum(bunky[1].get_text(" ", strip=True))
        out.append(model.novy_zaznam(
            "ns", f"ns:deska:{klic}" + (f"-{cast}" if cast else ""), spz=spz, url=pdf,
            pdf=pdf, druh="rozsudek", senat=senat, rejstrik=rejstrik,
            datum=vyhlaseno, zverejneno=vyhlaseno,
            meta={"zdroj": "úřední deska (vyhlášené rozhodnutí)"},
        ))
    return out


def pdf_text(data):
    """Text PDF přes pypdf; '' když se nedá přečíst."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    except Exception:
        return ""


class NS:
    soud = "ns"
    # Pauza mezi stahováním detailů – první běh jich stahuje stovky a server
    # NS je citlivý (testy ji nulují).
    pauza = 0.5

    def __init__(self, session_factory=requests.Session, deska=True):
        self._session_factory = session_factory
        self._deska = deska
        self._texty = {}   # id -> text z detailu (ať se stránka nestahuje dvakrát)

    # --- hledání ---

    def _session(self):
        s = self._session_factory()
        try:
            s.get(HOST + "/", headers=HLAVICKY, timeout=30)
        except Exception:
            pass
        return s

    def _stranka(self, dotaz, start):
        """HTML stránky výsledků (i prázdné „Nebyly nalezeny…"); None, když server
        ani napodruhé neodpoví stránkou výsledků."""
        url = (f"{HLEDANI}?SearchView&Query={quote(dotaz)}&SearchMax=1000"
               f"&SearchOrder=4&Start={start}&Count={POCET}&pohled=1")
        for _ in range(2):
            try:
                r = self._session().get(url, headers=HLAVICKY, timeout=60)
            except Exception as e:
                print(f"    [ns] {dotaz}: {type(e).__name__}")
                continue
            if r.status_code == 200 and ('id="tabl"' in r.text or BEZ_VYSLEDKU in r.text):
                return r.text
            chyba = re.sub(r"<[^>]+>", " ", r.text)
            print(f"    [ns] {dotaz}: {r.status_code} {' '.join(chyba.split())[:90]}")
        return None

    def _hledej(self, dotaz, senaty=None):
        """Všechny stránky výsledků dotazu; při selhání po senátech."""
        out, start = [], 1
        while True:
            html = self._stranka(dotaz, start)
            if html is None:
                if senaty:
                    print(f"    [ns] {dotaz}: dělím po senátech")
                    for s in senaty:
                        out += self._hledej(f"[spzn1]={s} AND {dotaz}")
                return out
            radky = parse_seznam(html)
            out += radky
            if len(radky) < POCET:
                return out
            start += POCET

    def objev(self, od, do):
        """Rozhodnutí zveřejněná od `od` (databáze) a vyhlášená na desce."""
        nalezene = {}
        datum = domino_datum(od)
        for rejstriky, senaty in ((REJSTRIKY_CIVILNI, SENATY_CIVILNI),
                                  (REJSTRIKY_TRESTNI, SENATY_TRESTNI)):
            for rej in rejstriky:
                for z in self._hledej(f"[spzn2]={rej} AND [datum_predani_na_web]>={datum}", senaty):
                    nalezene[z["id"]] = z
        for url in DESKY if self._deska else ():
            try:
                r = self._session_factory().get(url, headers={"User-Agent": USER_AGENT},
                                                timeout=30)
                r.raise_for_status()
                for z in parse_deska(r.text):
                    if not z["zverejneno"] or z["zverejneno"] >= od.isoformat():
                        nalezene.setdefault(z["id"], z)
            except Exception as e:
                print(f"    [ns] úřední deska nedostupná ({url.rsplit('/', 2)[-2]}): "
                      f"{type(e).__name__}")
        return list(nalezene.values())

    # --- detail a text ---

    def doplnit(self, z):
        """Metadata z detailu (data, druh, ECLI, heslo, předpisy); text si
        adaptér schová pro AI v témže běhu."""
        if z["id"].startswith("ns:deska:") or not z.get("url"):
            return
        time.sleep(self.pauza)
        r = self._session_factory().get(z["url"], headers=HLAVICKY, timeout=30)
        r.raise_for_status()
        d = parse_detail(r.text)
        for k in ("datum", "zverejneno", "druh", "ecli"):
            if d[k]:
                z[k] = d[k]
        z["meta"] = {**(z.get("meta") or {}), **{k: v for k, v in d["meta"].items() if v}}
        if d["text"]:
            self._texty[z["id"]] = d["text"]

    def text(self, z):
        """Celý text: ze stránky rozhodnutí, jinak z PDF (text, případně PDF
        přímo pro model). PDF přikládá soud s odstupem i dní, text na
        stránce bývá hned."""
        text = self._texty.pop(z["id"], "")
        if not text and not z["id"].startswith("ns:deska:") and z.get("url"):
            try:
                r = self._session_factory().get(z["url"], headers=HLAVICKY, timeout=30)
                r.raise_for_status()
                text = parse_detail(r.text)["text"]
            except Exception as e:
                print(f"    [ns] {z['spz']}: detail nedostupný ({type(e).__name__})")
        if text:
            return {"text": text, "zdroj": "html"}
        if not z.get("pdf"):
            return {}
        try:
            r = self._session_factory().get(z["pdf"], headers=HLAVICKY, timeout=60)
            r.raise_for_status()
        except Exception as e:
            print(f"    [ns] {z['spz']}: PDF nedostupné ({type(e).__name__})")
            return {}
        if not r.content.startswith(b"%PDF"):
            return {}
        text = pdf_text(r.content)
        if len(text) >= MIN_TEXT:
            return {"text": text, "zdroj": "pdf-text"}
        return {"pdf": r.content, "zdroj": "pdf"}
