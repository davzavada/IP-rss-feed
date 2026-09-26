"""Nejvyšší soud: databáze rozhodnutí (Lotus Domino) a úřední deska.

Databáze nemá API, jen vyhledávací pohled vracející HTML. Dotaz jen podle
data zveřejnění server shazuje na 500 („Field is too large (32K)" nebo „Item
value exceeds maximum allowable size"), a to i podle toho, co se na server
zrovna posílalo předtím. Proto:
  - dotaz jde po rejstřících (Cdo, Tdo, NSČR…) a vždy s čerstvou session
    (úvodní stránka nastaví cookie, jako to dělá prohlížeč);
  - když rejstřík spadne i napodruhé chybou serveru (HTTP 500 a spol.),
    dotaz se rozdělí po senátech;
  - co nejde ani po senátech, se zapíše do logu a zkusí příští běh.

Výpadek serveru (timeout, spojení odmítnuté) je jiná věc než příliš široký
dotaz: po několika síťových selháních za sebou se databáze bere jako
nedostupná, další dotazy se neposílají a nedělí (jistič), a hledání má
i celkový časový limit – visící server nesmí sebrat čas ostatním soudům
a AI. Adaptér vrátí, co se stihlo (i desku), a selhání ohlásí orchestru
v `chyby` (databáze nedala nic) nebo `varovani` (jen část).

Úřední deska (civilní i trestní kolegium) ohlašuje vyhlášené rozsudky dřív,
než je databáze zveřejní; záznam z desky pak převezme záznam z databáze se
stejnou spisovou značkou (orchestr._prevezmi_predbezne). Deska ohlašuje
vyhlášení i předem, bez přílohy – bere se jen řádek s PDF a datem, které
už nastalo.

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
from judikatura.soudy.web import cz_datum  # noqa: F401 (i pro migraci)

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
# Stanoviska občanskoprávního a trestního kolegia a pléna – značka nemá
# senát, takže se dotaz po senátech nedělí (a je malý, na 500 nepadá).
REJSTRIKY_STANOVISKA = ("cpjn", "tpjn", "plsn")
# Téměř vždy jen procesní rozhodnutí (přikázání věci, určení příslušnosti).
PROCESNI_REJSTRIKY = {"nd", "td", "ntd"}
# Senáty pro dělení dotazu, když celý rejstřík spadne.
SENATY_CIVILNI = tuple(range(20, 34))
SENATY_TRESTNI = (3, 4, 5, 6, 7, 8, 11, 15)
POCET = 200          # položek na stránku výsledků
SITOVYCH_SELHANI = 3  # síťová selhání za sebou -> databáze nedostupná (jistič)
SENATU_NA_ZKOUSKU = 3  # když spadnou první tři senátní dotazy, dělení se vzdá
MAX_MINUT = 20       # celkový čas hledání v databázi za běh
TIMEOUT_UVOD = (10, 30)    # (spojení, odpověď) v sekundách
TIMEOUT_HLEDANI = (10, 60)
MIN_TEXT = 2000      # kratší zbytek stránky je jen hlavička, ne odůvodnění
BEZ_VYSLEDKU = "Nebyly nalezeny žádné výsledky"   # prázdné hledání, žádná chyba
# „Výsledky 1 - 14 z 14 zobrazovaných dokumentů." nad výpisem
POCET_RE = re.compile(r"Výsledky\s+\d+\s*-\s*\d+\s+z\s+(\d+)")
# Značka bez senátu (stanoviska): „Cpjn 202/2025", „Plsn 1/2015"
REJSTRIK_RE = re.compile(r"^\s*([^\d\s/]+)\s+\d+\s*/")
# Poučení o citaci, které NS dává nad každé rozhodnutí.
CITACE_RE = re.compile(r"^Citace rozhodnutí Nejvyššího soudu by měla.*?www\.nsoud\.cz\s*\.?\s*",
                       re.S)


def domino_datum(d):
    return d.strftime("%d.%m.%Y")


def abs_url(href, host=HOST):
    """Doplní host k relativní cestě a zakóduje mezery (syrový výstup Domina)."""
    if not href:
        return ""
    if href.startswith("/"):
        href = host + href
    return href.replace(" ", "%20")


def pocet_vysledku(html):
    """Kolik výsledků stránka hlásí; None, když to na ní není."""
    m = POCET_RE.search(html or "")
    return int(m.group(1)) if m else None


def pocet_radku(html):
    """Řádky výsledků před výběrem soudu (kvůli kontrole tvaru stránky)."""
    return len(BeautifulSoup(html or "", "html.parser").select("table#tabl tr a.odk"))


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
        if not rejstrik:
            m = REJSTRIK_RE.match(spz)
            rejstrik = m.group(1) if m else ""
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
        self._pripravit_hledani()

    def _pripravit_hledani(self):
        self.chyby, self.varovani = [], []   # pro orchestr (stav.json, workflow)
        self._konec = time.monotonic() + MAX_MINUT * 60
        self._sitova = 0          # síťová selhání za sebou
        self._nedostupna = False  # jistič: databáze neodpovídá, dál se nezkouší
        self._selhani = ""        # druh posledního selhání stránky: http / sit / cas
        self._precteno = 0        # stránky výsledků, které server dal
        self._neprectene = []     # dotazy, které nešly ani po senátech

    # --- hledání ---

    def _session(self):
        s = self._session_factory()
        try:
            s.get(HOST + "/", headers=HLAVICKY, timeout=TIMEOUT_UVOD)
        except Exception:
            pass
        return s

    def _stranka(self, dotaz, start):
        """HTML stránky výsledků (i prázdné „Nebyly nalezeny…"); None, když server
        ani napodruhé neodpoví stránkou výsledků. Druh selhání zůstane
        v `_selhani`: http (server odpověděl chybou – dotaz je nejspíš moc
        široký), sit (timeout, spojení), cas (vypršel limit hledání)."""
        url = (f"{HLEDANI}?SearchView&Query={quote(dotaz)}&SearchMax=1000"
               f"&SearchOrder=4&Start={start}&Count={POCET}&pohled=1")
        self._selhani = "http"
        for _ in range(2):
            if self._nedostupna:
                self._selhani = "sit"
                return None
            if time.monotonic() >= self._konec:
                self._selhani = "cas"
                return None
            try:
                r = self._session().get(url, headers=HLAVICKY, timeout=TIMEOUT_HLEDANI)
            except Exception as e:
                print(f"    [ns] {dotaz}: {type(e).__name__}")
                self._selhani = "sit"
                self._sitova += 1
                if self._sitova >= SITOVYCH_SELHANI:
                    print(f"    [ns] databáze {self._sitova}× za sebou neodpověděla – "
                          "další dotazy vynechávám")
                    self._nedostupna = True
                continue
            self._sitova = 0
            if r.status_code == 200 and ('id="tabl"' in r.text or BEZ_VYSLEDKU in r.text):
                self._precteno += 1
                return r.text
            self._selhani = "http"
            chyba = re.sub(r"<[^>]+>", " ", r.text)
            print(f"    [ns] {dotaz}: {r.status_code} {' '.join(chyba.split())[:90]}")
        return None

    def _hledej(self, dotaz, senaty=None):
        """Všechny stránky výsledků dotazu; když server dotaz odmítne (HTTP
        chyba), po senátech. Po výpadku spojení se nedělí – další dotazy by
        dopadly stejně. Vrací (záznamy, jestli se dotaz přečetl celý)."""
        out, start = [], 1
        while True:
            html = self._stranka(dotaz, start)
            if html is None:
                if senaty and self._selhani == "http":
                    print(f"    [ns] {dotaz}: dělím po senátech")
                    return self._po_senatech(dotaz, senaty, out)
                self._neprectene.append(dotaz)
                return out, False
            radky = parse_seznam(html)
            celkem, pred_vyberem = pocet_vysledku(html), pocet_radku(html)
            if start == 1 and celkem and not pred_vyberem:
                # Stránka hlásí výsledky, ale řádky se nepřečetly – změnil se
                # tvar výpisu. Tiše to projít nesmí.
                if not any("přečteno 0" in c for c in self.chyby):
                    self.chyby.append(f"výpis hlásí {celkem} výsledků, přečteno 0 "
                                      "(změna tvaru stránky?)")
                self._neprectene.append(dotaz)
                return out, False
            out += radky
            if pred_vyberem < POCET:
                return out, True
            start += POCET

    def _po_senatech(self, dotaz, senaty, out):
        cely, prectenych = True, 0
        for i, s in enumerate(senaty):
            radky, ok = self._hledej(f"[spzn1]={s} AND {dotaz}")
            out += radky
            if not ok:
                cely = False
            else:
                prectenych += 1
            if not prectenych and i + 1 >= SENATU_NA_ZKOUSKU:
                # Ani úzké dotazy neprojdou – server je dole, ne dotaz široký.
                print(f"    [ns] {dotaz}: ani po senátech nic, zbytek vynechávám")
                self._neprectene.append(f"{dotaz} (zbylé senáty)")
                return out, False
        return out, cely

    def objev(self, od, do):
        """Rozhodnutí zveřejněná od `od` (databáze) a vyhlášená na desce
        (s PDF, vyhlášená od `od` do `do`)."""
        self._pripravit_hledani()
        nalezene = {}
        datum = domino_datum(od)
        dotazu = 0
        for rejstriky, senaty in ((REJSTRIKY_CIVILNI, SENATY_CIVILNI),
                                  (REJSTRIKY_TRESTNI, SENATY_TRESTNI),
                                  (REJSTRIKY_STANOVISKA, None)):
            for rej in rejstriky:
                dotaz = f"[spzn2]={rej} AND [datum_predani_na_web]>={datum}"
                dotazu += 1
                radky, _ = self._hledej(dotaz, senaty)
                for z in radky:
                    nalezene[z["id"]] = z
        if not self._precteno:
            self.chyby.append(f"databáze NS nedala výsledky na žádný z {dotazu} dotazů"
                              + (" (nedostupná)" if self._nedostupna else ""))
        elif self._neprectene:
            duvod = (" (spojení vypadlo)" if self._nedostupna
                     else " (vypršel čas hledání)" if time.monotonic() >= self._konec else "")
            self.varovani.append(f"databáze NS: nenačteno {len(self._neprectene)} dotazů{duvod}, např. "
                                 + "; ".join(self._neprectene[:3]))
        for url in DESKY if self._deska else ():
            try:
                r = self._session_factory().get(url, headers={"User-Agent": USER_AGENT},
                                                timeout=30)
                r.raise_for_status()
                for z in parse_deska(r.text):
                    # Předem ohlášené vyhlášení (bez přílohy, s datem, které
                    # teprve přijde) ještě není rozhodnutí – najde se po něm.
                    if z["pdf"] and (not z["zverejneno"]
                                     or od.isoformat() <= z["zverejneno"] <= do.isoformat()):
                        nalezene.setdefault(z["id"], z)
            except Exception as e:
                kolegium = url.rsplit('/', 2)[-2]
                print(f"    [ns] úřední deska nedostupná ({kolegium}): {type(e).__name__}")
                self.varovani.append(f"úřední deska nedostupná ({kolegium}): {type(e).__name__}")
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
        if not d["zverejneno"]:
            # Bez data zveřejnění by se staré rozhodnutí tvářilo jako nové –
            # orchestr ho odloží na další běh a ohlásí to.
            raise RuntimeError("detail NS bez data zveřejnění (změna tvaru stránky?)")
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
