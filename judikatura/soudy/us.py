"""Ústavní soud: NALUS (nalus.usoud.cz).

Hledání je stránka ASP.NET WebForms: GET formuláře dá viewstate a cookie
relace, POST s „Datum zpřístupnění" od–do vrátí první stránku výsledků.
Výsledky zůstávají v relaci, další stránky jsou GET Results.aspx?page=N
(číslováno od nuly, první je ta z POSTu).

Výpis nese všechno podstatné: značku, ECLI, soudce zpravodaje, populární
název, data rozhodnutí, vyhlášení, podání a zpřístupnění, dotčené předpisy,
formu, význam, typ výroku, předmět řízení a věcný rejstřík. Detail
(ResultDetail.aspx) je vázaný na relaci hledání, proto se nestahuje. Text
je na trvalé adrese GetText.aspx?sz=… – ta je zároveň odkazem pro čtenáře.

Tvar stránek odpovídá odpovědím, které stáhla sonda (tests/fixtures/us/).
"""

import re
from urllib.parse import unquote, urljoin

import requests
from bs4 import BeautifulSoup

from judikatura import mapy, model
from judikatura.soudy.web import HLAVICKY, cz_datum, formular_pole, jmeno, nastav, radky_html

HOST = "https://nalus.usoud.cz"
HLEDANI = HOST + "/Search/Search.aspx"
VYSLEDKY = HOST + "/Search/Results.aspx"
TEXT = HOST + "/Search/GetText.aspx?sz={sz}"
POLE = "ctl00$MainContent$"
NA_STRANKU = 80
MAX_STRAN = 40       # pojistka: 40 × 80 je víc, než ÚS zveřejní za čtvrt roku
MIN_TEXT = 300       # i krátké usnesení o odmítnutí pro vady má víc
PRAZDNE = "nebyly nalezeny žádné záznamy"
RAZENI_ZPRISTUPNENI = "20"   # „data zpřístupnění sestupně"

# Výroky bez věcného posouzení (§ 43 odst. 1 zákona o ÚS, zastavení,
# procesní výroky o nákladech). Odmítnutí pro zjevnou neopodstatněnost
# (§ 43 odst. 2) je věcné posouzení – procesní není.
PROCESNI_VYROKY = ("odmítnuto pro neodstraněné vady", "odmítnuto pro nepřípustnost",
                   "odmítnuto pro nepříslušnost", "odmítnuto pro opožděnost",
                   "odmítnuto pro nedodržení lhůty", "odmítnuto pro neoprávněnost",
                   "odmítnuto - pro", "zastaveno", "procesní")


def us_datum(d):
    return f"{d.day}.{d.month}.{d.year}"


def procesni(vyroky):
    vyroky = [v.strip().lower() for v in vyroky if v.strip()]
    return bool(vyroky) and all(v.startswith(PROCESNI_VYROKY) for v in vyroky)


def spisova_znacka(odkaz, citace=""):
    """„IV.ÚS 465/26 #1" (nebo citace „nález sp. zn. IV. ÚS 465/26 ze dne…")
    -> „IV. ÚS 465/26"."""
    m = re.search(r"sp\.\s*zn\.\s*(.+?)\s+ze dne", citace or "")
    s = m.group(1) if m else re.sub(r"\s*#\d+\s*$", "", odkaz or "")
    return re.sub(r"\.\s*ÚS\b", ". ÚS", " ".join(s.split())).strip()


def _casti(td):
    """Obsah buňky rozdělený podle <br> (prázdné části zůstávají – oddělují
    navrhovatele od populárního názvu)."""
    if td is None:
        return []
    return [" ".join(radky_html(c).split()) for c in re.split(r"(?i)<br\s*/?>", td.decode_contents())]


def parse_vysledky(html):
    """Stránka výsledků -> (řádky, celkem). Prázdné hledání -> ([], 0);
    stránka, která není výsledkem hledání, -> ([], None)."""
    soup = BeautifulSoup(html or "", "html.parser")
    m = re.search(r"Výsledky\s+\d+\s*-\s*\d+\s+z celkem\s+(\d+)", soup.get_text(" "))
    if not m:
        return [], (0 if PRAZDNE in (html or "") else None)
    out = []
    for a in soup.find_all("a", href=re.compile(r"ResultDetail\.aspx", re.I)):
        tr = a.find_parent("tr")
        tds = tr.find_all("td", recursive=False) if tr else []
        akce = tr.find_next_sibling("tr") if tr else None
        akce_html = str(akce) if akce is not None else ""
        sz = re.search(r"GetText\.aspx\?sz=([^\"'&\s]+)", akce_html)
        if len(tds) < 9 or not sz:
            continue
        citace = re.search(r'ShowLink\("([^"]*sp\. zn\.[^"]*)"', akce_html)
        hlava = _casti(tds[1])
        navrh = _casti(tds[2])
        mezera = navrh.index("") if "" in navrh else len(navrh)
        data = [c for c in _casti(tds[3]) if c]
        forma = [c for c in _casti(tds[5]) if c]
        vecne = [c for c in _casti(tds[8]) if c]
        out.append({
            "sz": unquote(sz.group(1)),
            "spz": spisova_znacka(a.get_text(" ", strip=True), citace.group(1) if citace else ""),
            "ecli": next((c for c in hlava if c.startswith("ECLI:")), ""),
            "soudce": jmeno(hlava[-1]) if len(hlava) >= 3 and not hlava[-1].startswith("ECLI") else "",
            "navrhovatel": [c for c in navrh[:mezera] if c],
            "nazev": " ".join(c for c in navrh[mezera:] if c),
            "datum": cz_datum(data[0]) if data else "",
            "vyhlaseno": next((cz_datum(c) for c in data[1:] if c.startswith("(")), ""),
            "zverejneno": cz_datum(data[-1]) if len(data) > 1 else "",
            "predpisy": [c for c in _casti(tds[4]) if c],
            "forma": forma[0] if forma else "",
            "vyznam": forma[1] if len(forma) > 1 else "",
            "vyroky": [c for c in _casti(tds[6]) if c],
            "predmet": [c for c in vecne if "/" in c],
            "rejstrik": [c for c in vecne if "/" not in c],
        })
    return out, int(m.group(1))


# Ústavní zákony a Úmluva jsou skoro u každého rozhodnutí – o oblasti nic neřeknou.
USTAVNI_PREDPISY = ("1/1993", "2/1993", "209/1992", "182/1993")


def oblasti_meta(r):
    predpisy = [p for p in r["predpisy"] if not any(p.startswith(u) for u in USTAVNI_PREDPISY)]
    plenum = ["ustavni"] if r["spz"].startswith("Pl.") else []
    return mapy.spoj(plenum, mapy.z_predpisu(predpisy), mapy.z_rejstriku(r["rejstrik"] + r["predmet"]))


def zaznam_z_radku(r):
    return model.novy_zaznam(
        "us", f"us:{r['sz']}", spz=r["spz"], ecli=r["ecli"], druh=r["forma"].lower(),
        datum=r["datum"], zverejneno=r["zverejneno"], nazev=r["nazev"],
        url=TEXT.format(sz=r["sz"]), rejstrik="ÚS",
        meta={k: v for k, v in {
            "soudce": r["soudce"],
            "navrhovatel": "; ".join(r["navrhovatel"]),
            "vyrok": "; ".join(r["vyroky"]),
            "predmet_rizeni": "; ".join(r["predmet"]),
            "vecny_rejstrik": ", ".join(r["rejstrik"]),
            "predpisy": "; ".join(r["predpisy"]),
            "vyznam": r["vyznam"],
            "vyhlaseno": r["vyhlaseno"],
        }.items() if v},
        oblasti_meta=oblasti_meta(r), procesni_meta=procesni(r["vyroky"]),
    )


def parse_text(html):
    """Text rozhodnutí z GetText.aspx: forma („NÁLEZ") a obsah."""
    soup = BeautifulSoup(html or "", "html.parser")
    obsah = soup.select_one("td.DocContent")
    if obsah is None:
        return ""
    forma = soup.select_one("#lblDecisionForm")
    text = radky_html(obsah.decode_contents())
    hlavicka = " ".join(forma.get_text(" ", strip=True).split()) if forma else ""
    return (hlavicka + "\n\n" + text).strip() if hlavicka else text


class US:
    soud = "us"

    def __init__(self, session_factory=requests.Session):
        self._session_factory = session_factory

    def _hledani(self, s, od, do):
        r = s.get(HLEDANI, headers=HLAVICKY, timeout=30)
        r.raise_for_status()
        form, pole = formular_pole(r.text, "form#aspnetForm", (POLE + "but_search", "Vyhledat"))
        if form is None:
            raise RuntimeError("NALUS: na stránce není vyhledávací formulář")
        nastav(pole, POLE + "availableFrom", us_datum(od))
        nastav(pole, POLE + "availableTo", us_datum(do))
        nastav(pole, POLE + "razeni", RAZENI_ZPRISTUPNENI)
        nastav(pole, POLE + "resultsPageSize", str(NA_STRANKU))
        akce = urljoin(getattr(r, "url", "") or HLEDANI, form.get("action") or "")
        r = s.post(akce or HLEDANI, data=pole, timeout=60,
                   headers=dict(HLAVICKY, Referer=HLEDANI, Origin=HOST))
        r.raise_for_status()
        return r.text

    def objev(self, od, do):
        """Rozhodnutí zpřístupněná od `od` do `do` (včetně)."""
        s = self._session_factory()
        radky, celkem = parse_vysledky(self._hledani(s, od, do))
        if celkem is None:
            raise RuntimeError("NALUS: odpověď hledání není stránka výsledků")
        strana = 0
        while len(radky) < celkem and strana < MAX_STRAN:
            strana += 1
            r = s.get(f"{VYSLEDKY}?page={strana}", headers=dict(HLAVICKY, Referer=VYSLEDKY),
                      timeout=60)
            r.raise_for_status()
            nove, _ = parse_vysledky(r.text)
            if not nove:
                break
            radky += nove
        self.varovani = []
        if celkem and not radky:
            # Web hlásí výsledky, ale řádky se nepřečetly – změnil se tvar
            # výpisu. Prázdný „úspěšný" běh by se nikdo nedozvěděl.
            raise RuntimeError(f"výpis hlásí {celkem} výsledků, přečteno 0 (změna tvaru stránky?)")
        if len(radky) < celkem:
            print(f"    [us] načteno jen {len(radky)} z {celkem} výsledků")
            self.varovani.append(f"načteno jen {len(radky)} z {celkem} výsledků")
        out = {}
        for r in radky:
            out.setdefault(r["sz"], zaznam_z_radku(r))
        return list(out.values())

    def doplnit(self, z):
        """Výpis nese všechno, detail se nestahuje."""

    def text(self, z):
        sz = z["id"].split(":", 1)[1]
        try:
            r = self._session_factory().get(TEXT.format(sz=sz), headers=HLAVICKY, timeout=60)
            r.raise_for_status()
        except Exception as e:
            print(f"    [us] {z['spz']}: text nedostupný ({type(e).__name__})")
            return {}
        text = parse_text(r.text)
        return {"text": text, "zdroj": "html"} if len(text) >= MIN_TEXT else {}
