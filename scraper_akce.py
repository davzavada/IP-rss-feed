#!/usr/bin/env python3
"""Kalendář akcí – vzdělávací akce pořadatelů z akce_config.json.

Semináře, webináře a konference ČAK, PF UK, Jednoty českých právníků,
Beck-seminářů, epravo.cz, ALAI a ÚPV v jednom kalendáři. Výstup jde do
docs/akce.json (čte ho Kalendář akcí na webu) a docs/akce.ics (odběr
v kalendáři).

Každý pořadatel má jiný web a žádný z nich nezveřejňuje akce ve společném
formátu, proto se akce z výpisu berou postupně třemi cestami a první, která
něco vrátí, vyhrává:

  1. vlastní parser (`parser` v configu, PARSERY níže) – pro weby, kde se
     vyplatí psát ho podle uložené stránky (tests/fixtures/akce/),
  2. strojově čitelná data na stránce: odkaz na iCal (.ics, webcal:) nebo
     schema.org Event v JSON-LD,
  3. AI z textu stránky (odkazy zůstanou v textu, ať AI vrátí i adresu
     přihlášky). Stejná záloha jako u přehledů jednání, jen tady je hlavní
     cestou, dokud vlastní parser chybí.

U nových akcí se pak (v rozpočtu AKCE_MAX_DETAILU) stáhne i jejich stránka
a doplní se, co výpis neuvádí: anotace, lektoři, cena, místo. Nakonec AI
každou akci zařadí do 1–3 oblastí ze stejného seznamu jako judikaturu
(docs/data/oblasti.json); zařazení se drží u akce a znovu se ptá, jen když
se změní název nebo anotace.

Když se výpis pořadatele nepodaří stáhnout ani přečíst, jeho akce zůstanou,
jak byly. Proběhlé akce se drží PROSLE_DNI dní (měsíční mřížka je ukazuje
tlumeně), pak vypadnou.

Lokálně bez přístupu na weby pořadatelů:
    python scraper_akce.py --local CAK=stranka.html UPV=akce.ics
    SKIP_GEMINI=1 python scraper_akce.py --jen CAK,UPV
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from feed_common import (
    USER_AGENT, gemini_enabled, gemini_generate_raw, load_json, save_json,
)
from judikatura.taxonomie import Taxonomie
from scraper_hearings import (
    VTIMEZONE, extract_json, ics_escape, ics_fold, site_host, strip_diacritics,
)

CONFIG_FILE = "akce_config.json"
OUTPUT_FILE = "docs/akce.json"
ICS_FILE = "docs/akce.ics"
PRAHA = ZoneInfo("Europe/Prague")

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
}

# Proběhlé akce se drží měsíc a něco (mřížka „3 týdny" jde i zpátky), pak
# vypadnou. Akce dál než rok dopředu jsou nejspíš chyba čtení data.
PROSLE_DNI = 45
DOPREDU_DNI = 400
# Kolik stránek akcí (detailů) se za běh stáhne a nechá přečíst AI. Dělí se
# rovným dílem mezi pořadatele; po prvních nocích zbývají jen nové akce.
AKCE_MAX_DETAILU = int(os.environ.get("AKCE_MAX_DETAILU", "80"))
# Kolik akcí jde do jednoho dotazu na oblasti.
OBLASTI_DAVKA = 25
# Kolik textu stránky dostane AI (výpisy mívají dlouhé menu a patičku).
MAX_TEXT = 60000
# Když AI z výpisu vrátí míň než tuhle část dosud známých budoucích akcí
# pořadatele, bere se to jako výpadek čtení, ne jako zrušené akce.
MIN_POMER = 0.5
# Doména v UID událostí v akce.ics je jen identifikátor a nesmí se měnit
# s doménou webu (SITE_HOST) – kalendáře by jinak viděly každou akci dvakrát.
ICS_UID_HOST = "owl.davidzavada.cz"

FORMY = {"prezencne": "Prezenčně", "online": "Online", "hybridne": "Hybridně"}

DATE_CZ_RE = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})")
DATE_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})")


# --- Pomocné ---------------------------------------------------------------

def http_get(url, timeout=60):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r


def cisty(s):
    return " ".join(str(s or "").split())


def norm_datum(s):
    """„2026-10-01", „2026-10-01T09:00+02:00", „1. 10. 2026" -> „2026-10-01"."""
    s = str(s or "")
    m = DATE_ISO_RE.search(s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = DATE_CZ_RE.search(s)
        if not m:
            return None
        d, mo, y = (int(x) for x in m.groups())
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def norm_cas(s):
    """„9:00", „09.00", „2026-10-01T09:00:00" -> „09:00"; bez času ""."""
    s = str(s or "")
    if "T" in s:
        s = s.split("T", 1)[1]
    m = TIME_RE.search(s)
    if not m:
        return ""
    h, mi = int(m.group(1)), int(m.group(2))
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else ""


def iso_na_prahu(s):
    """ISO datum a čas s pásmem („…+01:00", „…Z") převede na pražský čas.
    Vrací (datum, čas); bez pásma nebo bez času nechá, jak je."""
    s = str(s or "").strip()
    if "T" not in s:
        return norm_datum(s), ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return norm_datum(s), norm_cas(s)
    if dt.tzinfo:
        dt = dt.astimezone(PRAHA)
    return dt.date().isoformat(), f"{dt.hour:02d}:{dt.minute:02d}"


def norm_forma(s, misto=""):
    """Forma akce z volného textu: prezencne / online / hybridne."""
    t = strip_diacritics(f"{s or ''} {misto or ''}").casefold()
    if "hybrid" in t or "mixed" in t:
        return "hybridne"
    if re.search(r"online|webinar|on-line|virtual|zoom|stream|e-learning", t):
        # „online i prezenčně", „prezenčně nebo online" = hybridně
        if re.search(r"prezen|offline|osobne", t):
            return "hybridne"
        return "online"
    if t.strip():
        return "prezencne"
    return ""


def povoleny_odkaz(url, hosty):
    """Odkaz na akci jen https/http na doméně pořadatele (config `hosty`)."""
    try:
        p = urlparse(url or "")
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return p.scheme in ("http", "https") and any(
        host == h or host.endswith("." + h) for h in hosty)


def klic_nazvu(nazev):
    return re.sub(r"[^a-z0-9]+", " ", strip_diacritics(nazev or "").casefold()).strip()


def akce_id(org, datum, nazev):
    """Stálé id akce: pořadatel, den a název (bez času – ten pořadatel
    rád upřesní později). Slouží i jako UID v akce.ics."""
    return hashlib.sha1(f"{org}|{datum}|{klic_nazvu(nazev)}".encode("utf-8")).hexdigest()[:20]


def obsah_klic(a):
    """Otisk toho, podle čeho AI řadí do oblastí – když se nezmění, zařazení
    se nepřepočítává."""
    return hashlib.sha1(f"{a.get('nazev')}|{a.get('anotace')}".encode("utf-8")).hexdigest()[:12]


# Předpony, kterými pořadatelé v názvu oznamují formu („HYBRIDNÍ FORMA:",
# „ONLINE:"). Forma je ve vlastním poli, v názvu jen překáží.
PREDPONA_FORMY_RE = re.compile(
    r"^\s*(?:HYBRIDN[ÍI]\s+FORMA|ONLINE(?:\s+(?:FORMA|SEMIN[ÁA][ŘR]))?|WEBIN[ÁA][ŘR]"
    r"|PREZEN[ČC]N[ĚE](?:\s+FORMA)?)\s*[:–-]\s*",
    re.IGNORECASE)


def normalizuj(raw, org, cfg, zaklad_url):
    """Syrový záznam (z parseru, JSON-LD, iCal nebo AI) na akci pro web.
    Vrací None, když chybí název nebo datum."""
    nazev = cisty(raw.get("nazev"))
    datum = norm_datum(raw.get("datum"))
    if not nazev or not datum:
        return None
    predpona = PREDPONA_FORMY_RE.match(nazev)
    if predpona and len(nazev) > predpona.end() + 3:
        raw = dict(raw, forma=raw.get("forma") or norm_forma(predpona.group(0)))
        nazev = nazev[predpona.end():]
    url = raw.get("url") or ""
    url = urljoin(zaklad_url, url) if url else ""
    if not povoleny_odkaz(url, cfg.get("hosty", [])):
        url = ""
    misto = cisty(raw.get("misto"))
    forma = raw.get("forma") if raw.get("forma") in FORMY else norm_forma(raw.get("forma"), misto)
    datum_do = norm_datum(raw.get("datum_do"))
    lektori = raw.get("lektori") or []
    if isinstance(lektori, str):
        lektori = [x for x in re.split(r"\s*[;\n]\s*", lektori)]
    lektori = [cisty(x) for x in lektori if cisty(x)][:8]
    a = {
        "id": akce_id(org, datum, nazev),
        "poradatel": org,
        "datum": datum,
        "zacatek": norm_cas(raw.get("zacatek")),
        "konec": norm_cas(raw.get("konec")),
        "nazev": nazev,
        "misto": misto,
        "forma": forma,
        "lektori": lektori,
        "cena": cisty(raw.get("cena")),
        "anotace": cisty(raw.get("anotace"))[:600],
        "url": url,
    }
    if datum_do and datum_do > datum:
        a["datum_do"] = datum_do
    return a


# --- Strojově čitelná data na stránce ----------------------------------------

def _typy(obj):
    t = obj.get("@type")
    return t if isinstance(t, list) else [t]


def _jsonld_objekty(data):
    """Projde JSON-LD (seznamy, @graph, ItemList) a vrátí všechny Event."""
    if isinstance(data, list):
        for x in data:
            yield from _jsonld_objekty(x)
    elif isinstance(data, dict):
        if any(isinstance(t, str) and t.endswith("Event") for t in _typy(data)):
            yield data
            return
        for k in ("@graph", "itemListElement", "item", "event", "events", "subEvent"):
            if k in data:
                yield from _jsonld_objekty(data[k])


def _jmena(x):
    if isinstance(x, list):
        return [j for y in x for j in _jmena(y)]
    if isinstance(x, dict):
        return [cisty(x.get("name"))] if x.get("name") else []
    return [cisty(x)] if x else []


def z_jsonld(html):
    """Akce ze schema.org Event v <script type="application/ld+json">."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for sc in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
        try:
            data = json.loads(sc.string or sc.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for ev in _jsonld_objekty(data):
            d, t = iso_na_prahu(ev.get("startDate"))
            d_do, t_do = iso_na_prahu(ev.get("endDate"))
            loc = ev.get("location")
            locs = loc if isinstance(loc, list) else [loc]
            mista, online = [], False
            for l in locs:
                if isinstance(l, dict):
                    if "Virtual" in str(l.get("@type")):
                        online = True
                        continue
                    adr = l.get("address")
                    if isinstance(adr, dict):
                        adr = ", ".join(cisty(adr.get(k)) for k in
                                        ("streetAddress", "addressLocality") if adr.get(k))
                    mista.append(", ".join(x for x in (cisty(l.get("name")), cisty(adr)) if x))
                elif l:
                    mista.append(cisty(l))
            rezim = str(ev.get("eventAttendanceMode") or "")
            forma = ("hybridne" if "Mixed" in rezim else "online" if "Online" in rezim
                     else "prezencne" if "Offline" in rezim else "")
            if not forma:
                forma = ("hybridne" if online and mista else "online" if online
                         else "prezencne" if mista else "")
            offers = ev.get("offers")
            offers = offers if isinstance(offers, list) else [offers] if offers else []
            cena = ""
            for o in offers:
                if isinstance(o, dict) and o.get("price") not in (None, ""):
                    p = str(o["price"])
                    cena = "zdarma" if p in ("0", "0.0", "0.00") else \
                        f"{p} {'Kč' if o.get('priceCurrency') in (None, 'CZK') else o['priceCurrency']}"
                    break
            out.append({
                "nazev": ev.get("name"), "datum": d, "zacatek": t,
                "datum_do": d_do if d_do != d else None, "konec": t_do,
                "url": ev.get("url"), "misto": "; ".join(x for x in mista if x),
                "forma": forma, "lektori": _jmena(ev.get("performer")),
                "cena": cena,
                "anotace": BeautifulSoup(str(ev.get("description") or ""), "html.parser").get_text(" "),
            })
    return out


def ics_odkazy(html, zaklad_url):
    """Odkazy na iCal výpis (.ics, webcal:, ?ical=1) ze stránky."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("webcal:"):
            href = "https:" + href[len("webcal:"):]
        if re.search(r"\.ics([?#]|$)|[?&](ical|ics)=", href, re.I):
            u = urljoin(zaklad_url, href)
            if u not in out:
                out.append(u)
    return out


def _ics_radky(text):
    """Rozbalí pokračovací řádky (RFC 5545) a vrátí [(název, parametry, hodnota)]."""
    radky = []
    for ln in text.replace("\r\n", "\n").split("\n"):
        if ln.startswith((" ", "\t")) and radky:
            radky[-1] += ln[1:]
        elif ln:
            radky.append(ln)
    out = []
    for ln in radky:
        hlava, _, hodnota = ln.partition(":")
        jmeno, *param = hlava.split(";")
        out.append((jmeno.upper(), {p.partition("=")[0].upper(): p.partition("=")[2]
                                    for p in param}, hodnota))
    return out


def _ics_text(s):
    return (s.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",")
            .replace("\\;", ";").replace("\\\\", "\\"))


def _ics_cas(hodnota, param):
    """DTSTART/DTEND -> (datum ISO, čas) v pražském čase."""
    v = hodnota.strip()
    if param.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", v):
        return f"{v[:4]}-{v[4:6]}-{v[6:8]}", ""
    m = re.fullmatch(r"(\d{8})T(\d{2})(\d{2})\d{0,2}(Z?)", v)
    if not m:
        return None, ""
    dt = datetime(int(m.group(1)[:4]), int(m.group(1)[4:6]), int(m.group(1)[6:]),
                  int(m.group(2)), int(m.group(3)))
    if m.group(4):
        dt = dt.replace(tzinfo=timezone.utc).astimezone(PRAHA)
    elif param.get("TZID"):
        try:
            dt = dt.replace(tzinfo=ZoneInfo(param["TZID"])).astimezone(PRAHA)
        except Exception:
            pass
    return dt.date().isoformat(), f"{dt.hour:02d}:{dt.minute:02d}"


def z_ics(text):
    """Akce z iCalendar souboru."""
    out, ev = [], None
    for jmeno, param, hodnota in _ics_radky(text):
        if jmeno == "BEGIN" and hodnota.upper() == "VEVENT":
            ev = {}
        elif jmeno == "END" and hodnota.upper() == "VEVENT" and ev is not None:
            out.append(ev)
            ev = None
        elif ev is not None:
            if jmeno == "DTSTART":
                ev["datum"], ev["zacatek"] = _ics_cas(hodnota, param)
            elif jmeno == "DTEND":
                d, t = _ics_cas(hodnota, param)
                if param.get("VALUE") == "DATE" or (d and not t):
                    # Celodenní DTEND je den PO konci akce.
                    d = (date.fromisoformat(d) - timedelta(days=1)).isoformat() if d else d
                ev["datum_do"], ev["konec"] = d, t
            elif jmeno == "SUMMARY":
                ev["nazev"] = _ics_text(hodnota)
            elif jmeno == "LOCATION":
                ev["misto"] = _ics_text(hodnota)
            elif jmeno == "URL":
                ev["url"] = hodnota.strip()
            elif jmeno == "DESCRIPTION":
                ev["anotace"] = _ics_text(hodnota)
    for ev in out:
        if ev.get("datum_do") == ev.get("datum"):
            ev["datum_do"] = None
        ev["forma"] = norm_forma("", ev.get("misto"))
    return out


# --- AI -------------------------------------------------------------------

def text_stranky(html, zaklad_url):
    """Čitelný text stránky pro AI. Odkazy zůstanou jako „text <adresa>",
    ať AI k akci vrátí i odkaz na ni; menu, patička a skripty jdou pryč."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]):
        tag.decompose()
    for a in soup.find_all("a", href=True):
        href = urljoin(zaklad_url, a["href"])
        if href.startswith("http"):
            a.replace_with(f"{a.get_text(' ', strip=True)} <{href}>")
    text = soup.get_text("\n")
    radky = [cisty(r) for r in text.splitlines()]
    return "\n".join(r for r in radky if r)[:MAX_TEXT]


AKCE_POLE = (
    '{"nazev": "název akce", "datum": "RRRR-MM-DD", "datum_do": "RRRR-MM-DD nebo null", '
    '"zacatek": "HH:MM nebo null", "konec": "HH:MM nebo null", '
    '"url": "odkaz na stránku akce nebo přihlášku, přesně jak je v textu v <…>", '
    '"misto": "adresa nebo sál, u online akce \\"online\\"", '
    '"forma": "prezencne | online | hybridne", "lektori": ["Jméno Příjmení s tituly"], '
    '"cena": "např. 1 600 Kč, zdarma", "anotace": "jedna až dvě věty, o čem akce je"}'
).replace("{", "{{").replace("}", "}}")   # prompty níže jdou přes str.format

VYPIS_AI_PROMPT = (
    "Toto je text stránky s přehledem vzdělávacích akcí pořadatele {poradatel} "
    "(semináře, webináře, konference, kurzy pro právníky). Dnes je {dnes}. "
    "Vytáhni z něj všechny akce, které se konají dnes nebo později, a odpověz "
    "POUZE platným JSON polem bez dalšího textu, každý prvek ve tvaru:\n"
    + AKCE_POLE + "\n"
    "Když údaj v textu není, dej null (u lektorů prázdný seznam). Rok, který "
    "u data chybí, doplň podle dneška (nejbližší budoucí). Nic si nevymýšlej, "
    "přepisuj z textu; odkazy ber jen z <…>. Ber jen akce s datem konání "
    "(seminář, přednáška, webinář, konference, kurz, kulatý stůl). Vynech "
    "akce, které už proběhly, novinky, články, výzvy a granty, nabídky "
    "studia a výuky pro studenty, uzávěrky přihlášek a nabídky práce."
)

DETAIL_AI_PROMPT = (
    "Toto je text stránky jedné vzdělávací akce pořadatele {poradatel}. "
    "Odpověz POUZE platným JSON objektem bez dalšího textu ve tvaru:\n"
    + AKCE_POLE + "\n"
    "Když údaj v textu není, dej null (u lektorů prázdný seznam). Nic si "
    "nevymýšlej, přepisuj z textu."
)

OBLASTI_AI_PROMPT = (
    "Zařaď vzdělávací akce pro právníky do oblastí práva. Každá akce dostane "
    "jednu až tři oblasti, nejvýš tři, jen ze seznamu (použij id):\n{oblasti}\n\n"
    "Akce, která se žádné oblasti netýká (soft skills, marketing kanceláře), "
    "dostane prázdný seznam. Odpověz POUZE platným JSON objektem bez dalšího "
    "textu ve tvaru {{\"<id akce>\": [\"id oblasti\", …], …}} – klíče jsou id "
    "akcí ze zadání."
)


def z_ai_vypisu(text, org, cfg, dnes):
    if not gemini_enabled() or not text.strip():
        return []
    prompt = VYPIS_AI_PROMPT.format(poradatel=cfg.get("nazev", org), dnes=dnes.isoformat())
    data = extract_json(gemini_generate_raw(prompt, text, max_tokens=16384))
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def z_ai_detailu(text, org, cfg):
    if not gemini_enabled() or not text.strip():
        return None
    prompt = DETAIL_AI_PROMPT.format(poradatel=cfg.get("nazev", org))
    data = extract_json(gemini_generate_raw(prompt, text, max_tokens=4096))
    if isinstance(data, list):
        data = data[0] if data and isinstance(data[0], dict) else None
    return data if isinstance(data, dict) else None


def zarad_oblasti(akce, taxonomie):
    """Doplní `oblasti` akcím, které je nemají nebo se jim změnil obsah.

    Ptá se po dávkách (OBLASTI_DAVKA); akce z dávky, na kterou AI
    neodpověděla, zůstane bez oblasti (`oblasti_klic` se nezapíše) a zkusí
    se příští běh. Neznámá id oblastí se zahodí (Taxonomie.normalizuj)."""
    cekaji = [a for a in akce if a.get("oblasti_klic") != obsah_klic(a)]
    if not cekaji:
        return 0
    if not gemini_enabled():
        print(f"  oblasti: AI vypnutá, {len(cekaji)} akcí zůstává bez zařazení")
        return 0
    prompt = OBLASTI_AI_PROMPT.format(oblasti=taxonomie.do_promptu())
    hotovo = 0
    for i in range(0, len(cekaji), OBLASTI_DAVKA):
        davka = cekaji[i:i + OBLASTI_DAVKA]
        text = "\n".join(
            f"- id {a['id']}: {a['nazev']}" + (f" – {a['anotace']}" if a.get("anotace") else "")
            for a in davka)
        data = extract_json(gemini_generate_raw(prompt, text, max_tokens=4096))
        if not isinstance(data, dict):
            print(f"  oblasti: dávka {i // OBLASTI_DAVKA + 1} bez odpovědi AI, zkusím příště")
            continue
        for a in davka:
            if a["id"] not in data:
                continue
            hodnota = data[a["id"]]
            a["oblasti"] = taxonomie.normalizuj(hodnota if isinstance(hodnota, list) else [])
            a["oblasti_klic"] = obsah_klic(a)
            hotovo += 1
    print(f"  oblasti: zařazeno {hotovo} z {len(cekaji)} akcí")
    return hotovo


# --- Vlastní parsery ------------------------------------------------------

# Jméno z configu (`parser`) -> funkce(html, url) vracející syrové záznamy
# ve stejném tvaru jako AI. Přidávají se podle stránek uložených sondou
# (python tools/probe_zdroje.py --soudy akce) do tests/fixtures/akce/.
def parser_cak(html, zaklad_url):
    """Výpis ČAK je tabulka: název | „30.09.2026 (10:00 - 14:00)" nebo
    „05.10.2026-14.12.2026 (…)" | místo; každá buňka odkazuje na /akce/N."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.select("tr"):
        bunky = tr.find_all("td")
        if len(bunky) < 3:
            continue
        a = bunky[0].find("a", href=True)
        kdy = cisty(bunky[1].get_text(" "))
        data = DATE_CZ_RE.findall(kdy)
        if not a or not data:
            continue
        casy = TIME_RE.findall(kdy.split("(", 1)[1]) if "(" in kdy else []
        misto = cisty(bunky[2].get_text(" "))
        out.append({
            "nazev": a.get_text(" "),
            "datum": "{}. {}. {}".format(*data[0]),
            "datum_do": "{}. {}. {}".format(*data[1]) if len(data) > 1 else None,
            "zacatek": ":".join(casy[0]) if casy else "",
            "konec": ":".join(casy[1]) if len(casy) > 1 else "",
            "url": urljoin(zaklad_url, a["href"]),
            "misto": misto,
            "forma": norm_forma(a.get_text(" "), misto),
        })
    return out


PARSERY = {"cak": parser_cak}


# --- Stažení a sloučení -----------------------------------------------------

def nacti_poradatele(org, cfg, dnes, local=None):
    """Stáhne výpisy pořadatele a vrátí (akce, zdroj_dat), nebo (None, chyba),
    když se nepodařilo nic stáhnout ani přečíst."""
    zdroje = [(local, None)] if local else [(None, u) for u in cfg.get("stranky", [])]
    syrove, cesta, chyby = [], None, []
    for soubor, url in zdroje:
        try:
            if soubor:
                with open(soubor, "rb") as f:
                    obsah = f.read().decode("utf-8", "replace")
                zaklad = (cfg.get("stranky") or [""])[0]
                print(f"  [{org}] lokální soubor: {soubor}")
            else:
                obsah = http_get(url).text
                zaklad = url
        except (requests.RequestException, OSError) as e:
            chyby.append(str(e))
            print(f"  [{org}] stažení selhalo: {e}")
            continue

        if "BEGIN:VCALENDAR" in obsah[:2000]:
            syrove += z_ics(obsah)
            cesta = "ical"
            continue
        if cfg.get("parser") in PARSERY:
            nalez = PARSERY[cfg["parser"]](obsah, zaklad)
            if nalez:
                syrove += nalez
                cesta = "parser"
                continue
        nalez = []
        for u in (ics_odkazy(obsah, zaklad)[:2] if not soubor else []):
            if not povoleny_odkaz(u, cfg.get("hosty", [])):
                continue
            try:
                ics = http_get(u).text
            except requests.RequestException:
                continue
            nalez = z_ics(ics) if "BEGIN:VCALENDAR" in ics[:2000] else []
            if nalez:
                cesta = "ical"
                break
        if not nalez:
            nalez = z_jsonld(obsah)
            if nalez:
                cesta = cesta or "jsonld"
        if not nalez:
            nalez = z_ai_vypisu(text_stranky(obsah, zaklad), org, cfg, dnes)
            if nalez:
                cesta = cesta or "ai"
        syrove += nalez

    if not syrove and chyby and len(chyby) == len(zdroje):
        return None, "; ".join(chyby)
    if not syrove and not gemini_enabled():
        return None, "stránka nemá strojově čitelné akce a AI je vypnutá"

    hranice_od = (dnes - timedelta(days=PROSLE_DNI)).isoformat()
    hranice_do = (dnes + timedelta(days=DOPREDU_DNI)).isoformat()
    akce, videne = [], set()
    zaklad = (cfg.get("stranky") or [""])[0]
    for raw in syrove:
        a = normalizuj(raw, org, cfg, zaklad)
        if not a or not (hranice_od <= a["datum"] <= hranice_do) or a["id"] in videne:
            continue
        videne.add(a["id"])
        akce.append(a)
    print(f"  [{org}] akcí: {len(akce)}" + (f" (cesta: {cesta})" if cesta else ""))
    return akce, cesta


def _pdf_text(data):
    from pypdf import PdfReader
    import io
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((pg.extract_text() or "") for pg in reader.pages[:6])[:MAX_TEXT]


def odkaz_pozvanky(html, url, hosty):
    """Odkaz na pozvánku (PDF) ze stránky akce – ČAK na stránce uvádí jen
    místo a čas, lektory, program a cenu má až v pozvánce."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        popis = strip_diacritics(a.get_text(" ", strip=True)).casefold()
        if "pozvank" in popis or ("program" in popis and ".pdf" in popis):
            odkaz = urljoin(url, a["href"])
            if povoleny_odkaz(odkaz, hosty):
                return odkaz
    return None


def text_detailu(url, hosty=()):
    """(html, text) stránky akce. U PDF (pozvánky ÚPV) je html prázdné
    a text je z PDF; když stránka odkazuje na pozvánku, přidá se k textu
    i ta."""
    r = http_get(url)
    if r.content[:5] == b"%PDF-" or "pdf" in r.headers.get("Content-Type", "").lower():
        return "", _pdf_text(r.content)
    text = text_stranky(r.text, url)
    pozvanka = odkaz_pozvanky(r.text, url, hosty)
    if pozvanka:
        try:
            p = http_get(pozvanka)
            if p.content[:5] == b"%PDF-":
                text += "\n\n--- POZVÁNKA ---\n" + _pdf_text(p.content)
        except Exception as e:   # bez pozvánky zůstane text stránky
            print(f"    pozvánka {pozvanka}: {e}")
    return r.text, text[:MAX_TEXT]


# Kolikrát se stránka akce zkusí přečíst. Na co nezbude rozpočet, přijde
# na řadu další noc (počet pokusů se drží u akce v akce.json).
DETAIL_POKUSU = 2


def potrebuje_detail(a):
    return (a.get("url") and a.get("detail_pokusy", 0) < DETAIL_POKUSU
            and not (a.get("anotace") and a.get("lektori") and a.get("cena")))


def dopln_detaily(akce, config, rozpocet):
    """Akcím, kterým ve výpisu chybí anotace, lektoři nebo cena, stáhne
    stránku a doplní je (JSON-LD, jinak AI). Vrací zbytek rozpočtu.

    `rozpocet` je podíl pořadatele na AKCE_MAX_DETAILU (viz main); akce se
    berou od nejbližších, ty jsou pro kalendář nejdůležitější."""
    doplneno = 0
    for a in sorted(akce, key=lambda x: x["datum"]):
        if rozpocet <= 0:
            break
        if not potrebuje_detail(a):
            continue
        cfg = config["poradatele"][a["poradatel"]]
        rozpocet -= 1
        a["detail_pokusy"] = a.get("detail_pokusy", 0) + 1
        try:
            html, text = text_detailu(a["url"], cfg.get("hosty", []))
        except Exception as e:   # síť i rozbité PDF – akce zůstane z výpisu
            print(f"    detail {a['url']}: {e}")
            continue
        kandidati = z_jsonld(html) if html else []
        raw = next((k for k in kandidati if norm_datum(k.get("datum")) == a["datum"]), None)
        if raw is None:
            raw = z_ai_detailu(text, a["poradatel"], cfg)
        if not raw:
            continue
        doplneno += 1
        for pole in ("zacatek", "konec", "misto", "cena", "anotace"):
            if not a.get(pole) and raw.get(pole):
                a[pole] = norm_cas(raw[pole]) if pole in ("zacatek", "konec") else cisty(raw[pole])
        a["anotace"] = a.get("anotace", "")[:600]
        if not a.get("lektori") and raw.get("lektori"):
            lk = raw["lektori"]
            a["lektori"] = [cisty(x) for x in (lk if isinstance(lk, list) else [lk]) if cisty(x)][:8]
        if not a.get("forma"):
            a["forma"] = (raw.get("forma") if raw.get("forma") in FORMY
                          else norm_forma(raw.get("forma"), a.get("misto")))
    if doplneno:
        print(f"  [{akce[0]['poradatel']}] ze stránek akcí doplněno: {doplneno}")
    return rozpocet


def sloucit(stare, org, nove, cesta, dnes):
    """Akce pořadatele: nové z výpisu + to, co se z minulých běhů drží.

    - Proběhlé akce zůstávají do PROSLE_DNI (výpisy je obvykle stáhnou).
    - Budoucí akce, která z výpisu zmizela, pořadatel zrušil nebo stáhl –
      vypadne. Kromě případu, kdy výpis četla AI a vrátila výrazně míň akcí
      než minule (MIN_POMER): to je spíš výpadek čtení, tak se staré nechají.
    - Údaje, které nové čtení nemá (lektor, cena, oblasti…), se převezmou
      ze staré verze téže akce.
    """
    dnes_iso = dnes.isoformat()
    moje = [a for a in stare if a.get("poradatel") == org]
    podle_id = {a["id"]: a for a in moje}
    budouci_stare = [a for a in moje if a.get("datum", "") >= dnes_iso]
    budouci_nove = [a for a in nove if a["datum"] >= dnes_iso]
    podezrele = (cesta == "ai" and budouci_stare
                 and len(budouci_nove) < len(budouci_stare) * MIN_POMER)
    if podezrele:
        print(f"  [{org}] POZOR: AI přečetla {len(budouci_nove)} budoucích akcí, "
              f"minule jich bylo {len(budouci_stare)} – nechávám i ty staré")

    vysledek = {}
    for a in moje:
        if a.get("datum", "") < dnes_iso or podezrele:
            vysledek[a["id"]] = a
    for a in nove:
        b = podle_id.get(a["id"])
        if b:
            for k, v in b.items():
                if k not in a or a[k] in ("", [], None):
                    a[k] = v
        vysledek[a["id"]] = a
    return list(vysledek.values())


def prorezat(akce, dnes):
    hranice = (dnes - timedelta(days=PROSLE_DNI)).isoformat()
    return [a for a in akce if (a.get("datum_do") or a.get("datum") or "") >= hranice]


# --- iCalendar export -----------------------------------------------------

def write_ics(output, path=None):
    """Akce jako iCalendar pro odběr v kalendáři (stejně jako hearings.ics)."""
    path = path or ICS_FILE
    porad = output.get("poradatele", {})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//IP-rss-feed//Kalendar akci//CS",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "X-WR-CALNAME:Owl – vzdělávací akce",
        "X-WR-TIMEZONE:Europe/Prague",
        "X-WR-CALDESC:Semináře\\, webináře a konference pro právníky.",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H", "X-PUBLISHED-TTL:PT12H",
    ] + VTIMEZONE
    for a in output.get("akce", []):
        ymd = a["datum"].replace("-", "")
        lines += ["BEGIN:VEVENT", f"UID:akce-{a['id']}@{ICS_UID_HOST}", f"DTSTAMP:{stamp}"]
        # Dlouhý kurz (přes týden) jde jako jedna událost v den začátku –
        # celodenní blok přes celý semestr by kalendář zabral.
        rozpeti = ((date.fromisoformat(a["datum_do"]) - date.fromisoformat(a["datum"])).days
                   if a.get("datum_do") else 0)
        if rozpeti > 7:
            desc_do = f"Do {date.fromisoformat(a['datum_do']):%-d. %-m. %Y}"
        else:
            desc_do = ""
        if a.get("zacatek") and (not a.get("datum_do") or rozpeti > 7):
            zac = a["zacatek"].replace(":", "")
            kon = (a.get("konec") or "").replace(":", "")
            if not kon or kon <= zac:
                h = datetime(2000, 1, 1, int(zac[:2]), int(zac[2:])) + timedelta(hours=1)
                kon = f"{h:%H%M}" if h.day == 1 else "2359"
            lines += [f"DTSTART;TZID=Europe/Prague:{ymd}T{zac}00",
                      f"DTEND;TZID=Europe/Prague:{ymd}T{kon}00"]
        else:
            konec = date.fromisoformat(a["datum"] if rozpeti > 7 else (a.get("datum_do") or a["datum"])) + timedelta(days=1)
            lines += [f"DTSTART;VALUE=DATE:{ymd}",
                      f"DTEND;VALUE=DATE:{konec.isoformat().replace('-', '')}"]
        p = porad.get(a.get("poradatel"), {})
        desc = [f"Pořadatel: {p.get('nazev', a.get('poradatel'))}"] + ([desc_do] if desc_do else [])
        if a.get("forma"):
            desc.append(f"Forma: {FORMY[a['forma']]}")
        if a.get("lektori"):
            desc.append("Lektoři: " + ", ".join(a["lektori"]))
        if a.get("cena"):
            desc.append(f"Cena: {a['cena']}")
        if a.get("anotace"):
            desc.append("")
            desc.append(a["anotace"])
        odkaz = a.get("url") or (p.get("stranky") or [""])[0]
        if odkaz:
            desc.append("")
            desc.append(f"Přihláška: {odkaz}")
        lines += [
            "SUMMARY:" + ics_escape(f"{p.get('zkratka', a.get('poradatel'))}: {a['nazev']}"),
            "DESCRIPTION:" + ics_escape("\n".join(desc)),
        ]
        if a.get("misto"):
            lines.append("LOCATION:" + ics_escape(a["misto"]))
        if odkaz:
            lines.append("URL:" + odkaz)
        lines += ["STATUS:CONFIRMED", "TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(ics_fold(x) for x in lines) + "\r\n")
    return f"https://{site_host()}/{os.path.basename(path)}"


# --- Hlavní běh -----------------------------------------------------------

def parse_kv(pairs):
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"Čekám POŘADATEL=cesta, dostal jsem: {p}")
        k, v = p.split("=", 1)
        out[k.upper()] = v
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--local", nargs="*", metavar="POŘADATEL=CESTA",
                    help="místo stahování přečíst uloženou stránku nebo .ics")
    ap.add_argument("--jen", default="", help="jen tito pořadatelé (CAK,UPV…)")
    args = ap.parse_args()
    local = parse_kv(args.local)

    config = load_json(CONFIG_FILE)
    if not config.get("poradatele"):
        sys.exit(f"Chybí {CONFIG_FILE} – bez pořadatelů nemá běh smysl.")
    jen = {x.strip().upper() for x in args.jen.split(",") if x.strip()} or set(local)

    dnes = datetime.now(PRAHA).date()
    output = load_json(OUTPUT_FILE)
    akce = [a for a in output.get("akce", []) if isinstance(a, dict) and a.get("id")]
    stav = output.get("poradatele", {})
    rozpocet = AKCE_MAX_DETAILU
    # Každý pořadatel dostane stejný díl rozpočtu (nevyčerpaný přechází na
    # další), ať jeden dlouhý výpis nespotřebuje všechno.
    zbyva_poradatelu = len([o for o in config["poradatele"] if not jen or o in jen])
    ok = 0

    print("Výpisy akcí…")
    for org, cfg in config["poradatele"].items():
        if jen and org not in jen:
            continue
        nove, cesta = nacti_poradatele(org, cfg, dnes, local.get(org))
        meta = stav.setdefault(org, {})
        if nove is None:
            meta["chyba"] = cesta
            zbyva_poradatelu -= 1
            continue
        # Sloučit napřed: nová verze akce tak převezme, co už se o ní ví
        # (lektoři, cena, počet pokusů o stránku), a znovu se nestahuje.
        moje = sloucit(akce, org, nove, cesta, dnes)
        akce = [a for a in akce if a.get("poradatel") != org] + moje
        podil = rozpocet // max(zbyva_poradatelu, 1)
        zbyva_poradatelu -= 1
        if org not in local:   # lokální běh je test čtení, ne stahování
            nadchazejici = [a for a in moje if (a.get("datum_do") or a["datum"]) >= dnes.isoformat()]
            rozpocet -= podil - dopln_detaily(nadchazejici, config, podil)
        meta.update({"stazeno": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "cesta": cesta, "chyba": None})
        ok += 1

    akce = prorezat(akce, dnes)
    zarad_oblasti(akce, Taxonomie())
    akce.sort(key=lambda a: (a["datum"], a.get("zacatek") or "", a["poradatel"], a["nazev"]))

    if not ok and not akce:
        sys.exit("Nepodařilo se získat žádné akce.")

    # Pořadatelé v pořadí z configu (= pořadí štítků na webu); stav běhu
    # (kdy a jak se výpis četl, chyba) jde s nimi.
    output = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "poradatele": {
            org: {"nazev": cfg["nazev"], "zkratka": cfg["zkratka"], "barva": cfg.get("barva"),
                  "url": (cfg.get("stranky") or [None])[0],
                  "pocet": sum(1 for a in akce if a["poradatel"] == org and a["datum"] >= dnes.isoformat()),
                  **{k: stav.get(org, {}).get(k) for k in ("stazeno", "cesta", "chyba")}}
            for org, cfg in config["poradatele"].items()
        },
        "formy": FORMY,
        "akce": akce,
    }
    output["ics"] = write_ics(output)
    save_json(OUTPUT_FILE, output)
    print(f"Hotovo: {len(akce)} akcí -> {OUTPUT_FILE}, {ICS_FILE}")


if __name__ == "__main__":
    main()
