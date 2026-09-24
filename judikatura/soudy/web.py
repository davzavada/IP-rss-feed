"""Společné pomůcky adaptérů: formuláře ASP.NET, data, text z HTML.

Weby NSS i ÚS hledají přes formuláře s antiforgery tokenem nebo
viewstate – posílá se proto celý formulář tak, jak by ho odeslal
prohlížeč, a mění se jen pole, o která jde.
"""

import html as html_mod
import re

from bs4 import BeautifulSoup

from feed_common import USER_AGENT

HLAVICKY = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
}


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


def cz_datum(text):
    """„20. 5. 2026" i „20.05.2026 11:46" -> „2026-05-20"; '' když to nejde."""
    m = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})", text or "")
    if not m:
        return ""
    den, mes, rok = (int(x) for x in m.groups())
    return f"{rok:04d}-{mes:02d}-{den:02d}"


def jmeno(prijmeni_napred):
    """„CAMRDA Jakub" / „Bartoň Michal" -> „Jakub Camrda" / „Michal Bartoň".

    NSS píše příjmení velkými písmeny (i dvojí), ÚS příjmení první a jméno
    poslední."""
    casti = (prijmeni_napred or "").split()
    if len(casti) < 2:
        return " ".join(casti)
    velke = [c for c in casti if c.isupper() and len(c) > 1]
    if velke and casti[:len(velke)] == velke and len(velke) < len(casti):
        prijmeni, krestni = casti[:len(velke)], casti[len(velke):]
        return " ".join(krestni + [p.capitalize() for p in prijmeni])
    return " ".join(casti[-1:] + casti[:-1])


def radky_html(fragment):
    """Text z kousku HTML: <br> a konce odstavců jako nové řádky, bez značek."""
    s = re.sub(r"(?i)<br\s*/?>|</p\s*>|</div\s*>|</tr\s*>|</h\d\s*>", "\n", fragment or "")
    s = html_mod.unescape(re.sub(r"<[^>]+>", "", s))
    radky = [" ".join(r.split()) for r in s.replace("\xa0", " ").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(radky)).strip()


def dekoduj(obsah, kodovani=None):
    """Tělo odpovědi jako text – NSS posílá prostý text v UTF-16 bez BOM."""
    if obsah[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return obsah.decode("utf-16", errors="replace")
    if len(obsah) > 1 and obsah[1:2] == b"\x00" and obsah[:1] != b"\x00":
        return obsah.decode("utf-16-le", errors="replace")
    return obsah.decode(kodovani or "utf-8", errors="replace")
