"""Záznam rozhodnutí: identifikátory, spisové značky, časy.

Záznam je obyčejný dict s českými klíči (jako hearings.json). Pole, která
vyplňuje adaptér soudu, jsou popsaná u `novy_zaznam`; `ai` a `stav` plní
orchestr. Archiv drží všechno, na web jde jen výřez (sklad.slim).
"""

import re
import unicodedata
from datetime import datetime, timedelta, timezone

from feed_common import OKNO_DNI

SOUDY = ("ns", "nss", "us", "sdeu")
NAZVY_SOUDU = {
    "ns": "Nejvyšší soud",
    "nss": "Nejvyšší správní soud",
    "us": "Ústavní soud",
    "sdeu": "Soudní dvůr EU",
}

# Kolik dní zpět zobrazuje web (podle prvního výskytu): u všech soudů měsíc,
# ať je v okně celý podklad pro měsíční shrnutí. Archiv drží všechno.
OKNA_DNI = {"ns": OKNO_DNI, "nss": OKNO_DNI, "us": OKNO_DNI, "sdeu": OKNO_DNI}


def ted():
    return datetime.now(timezone.utc)


def iso(dt):
    """ISO čas v UTC bez mikrosekund („2026-09-24T05:12:00Z")."""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def z_iso(s):
    """ISO čas nebo datum -> datetime v UTC; None, když to nejde."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def poledne(datum):
    """Datum („2026-09-20") -> datetime v poledne UTC (jako pubDate ve feedech)."""
    dt = z_iso(datum)
    return dt.replace(hour=12, minute=0, second=0) if dt else None


def mesic(dt):
    return dt.strftime("%Y-%m")


def bez_diakritiky(s):
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


# --- Spisové značky ---

# „23 Cdo 418 / 2026", „28 Cdo 3880/2023- II.", „29 NSČR 18/2024"
_SPZ_RE = re.compile(r"^\s*(\d+)\s*([^\d\s/]+)\s+(\d+)\s*/\s*(\d{2,4})\s*(.*)$")


def normalizuj_spz(spz):
    """Zobrazovací tvar: „23 Cdo 418 / 2026" -> „23 Cdo 418/2026".

    Úřední deska NS píše kolem lomítka mezery, databáze ne; dřív se kvůli
    tomu stejné rozhodnutí vedlo dvakrát."""
    s = re.sub(r"\s+", " ", str(spz or "")).strip()
    s = re.sub(r"\s*/\s*", "/", s)
    return re.sub(r"\s*-\s*([IVX]+\.?)$", r" - \1", s)


def spz_klic(spz):
    """Klíč pro porovnání značek bez ohledu na zápis: („23cdo418/2026", část).

    Část je pořadí dalšího rozhodnutí v téže věci („28 Cdo 3880/2023- II.")."""
    s = bez_diakritiky(normalizuj_spz(spz)).lower()
    cast = ""
    m = re.search(r" - ([ivx]+)\.?$", s)
    if m:
        cast = m.group(1).upper()
        s = s[:m.start()]
    return re.sub(r"\s+", "", s), cast


def senat_z_spz(spz):
    """(číslo senátu, rejstřík) ze značky NS; (None, '') když to nejde."""
    m = _SPZ_RE.match(normalizuj_spz(spz))
    if not m:
        return None, ""
    return int(m.group(1)), m.group(2)


# Předběžné záznamy: rozhodnutí známé z rychlejšího zdroje dřív, než ho
# vydá ten úřední – úřední deska NS před databází, ipcuria.eu před
# oznámením v Úředním věstníku. Úřední záznam je převezme
# (orchestr._prevezmi_predbezne).
PREDBEZNE_ID = {"ns": "ns:deska:", "sdeu": "sdeu:ipc:"}


# --- Záznam ---

def novy_zaznam(soud, id, **pole):
    """Nový záznam s výchozími hodnotami.

    Adaptér vyplní, co ví: spz, ecli, druh (rozsudek, usnesení, nález,
    stanovisko GA, předběžná otázka…), datum (rozhodnuto), zverejneno,
    nazev, url, pdf, jazyk, meta (úřední údaje jako nápověda pro AI),
    oblasti_meta a procesni_meta (první zařazení podle metadat)."""
    z = {
        "id": id, "soud": soud, "spz": "", "spz_klic": "", "cast": "",
        "ecli": "", "druh": "", "senat": None, "rejstrik": "",
        "datum": "", "zverejneno": "", "first_seen": "",
        "nazev": "", "url": "", "pdf": "", "jazyk": "cs",
        "meta": {}, "oblasti_meta": [], "procesni_meta": False,
        "ai": None, "stav": {"pokusy": 0, "dalsi_pokus": None, "duvod": None},
        "nahrazeno": None, "bootstrap": False,
    }
    z.update(pole)
    if z["spz"]:
        z["spz"] = normalizuj_spz(z["spz"])
        z["spz_klic"], z["cast"] = spz_klic(z["spz"])
    return z


# Pole, která nové objevení smí přepsat (odkazy a metadata se u soudů
# doplňují s odstupem – PDF třeba až za pár dní). AI, stav a první výskyt
# zůstávají z archivu.
_AKTUALIZOVATELNE = ("spz", "spz_klic", "cast", "ecli", "druh", "senat", "rejstrik",
                     "datum", "zverejneno", "nazev", "url", "pdf", "jazyk",
                     "oblasti_meta", "procesni_meta")


def sloucit(stary, novy):
    """Doplní do archivního záznamu, co nově přišlo. Vrací True, když se
    něco změnilo."""
    zmena = False
    for k in _AKTUALIZOVATELNE:
        hodnota = novy.get(k)
        if hodnota not in (None, "", []) and stary.get(k) != hodnota:
            stary[k] = hodnota
            zmena = True
    meta = dict(stary.get("meta") or {})
    for k, v in (novy.get("meta") or {}).items():
        if v not in (None, "", []) and meta.get(k) != v:
            meta[k] = v
            zmena = True
    stary["meta"] = meta
    return zmena


def prvni_vyskyt(zverejneno, nyni, bootstrap):
    """Kdy záznam „poprvé přibyl".

    Běžně teď. Když ale objevíme rozhodnutí zveřejněné před víc než třemi dny
    (náběh, vynechané běhy, NSS znovu vystaví starý dokument), bereme datum
    zveřejnění – jinak by se staré věci tvářily jako novinky."""
    dt = poledne(zverejneno)
    if dt and (bootstrap or dt < nyni - timedelta(days=3)):
        return min(dt, nyni)
    return nyni
