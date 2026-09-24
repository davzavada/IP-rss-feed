"""Nejvyšší správní soud: vyhledávač vyhledavac.nssoud.cz.

Vyhledávač je formulář ASP.NET s antiforgery tokenem (cookie + skryté pole):
  1. GET úvodní stránky dá cookie, token a všechna pole formuláře;
  2. POST s podmínkou „Datum zpřístupnění" od–do vrátí první stránku
     výsledků (40 řádků) a parametry hledání vypíše do skriptu stránky;
  3. další řádky (po 20) vrací POST na /Home/MyResTRowsCont s těmito
     parametry a číslem stránky – tak je dočítá prohlížeč při posouvání;
     prázdná odpověď znamená konec.
„Do" je půlnoc, zadává se tedy den po posledním hledaném dni.

Vyhledávač drží i rozhodnutí krajských soudů – bereme jen senáty NSS.
Detail (DokumentDetail/Index/{id}) má úřední údaje: oblast úpravy, typ
řízení, výrok, soudce zpravodaje, aplikované předpisy, napadený správní
orgán a datum zpřístupnění. Text je na DokumentOriginal/Text/{id} (prostý
text v UTF-16), čitelná podoba na DokumentOriginal/Html/{id} a originál
v PDF na DokumentOriginal/Index/{id}.

NSS znovu zpřístupňuje i starší dokumenty, když je opraví nebo doplní.
Rozhodnutí vydané víc než rok před zpřístupněním proto nebereme jako
novinku: zveřejnění se mu nastaví na datum rozhodnutí, takže do okna webu
nepadne (orchestr ho zapíše jen do archivu).

Tvar stránek odpovídá odpovědím, které stáhla sonda (tests/fixtures/nss/).
"""

import json
import re
import time
from datetime import date, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from judikatura import mapy, model
from judikatura.soudy.ns import pdf_text
from judikatura.soudy.web import (HLAVICKY, cz_datum, dekoduj, formular_pole, jmeno, nastav,
                                  radky_html)

HOST = "https://vyhledavac.nssoud.cz"
DETAIL = HOST + "/DokumentDetail/Index/{id}"
TEXT = HOST + "/DokumentOriginal/Text/{id}"
HTML = HOST + "/DokumentOriginal/Html/{id}"
ORIGINAL = HOST + "/DokumentOriginal/Index/{id}"
POLE_ZPRISTUPNENI = "aktualizovano"   # technický název pole „Datum zpřístupnění"
MAX_STRAN = 150      # pojistka: 40 + 150 × 20 řádků je víc, než NSS vydá za měsíc
MIN_TEXT = 500       # usnesení o nepřijatelnosti bývají krátká, ale ne tolik
ZNAK_TEXTU = "správní soud"   # text rozhodnutí, ne chybová stránka (malými písmeny)
STARE_DNI = 365      # starší rozhodnutí zpřístupněné znovu není novinka

# Senáty NSS ve výběru „Soud (senát)". Ostatní položky jsou krajské soudy
# (i jejich pobočky) a kárné soudy obecného soudnictví.
SENATY_BEZ_ZKRATKY = {"kárný senát", "zvláštní senát podle z. č. 131/2002 Sb."}

# Výroky bez věcného závěru: odmítnutí (lhůta, vady, nepřijatelnost),
# zastavení, odkladný účinek, námitka podjatosti.
PROCESNI_VYROKY = ("odmítnuto", "zastaveno", "řízení:", "odkladný účinek", "nepodjatý", "podjatý")


def je_nss(senat):
    senat = " ".join((senat or "").split())
    return "NSS" in senat.split() or senat in SENATY_BEZ_ZKRATKY


def cislo_jednaci(text):
    """„21 Afs    31/2026 -   32" -> „21 Afs 31/2026-32"."""
    s = " ".join((text or "").replace("\xa0", " ").split())
    s = re.sub(r"\s*/\s*", "/", s)
    return re.sub(r"\s*-\s*(\d+)$", r"-\1", s)


def procesni(vyrok):
    vyrok = (vyrok or "").strip().lower()
    return bool(vyrok) and vyrok.startswith(PROCESNI_VYROKY)


def nss_datum(d):
    return d.strftime("%d.%m.%Y")


# --- stránky ---

def parse_radky(html):
    """Řádky výsledků (první stránka i dočtené řádky) -> slovníky."""
    soup = BeautifulSoup(html or "", "html.parser")
    out = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        idx = next((i for i, td in enumerate(tds) if td.select_one("input[name$='.ID']")), None)
        if idx is None or len(tds) < idx + 9:
            continue
        id_ = tds[idx].select_one("input[name$='.ID']").get("value", "").strip()
        if not id_.isdigit():
            continue
        bunky = [" ".join(td.get_text(" ", strip=True).replace("\xa0", " ").split())
                 for td in tds[idx + 1:idx + 9]]
        out.append({
            "id": id_, "datum": cz_datum(bunky[0]), "cj": cislo_jednaci(bunky[1]),
            "senat": bunky[2], "druh": bunky[3].lower(), "vyrok": bunky[4],
            "ucastnici": bunky[7],
        })
    return out


def pocet_vysledku(html):
    m = re.search(r"Počet nalezených záznamů:\s*(\d+)", html or "")
    return int(m.group(1)) if m else None


def parametry_dalsich(html):
    """Adresa a parametry, se kterými stránka dočítá další řádky."""
    url = re.search(r"var moreRowsUrl = '([^']+)'", html or "")
    par = re.search(r"var currParams = '(.*?)';\s*$", html or "", re.M)
    if not url or not par:
        return None
    view = re.search(r"var currViewId = '([^']*)'", html)
    sort = re.search(r"var currSort = '([^']*)'", html)
    return urljoin(HOST, url.group(1)), {
        # Řetězec je v JS s escapy " – json ho vrátí do původní podoby.
        "vyhledavaciPodminky": json.loads('"' + par.group(1) + '"'),
        "zobrazeniVysledkuId": view.group(1) if view else "1",
        "resultOrder": sort.group(1) if sort else "",
    }


def zaznam_z_radku(r):
    senat, rejstrik = model.senat_z_spz(r["cj"])
    return model.novy_zaznam(
        "nss", f"nss:{r['id']}", spz=r["cj"], druh=r["druh"], datum=r["datum"],
        senat=senat, rejstrik=rejstrik, url=HTML.format(id=r["id"]),
        pdf=ORIGINAL.format(id=r["id"]),
        meta={"senat_nss": r["senat"], "vyrok": r["vyrok"], "ucastnici": r["ucastnici"]},
        procesni_meta=procesni(r["vyrok"]),
    )


PREDPIS_POLE = ("cl", "§", "odst", "pism", "predpis", "cislo", "rok")


def _radky_tabulky(soup, predpona, pole):
    """Řádky tabulky detailu jako slovníky {pole: hodnota}. Buňky s údaji
    mají data-field-id = předpona + pole (záhlaví má stejné id, ale jinou
    třídu)."""
    out = []
    for tr in soup.find_all("tr"):
        radek = {}
        for td in tr.find_all("td", recursive=False):
            fid = td.get("data-field-id", "")
            if "det-textval" in (td.get("class") or []) and fid.startswith(predpona) \
                    and fid[len(predpona):] in pole:
                radek[fid[len(predpona):]] = " ".join(td.get_text(" ", strip=True).split())
        if any(radek.values()):
            out.append(radek)
    return out


def _predpis(r):
    """Řádek tabulky předpisů -> „§ 36 odst. 3 zákona č. 500/2004 Sb."."""
    if not r.get("cislo") or not r.get("rok"):
        return ""
    casti = [f"{popis} {r[k]}" for k, popis in (("cl", "čl."), ("§", "§"), ("odst", "odst."),
                                                 ("pism", "písm.")) if r.get(k)]
    return " ".join(casti + [f"{r.get('predpis') or 'předpisu'} č. {r['cislo']}/{r['rok']} Sb."])


def parse_detail(html):
    """Úřední údaje z detailu dokumentu (pole data-field-id)."""
    soup = BeautifulSoup(html or "", "html.parser")
    pole = {}
    for el in soup.select("[data-field-id]"):
        hodnota = el.select_one(".det-textval")
        if hodnota is not None:
            pole.setdefault(el["data-field-id"], " ".join(hodnota.get_text(" ", strip=True).split()))
    predpisy = []
    for r in _radky_tabulky(soup, "aplikovanepravnipredpisysb", PREDPIS_POLE):
        p = _predpis(r)
        if p and p not in predpisy:
            predpisy.append(p)
    organy = []
    for r in _radky_tabulky(soup, "nazevspravnihoorganu", ("",)):
        if r[""] not in organy:
            organy.append(r[""])
    return {
        "cj": cislo_jednaci(pole.get("cj", "")),
        "datum": cz_datum(pole.get("datumvydanirozhodnuti", "")),
        "zverejneno": cz_datum(pole.get("aktualizovano", "")),
        "druh": pole.get("druhdokumentuavyrokrozhodnuti", "").lower(),
        "ecli": pole.get("ecli", ""),
        "meta": {
            "senat_nss": pole.get("soudsenat", ""),
            "soudce": jmeno(pole.get("soudcezpravodaj", "")),
            "oblast_upravy": pole.get("oblastupravy", ""),
            "typ_rizeni": pole.get("typrizeni", ""),
            "vyrok": pole.get("vyrokrozhodnuti", ""),
            "ucastnici": pole.get("ucastnicirizeniz", ""),
            "spravni_organ": "; ".join(o for o in organy if o),
            "predpisy": "; ".join(predpisy),
        },
    }


def oblasti_meta(meta):
    """Oblast úpravy, pak napadený orgán a účastníci, pak předpisy."""
    return mapy.spoj(mapy.z_oblasti_upravy(meta.get("oblast_upravy")),
                     mapy.z_organu([meta.get("spravni_organ"), meta.get("ucastnici")]),
                     mapy.z_predpisu((meta.get("predpisy") or "").split("; ")))


def text_z_txt(obsah):
    """Prostý text (DokumentOriginal/Text) – HTML s <br> v UTF-16."""
    s = dekoduj(obsah).replace("\x00", "").replace("﻿", "")
    s = re.sub(r"(?is)<head.*?</head>", "", s)
    s = radky_html(s)
    return re.sub(r"(?m)^\[OBRÁZEK\]\n?", "", s).strip()


def text_z_html(obsah):
    """Čitelná podoba (DokumentOriginal/Html, z Wordu přes Aspose) – odstavce
    poskládané z úseků, bez záhlaví a zápatí stránek."""
    soup = BeautifulSoup(dekoduj(obsah), "html.parser")
    for smeti in soup(["head", "script", "style"]):
        smeti.decompose()
    for el in soup.select("[style*=headerfooter-type]"):
        el.decompose()
    radky = [" ".join(p.get_text("", strip=False).split()) for p in soup.find_all(["p", "li"])]
    text = "\n".join(r for r in radky if r and r != "[OBRÁZEK]")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class NSS:
    soud = "nss"
    pauza = 0.3   # mezi detaily a dočítáním stránek (testy ji nulují)

    def __init__(self, session_factory=requests.Session):
        self._session_factory = session_factory
        self._relace = None   # relace z hledání – detaily a texty jdou s jejími cookies

    def _s(self):
        if self._relace is None:
            self._relace = self._session_factory()
        return self._relace

    # --- hledání ---

    def _hledani(self, s, od, do):
        """První stránka výsledků pro zpřístupnění od `od` do `do` včetně."""
        r = s.get(HOST + "/", headers=HLAVICKY, timeout=30)
        r.raise_for_status()
        form, pole = formular_pole(r.text, "form#findform", ("btSubmit", ""))
        if form is None:
            raise RuntimeError("NSS: na úvodní stránce není vyhledávací formulář")
        predpona = next((n[:-len("TechnickyNazev")] for n, v in pole
                         if n.endswith("vyhledavaciPodminkaHodnota[0].TechnickyNazev")
                         and v == POLE_ZPRISTUPNENI), None)
        if not predpona:
            raise RuntimeError("NSS: ve formuláři chybí pole Datum zpřístupnění")
        nastav(pole, predpona + "HodnotaDatumACasOd", nss_datum(od))
        nastav(pole, predpona + "HodnotaDatumACasDo", nss_datum(do + timedelta(days=1)))
        akce = urljoin(getattr(r, "url", "") or HOST + "/", form.get("action") or "")
        r = s.post(akce or HOST + "/", data=pole, timeout=60,
                   headers=dict(HLAVICKY, Referer=HOST + "/", Origin=HOST))
        r.raise_for_status()
        if pocet_vysledku(r.text) is None:
            raise RuntimeError("NSS: odpověď hledání není stránka výsledků")
        return r.text

    def objev(self, od, do):
        """Rozhodnutí NSS zpřístupněná od `od` do `do` (včetně)."""
        s = self._relace = self._session_factory()
        html = self._hledani(s, od, do)
        celkem = pocet_vysledku(html)
        radky = parse_radky(html)
        dalsi = parametry_dalsich(html)
        strana = 0
        while dalsi and len(radky) < celkem and strana < MAX_STRAN:
            strana += 1
            time.sleep(self.pauza)
            url, data = dalsi
            r = s.post(url, data=dict(data, pageNum=str(strana)), timeout=60,
                       headers=dict(HLAVICKY, Referer=HOST + "/",
                                    **{"X-Requested-With": "XMLHttpRequest"}))
            r.raise_for_status()
            nove = parse_radky(r.text)
            if not nove:
                break
            radky += nove
        if len(radky) < celkem:
            print(f"    [nss] načteno jen {len(radky)} z {celkem} výsledků")
        videne, out = set(), []
        for r in radky:
            if r["id"] in videne or not je_nss(r["senat"]):
                continue
            videne.add(r["id"])
            out.append(zaznam_z_radku(r))
        return out

    # --- detail a text ---

    def doplnit(self, z):
        """Úřední údaje z detailu: zveřejnění (datum zpřístupnění), ECLI,
        soudce, oblast úpravy, předpisy – a z nich první zařazení."""
        time.sleep(self.pauza)
        id_ = z["id"].split(":", 1)[1]
        r = self._s().get(DETAIL.format(id=id_), headers=HLAVICKY, timeout=30)
        r.raise_for_status()
        d = parse_detail(r.text)
        for k in ("datum", "zverejneno", "druh", "ecli"):
            if d[k]:
                z[k] = d[k]
        if d["cj"] and d["cj"] != z.get("spz"):
            z.update({k: v for k, v in model.novy_zaznam("nss", z["id"], spz=d["cj"]).items()
                      if k in ("spz", "spz_klic", "cast")})
        z["meta"] = {**(z.get("meta") or {}), **{k: v for k, v in d["meta"].items() if v}}
        z["oblasti_meta"] = oblasti_meta(z["meta"])
        if z["meta"].get("vyrok"):
            z["procesni_meta"] = procesni(z["meta"]["vyrok"])
        # Znovu zpřístupněné staré rozhodnutí: zveřejnění = datum rozhodnutí.
        if z.get("datum") and z.get("zverejneno") and (
                date.fromisoformat(z["zverejneno"]) - date.fromisoformat(z["datum"])
                > timedelta(days=STARE_DNI)):
            z["meta"]["zpristupneno"] = z["zverejneno"]
            z["zverejneno"] = z["datum"]

    def text(self, z):
        """Celý text: prostý text, jinak čitelná podoba, jinak PDF."""
        id_ = z["id"].split(":", 1)[1]
        for url, prevod in ((TEXT.format(id=id_), text_z_txt), (HTML.format(id=id_), text_z_html)):
            try:
                r = self._s().get(url, headers=HLAVICKY, timeout=60)
                r.raise_for_status()
                text = prevod(r.content)
            except Exception as e:
                print(f"    [nss] {z['spz']}: {url.rsplit('/', 2)[-2]} nedostupný ({type(e).__name__})")
                continue
            if len(text) >= MIN_TEXT and ZNAK_TEXTU in text.lower():
                return {"text": text, "zdroj": "html" if prevod is text_z_html else "text"}
        try:
            r = self._s().get(ORIGINAL.format(id=id_), headers=HLAVICKY, timeout=60)
            r.raise_for_status()
        except Exception as e:
            print(f"    [nss] {z['spz']}: originál nedostupný ({type(e).__name__})")
            return {}
        if not r.content.startswith(b"%PDF"):
            return {}
        text = pdf_text(r.content)
        if len(text) >= MIN_TEXT and ZNAK_TEXTU in text.lower():
            return {"text": text, "zdroj": "pdf-text"}
        return {"pdf": r.content, "zdroj": "pdf"}
