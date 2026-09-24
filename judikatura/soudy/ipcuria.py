"""ipcuria.eu – doplňkový zdroj: nové předběžné otázky k duševnímu
vlastnictví a ochraně údajů hned po podání.

Oznámení o nové věci vychází v Úředním věstníku (a v Cellaru) dva až tři
měsíce po podání. ipcuria.eu vede seznam dosud nerozhodnutých předběžných
otázek z IP a ochrany údajů (all_referrals.php) s datem podání, názvem věci
a zařazením do kategorií; nová věc tam bývá do dvou týdnů. Z něj se berou
otázky podané za poslední měsíc – jako záznam `sdeu:ipc:{věc}`, který
později nahradí oznámení z ÚV (viz orchestr._prevezmi_predbezne).

Stránka věci (case?reference=…) nese položené otázky, jakmile je autor webu
doplní; do té doby na ní stojí „questions are not yet available".

Tvar stránek odpovídá tomu, co stáhla sonda (tests/fixtures/sdeu/ipcuria_*).
"""

import re
from datetime import datetime

from bs4 import BeautifulSoup

from judikatura import mapy, model
from judikatura.soudy.web import radky_html

HOST = "https://ipcuria.eu"
SEZNAM = HOST + "/all_referrals.php"
VEC = HOST + "/case?reference={vec}"
# Odkaz pro čtenáře: věc na webu Soudního dvora (dokumenty, jak přibývají).
CURIA = "https://curia.europa.eu/juris/liste.jsf?num={vec}&language=cs"
PREFIX = model.PREDBEZNE_ID["sdeu"]
# Otázky u věci zatím nejsou – takovou stránku nemá smysl shrnovat.
BEZ_OTAZEK = "questions are not yet available"

_VEC_RE = re.compile(r"^C-(\d+)/(\d{2})$")
_PODANO_RE = re.compile(r"lodged on\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})")


def celex_oznameni(vec):
    """CELEX oznámení o věci v ÚV: „C-1009/26" -> „62026CN1009"; '' jinak."""
    m = _VEC_RE.match((vec or "").strip())
    return f"620{m.group(2)}CN{int(m.group(1)):04d}" if m else ""


def _datum(text):
    try:
        return datetime.strptime(" ".join(text.split()), "%d %b %Y").date().isoformat()
    except ValueError:
        return ""


def parse_seznam(html):
    """Seznam otázek: [{vec, nazev, podano, kategorie}] v pořadí stránky.

    Bloky věcí odděluje <hr>; věc, která je na stránce dvakrát, se spojí
    (kategorie z obou výskytů)."""
    out = {}
    for blok in re.split(r"<hr\s*/?>", html or "", flags=re.I):
        soup = BeautifulSoup(blok, "html.parser")
        odkaz = soup.find("a", href=re.compile(r"case\?reference="))
        if odkaz is None:
            continue
        vec = odkaz.get_text(strip=True)
        podano = _PODANO_RE.search(soup.get_text(" "))
        if not _VEC_RE.match(vec) or not podano:
            continue
        nazev = soup.find("i")
        kategorie = [" > ".join(a.get_text(" ", strip=True) for a in span.find_all("a"))
                     for span in soup.select("span.breadcrumbs")]
        r = out.setdefault(vec, {"vec": vec, "nazev": nazev.get_text(" ", strip=True) if nazev else "",
                                 "podano": _datum(podano.group(1)), "kategorie": []})
        for k in kategorie:
            k = " ".join(k.split())
            if k and k not in r["kategorie"]:
                r["kategorie"].append(k)
    return list(out.values())


def oblasti(kategorie):
    """Oblasti podle kategorií ipcuria; věc zatím bez kategorie dostane
    hlavní oblasti IP a ochrany údajů – ať se ve výběru IP/IT ukáže, než ji
    zařadí AI."""
    return mapy.z_ipcurie(kategorie) or mapy.spoj(mapy.nacti("ipcuria")["bez_kategorie"])


def text_vec(html):
    """Text stránky věci (položené otázky); '' když otázky ještě nejsou."""
    soup = BeautifulSoup(html or "", "html.parser")
    for smeti in soup(["head", "script", "style", "noscript"]):
        smeti.decompose()
    for smeti in soup.select("#nav, .breadcrumbs"):
        smeti.decompose()
    text = radky_html((soup.body or soup).decode_contents())
    return "" if BEZ_OTAZEK in text.lower() else text
