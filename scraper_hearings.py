#!/usr/bin/env python3
"""Kalendář jednání MSPH a VS Praha – filtr na duševní vlastnictví.

Oba soudy zveřejňují přehledy nařízených jednání jako dokumenty (MSPH .docx,
VS .pdf) na portálu justice. Tenhle scraper je stáhne, vytáhne z nich
jednotlivá jednání a nechá si ta, která patří do agendy duševního
vlastnictví. Výstup jde do docs/hearings.json, který čte kalendář na webu;
ostatní jednání se zahodí, ať se veřejně nerozepisují účastníci
nesouvisejících sporů. Účastníky, kteří jsou fyzická osoba, drží archiv
jen pod iniciálami – kdo je fyzická osoba, rozhoduje AI (viz redact_osoby).

MSPH vedle civilního úseku zveřejňuje zvlášť (jako další dokument) i přehled
úseku správního soudnictví; z něj se berou žaloby proti Úřadu průmyslového
vlastnictví. Tam senát nic neříká – správní oddělení soudí všechnu správní
agendu – a rozhoduje žalovaný: jednání s ÚPV mezi účastníky je IP, ať je
v kterémkoli senátu a úseku kteréhokoli z obou soudů.

Které senáty jsou IP se bere z rozvrhů práce obou soudů. Rozvrhy se často
mění, proto je v hearings_config.json uložený aktuální seznam senátů a soudců
a scraper ho umí obnovit: stáhne rozvrh (PDF o stovkách stran), najde stránky
o duševním vlastnictví a nechá AI vytáhnout senáty a předsedy.
Když AI extrakce selže, zůstává v platnosti poslední známý seznam.

V civilním úseku jde filtr primárně přes senát ze spisové značky (např.
„12 C" je na MSPH IP, ale „12 Co" je odvolací neIP agenda) – samotné jméno
soudce nestačí, protože titíž soudci soudí i neIP rejstříky (EPR, ICm…).
Jméno předsedy se používá jen u rejstříku Nc, kde číslo senátu specializaci
nerozlišuje.

Parsování přehledů je deterministické (tabulka v .docx, regex nad textem
.pdf); AI nastupuje jako záloha, kdyby soud změnil formát dokumentu.

Lokální testování bez přístupu k msp.gov.cz:
    python scraper_hearings.py --local-jednani MS=cesta.docx \
                               MS:spravni=cesta.docx VS=cesta.pdf \
                               --local-rozvrh VS=rozvrh.pdf
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
import unicodedata
import zipfile
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from feed_common import (
    USER_AGENT, gemini_enabled, gemini_generate_raw, load_json, save_json,
)

CONFIG_FILE = "hearings_config.json"
OUTPUT_FILE = "docs/hearings.json"

HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8"}

# Po kolika dnech zkusit obnovit IP senáty z rozvrhu práce. Proběhlá jednání
# se z hearings.json nemažou – kalendář slouží i jako archiv.
ROZVRH_REFRESH_DAYS = 7
# Kolik řádků z dokumentu se musí naparsovat, aby se výsledek bral jako úplný.
MIN_PARSE_RATIO = 0.8

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# Spisová značka: „91Co 99/2026", „3 Cmo 85/2025", „12 C 7/2026"…
SPZ_RE = re.compile(r"(\d+)\s*([A-Za-z]+)\s+(\d+)\s*/\s*(\d{4})")
DATE_RE = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})")
TIME_RE = re.compile(r"\d{1,2}:\d{2}")

# Tituly, které se při porovnávání jmen soudců zahazují.
TITLES_RE = re.compile(
    r"\b(?:JUDr|Mgr|Bc|Ing|PhDr|MUDr|RNDr|Dr|doc|prof|et|Ph\.?D|LL\.?M|MBA|DiS|CSc)\b\.?",
    re.IGNORECASE,
)


def normalize_judge(name):
    """„JUDr. Mgr. Petr Košík, Ph.D." -> „petr košík" (pro porovnávání)."""
    s = TITLES_RE.sub(" ", name or "")
    s = re.sub(r"[.,;()]", " ", s)
    return re.sub(r"\s+", " ", s).strip().casefold()


def strip_diacritics(s):
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def norm_rejstrik(rejstrik):
    """Rejstřík s velkým prvním písmenem a NEDOTČENÝM zbytkem.

    Pozor na str.capitalize(): ta zbytek převede na malá písmena, takže by
    z „EC" udělala „Ec" a z „ECm" „Ecm" – klíč by se pak rozešel s rozvrhem
    práce (a InfoSoud by dostal druhVec=Ec místo EC).
    """
    r = (rejstrik or "").strip()
    return r[:1].upper() + r[1:]


def senat_key(cislo, rejstrik):
    """Klíč senátu: číslo + rejstřík („12 C", „3 Cmo", „12 ECm")."""
    return f"{int(cislo)} {norm_rejstrik(rejstrik)}"


# --- Stažení dokumentů z portálu justice ---

def http_get(url, timeout=60):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r


def find_document_links(page_url):
    """Vrátí [(url, text)] odkazů na dokumenty (.doc/.docx/.pdf, /documents/)
    z článku na portálu justice."""
    html = http_get(page_url).text
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"])
        text = " ".join(a.get_text(" ", strip=True).split())
        low = strip_diacritics((href + " " + text)).lower()
        if ("/documents/" in href.lower()
                or re.search(r"\.(docx?|pdf)([?#]|$)", href.lower())):
            links.append((href, text, low))
    return links


def pick_link(links, keywords, prefer=(), strict=False):
    """Vybere odkaz na dokument podle klíčových slov v URL/textu (bez
    diakritiky). Slova v `prefer` se zkoušejí JEDNO PO DRUHÉM v pořadí, jak
    jsou zapsaná – stránka rozvrhu nese vedle úplného znění i jednotlivé
    změny a při společném průchodu by rozhodovalo jen pořadí v DOM.

    Když nic nesedí, vrátí první dokument na stránce – ne však při `strict`.
    Ten platí u soudu s víc přehledy (MSPH: civilní a správní úsek na jedné
    stránce): obecná slova („prehled", „jednani") ani první odkaz úsek
    nerozliší a místo chybějícího správního dokumentu by se stáhl civilní
    a jeho jednání by se zapsala pod cizí úsek. Tam se bere jen shoda
    s `prefer` (a když žádné není, s `keywords`), jinak nic."""
    passes = [(p,) for p in prefer]
    if not strict or not passes:
        passes.append(tuple(keywords))
    for kws in passes:
        for href, text, low in links:
            if any(k in low for k in kws):
                return href, text
    if links and not strict:
        return links[0][0], links[0][1]
    return None, None


# --- Parsování přehledu jednání (MSPH .docx) ---

def docx_tables(data):
    """Z .docx vytáhne tabulky jako seznam řádků (řádek = seznam buněk,
    buňka = seznam odstavců) + celý prostý text dokumentu."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        xml = zf.read("word/document.xml")
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml)

    def cell_paragraphs(tc):
        out = []
        for p in tc.iter(W_NS + "p"):
            txt = "".join(t.text or "" for t in p.iter(W_NS + "t")).strip()
            if txt:
                out.append(txt)
        return out

    tables = []
    for tbl in root.iter(W_NS + "tbl"):
        rows = []
        for tr in tbl.findall(W_NS + "tr"):
            rows.append([cell_paragraphs(tc) for tc in tr.findall(W_NS + "tc")])
        tables.append(rows)
    text = " ".join(t.text or "" for t in root.iter(W_NS + "t"))
    return tables, text


def parse_jednani_docx(data):
    """Přehled MSPH: tabulka se sloupci Datum / Jednací síň / Předseda senátu /
    Spisová značka / Hodina / Jména účastníků."""
    tables, text = docx_tables(data)
    period = parse_period(text)
    items = []
    for rows in tables:
        for row in rows:
            cells = ["\n".join(c) for c in row]
            if len(cells) < 5 or not DATE_RE.match(cells[0].strip()):
                continue
            m = SPZ_RE.search(cells[3])
            if not m:
                continue
            m_time = TIME_RE.search(cells[4])
            items.append(make_item(
                datum=cells[0].strip(),
                sin=cells[1].strip(),
                predseda=" ".join(cells[2].split()),
                spz=m,
                hodina=m_time.group(0) if m_time else "",
                ucastnici=[u for u in (row[5] if len(row) > 5 else []) if u.strip()],
            ))
    return items, period


# --- Parsování přehledu jednání (VS .pdf) ---

def pdf_text(data):
    """Text PDF po řádcích. Řádky, které ve sloupci „Jména účastníků" jen
    pokračují zalomeným jménem, se slepí s předchozím (viz
    zalomene_ucastniky) – jinak by každý kus jména byl samostatná strana."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    stranky = []
    for page in reader.pages:
        kusy = []

        def visitor(text, cm, tm, font, size):
            if text and text.strip():
                kusy.append((tm[4], tm[5], text))

        text = page.extract_text(visitor_text=visitor) or ""
        stranky.append(spoj_zalomene(text, zalomene_ucastniky(kusy)))
    return "\n".join(stranky)


# Svislá mezera (pt) mezi řádky ve sloupci účastníků, pod kterou jde o
# zalomení téhož jména. VS sází každého účastníka do vlastního odstavce:
# uvnitř jména jsou řádky od sebe ~12,4 pt, mezi účastníky ~20,4 pt.
# Pravý okraj sloupce ani právní forma na konci řádku to neřeknou spolehlivě
# („Honební společenstvo / Zákupy-Brenná" končí daleko před okrajem, „Marek
# Vitásek" a „TnG-Air Servis s.r.o." jsou dvě strany).
ZALOMENI_PT = 16


def zalomene_ucastniky(kusy):
    """Z kusů textu jedné stránky (x, y, text) vrátí dvojice (řádek,
    pokračování) ve sloupci „Jména účastníků": řádek, který leží těsně pod
    předchozím (mezera pod ZALOMENI_PT), je zalomený kus téhož jména.

    Sloupec začíná tam, kde v hlavičce stránky stojí „Jména účastníků";
    bez hlavičky se nic neslepuje (dosavadní chování)."""
    x_sloupce = next((x for x, _, t in kusy if t.strip().startswith("Jména")), None)
    if x_sloupce is None:
        return set()
    radky = {}
    for x, y, t in kusy:
        if x >= x_sloupce - 2:
            radky.setdefault(round(y, 1), []).append((x, t))
    serazene = [(y, " ".join(" ".join(t for _, t in sorted(kus)).split()))
                for y, kus in sorted(radky.items(), reverse=True)]
    dvojice = set()
    for (y1, a), (y2, b) in zip(serazene, serazene[1:]):
        if a and b and 0 < y1 - y2 < ZALOMENI_PT:
            dvojice.add((a, b))
    return dvojice


def spoj_zalomene(text, dvojice):
    """Slepí řádek textu s předchozím, když spolu tvoří dvojici (řádek,
    pokračování) ze zalomene_ucastniky. Předchozí řádek může být i celý
    záznam („… 09:00 CHMIELNICKI-MLYN" + „LIMITED") – porovnává se konec."""
    if not dvojice:
        return text
    # Mezery se porovnávají pryč: pypdf skládá řádek z kusů jinak, než je
    # tu skládáme z pozic („Zákupy-Brenná" vs. „Zákupy - Brenná").
    bez_mezer = lambda t: re.sub(r"\s+", "", t)
    pokracovani = {}
    for a, b in dvojice:
        pokracovani.setdefault(bez_mezer(b), set()).add(bez_mezer(a))
    out = []
    for ln in text.splitlines():
        klic = bez_mezer(ln)
        if out and klic in pokracovani:
            predchozi = bez_mezer(out[-1])
            if any(predchozi.endswith(a) for a in pokracovani[klic]):
                out[-1] = " ".join(out[-1].split()) + " " + " ".join(ln.split())
                continue
        out.append(ln)
    return "\n".join(out)


PDF_SKIP_RE = re.compile(
    r"^(Datum\b|síň\b|Údaje jsou platné|Stav není|Aktuální stav|Přehled$|"
    r"zasedání senátů|Jednací\b|Předseda senátu\b|\d+\s*$)"
)


def parse_jednani_pdf(data):
    return parse_jednani_text(pdf_text(data))


def parse_jednani_text(text):
    """Přehled VS: text po řádcích; záznam začíná datem, spisová značka
    s hodinou ho dělí na hlavičku (síň, předseda) a účastníky. Účastníci
    můžou pokračovat na dalších řádcích až do dalšího data.

    Oddělené od načtení PDF, ať se dá parsování testovat na textu."""
    period = parse_period(text)
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not PDF_SKIP_RE.match(ln)]

    # Rozsekat na záznamy podle řádků začínajících datem.
    records, cur = [], None
    for ln in lines:
        if DATE_RE.match(ln):
            if cur:
                records.append(cur)
            cur = [ln]
        elif cur:
            cur.append(ln)
    if cur:
        records.append(cur)

    items = []
    for rec in records:
        joined = "\n".join(rec)
        m = re.search(
            r"^(\d{2}\.\d{2}\.\d{4})\s+(\S+)\s+(.*?)\s*"
            r"(\d+)\s*([A-Za-z]+)\s+(\d+)\s*/\s*(\d{4})\s+(\d{1,2}:\d{2})\s*(.*)$",
            joined, re.DOTALL,
        )
        if not m:
            continue
        datum, sin, predseda = m.group(1), m.group(2), " ".join(m.group(3).split())
        spz = SPZ_RE.search(f"{m.group(4)}{m.group(5)} {m.group(6)}/{m.group(7)}")
        items.append(make_item(
            datum, sin, predseda, spz, m.group(8), m.group(9).splitlines()))
    return items, period


def parse_period(text):
    """„v období od 16.08.2026 do 31.08.2026" -> (iso_od, iso_do).

    Snese i pomlčku nebo „až" místo „do" („období 16. 9. 2026 – 30. 9.
    2026"); neplatné nebo obrácené období je None."""
    m = re.search(
        r"(?:\bod|obdob\w*)\s+(?:od\s+)?(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})\s*"
        r"(?:do|až|-|–)\s*(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})",
        text,
    )
    if not m:
        return None
    od, do = (czech_date_to_iso(x) for x in m.groups())
    if not od or not do or od > do:
        return None
    return od, do


def obdobi_z_odkazu(url):
    """Záloha, když hlavička dokumentu období neuvádí v podobě, jakou zná
    parse_period: MSPH dává období do adresy dokumentu
    („…/spravni-usek-16-30-9-2026" -> 16.–30. 9. 2026). Bere se jen přesně
    tahle koncovka a jen platné období, jinak None (VS má stálou adresu)."""
    m = re.search(r"-(\d{1,2})-(\d{1,2})-(\d{1,2})-(\d{4})/?$", url or "")
    if not m:
        return None
    d1, d2, mes, rok = (int(x) for x in m.groups())
    try:
        od, do = date(rok, mes, d1), date(rok, mes, d2)
    except ValueError:
        return None
    return (od.isoformat(), do.isoformat()) if od <= do else None


def czech_date_to_iso(s):
    m = DATE_RE.search(s or "")
    if not m:
        return None
    d, mo, y = (int(x) for x in m.groups())
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def merge_participant_lines(lines):
    """Slepí zalomené/oddělené kusy jmen účastníků.

    Pokračováním předchozího jména je řádek začínající malým písmenem
    („s r.o."), spojkou, nebo známým titulem či právní formou psanou velkými
    písmeny („Ph.D.", „MBA", „GmbH", „LIMITED") – ty soudy sázejí do vlastního
    odstavce a bez slepení by se v kalendáři objevily jako samostatné strany
    sporu.

    Zalomení bez rozpoznatelné přípony („Honební společenstvo / Zákupy-
    Brenná") se odsud poznat nedá a rozlepené by dalo špatný popisek sporu
    (kus jména jako protistrana). U PDF VS ho proto slepí už pdf_text podle
    svislé mezery řádků. Pravidlo „řádek končící právní formou patří
    k předchozímu" sem nepatří: slepilo by dvě samostatné strany („Marek
    Vitásek" + „TnG-Air Servis s.r.o.") a osoba by se schovala do firmy."""
    out = []
    for ln in lines:
        ln = " ".join(str(ln).split())
        if not ln:
            continue
        cont = ln[0].islower() or ln.startswith(("&", "-")) or CONT_RE.match(ln)
        if out and cont:
            out[-1] += " " + ln
        else:
            out.append(ln)
    return out


# Fragmenty, které patří k předchozímu jménu, i když začínají velkým písmenem.
# Řádek se slepí, jen když se z těchhle kousků skládá CELÝ („Ph.D. MBA" ano,
# „MBA Consulting" ne – to je samostatná firma).
CONT_RE = re.compile(
    r"^(?:(?:Ph\.?\s?D\.?|CSc\.?|DrSc\.?|LL\.?\s?M\.?|M\.?B\.?A\.?|MSc\.?|DiS\.?"
    r"|GmbH|AG|SE|KG|LIMITED|Ltd\.?|LLC|Inc\.?|N\.V\.|B\.V\.|S\.[A-Z]\.[A-Z]?\.?"
    r"|a\.?\s?s\.?|s\.?\s?r\.?\s?o\.?|z\.?\s?[sú]\.?)[\s,]*)+$",
    re.IGNORECASE,
)

# Právní formy a tituly, které se z názvu strany pro krátký popisek odřezávají.
# Před formou musí být mezera nebo čárka – jinak by se (s IGNORECASE) chytala
# i koncovka obyčejného slova: „House" -> „Hou" (SE), „Atlas" -> „Atl" (a.s.).
FORM_RE = re.compile(
    r"(?:(?:\s*,\s*|\s+)(?:spol\.\s*s\s*r\.?\s*o\.?|s\.?\s*r\.?\s*o\.?|a\.?\s*s\.?|k\.?\s*s\.?"
    r"|v\.?\s*o\.?\s*s\.?|z\.?\s*s\.?|z\.?\s*ú\.?|o\.?\s*p\.?\s*s\.?|s\.?\s*p\.?"
    r"|GmbH|AG|SE|KG|LIMITED|Ltd\.?|LLC|Inc\.?|N\.V\.|B\.V\.|S\.L\.U\.|Corp\.?"
    r"|v\s+likvidaci|příspěvková\s+organizace|státní\s+podnik))+\s*$",
    re.IGNORECASE,
)
LEAD_TITLE_RE = re.compile(
    r"^(?:(?:JUDr|Mgr|Bc|Ing|MgA|PhDr|MUDr|RNDr|PaedDr|Dr|doc|prof|arch|art)\.?\s+)+",
    re.IGNORECASE,
)

# Akademické tituly za jménem („Jan Babák CSc.", „Jana Puhlovská Ph.D.") –
# při zkracování na iniciály se odseknou, ať se nepočítají za další slovo
# jména.
TRAILING_DEGREE_RE = re.compile(
    r"\s+(?:CSc\.?|DrSc\.?|Ph\.?\s?D\.?|LL\.?\s?M\.?|M\.?B\.?A\.?|MSc\.?|DiS\.?"
    r"|MJUr\.?|MPA\.?)$",
    re.IGNORECASE,
)


MAX_PARTY_LEN = 32

# Úřady, které přehled uvádí plným názvem, ale na štítku v kalendáři stačí
# zkratka („Xiaomi v. ÚPV"). Klíč bez diakritiky a malými písmeny; sedí
# i na název s dovětkem („… ČR") a na skloněný tvar.
PARTY_ABBREV = {
    "urad prumysloveho vlastnictvi": "ÚPV",
    "uradu prumysloveho vlastnictvi": "ÚPV",
}


def short_party(name):
    """Zkrátí název strany pro popisek v kalendáři: „OSA z.s." -> „OSA",
    „Ing. Tomáš Seidl" -> „Tomáš Seidl", „Úřad průmyslového vlastnictví"
    -> „ÚPV".

    Kolektivní správci vystupují pod dlouhým názvem z rejstříku („INTERGRAM
    nezávislá společnost umělců a…"); ten se zkrátí na úvodní zkratku, a když
    žádná není, ořízne se na hranici slova.
    """
    s = LEAD_TITLE_RE.sub("", " ".join(str(name).split()))
    s = FORM_RE.sub("", s).strip(" ,-–") or " ".join(str(name).split())
    klic = strip_diacritics(s).casefold()
    for plny, zkratka in PARTY_ABBREV.items():
        if klic.startswith(plny) and not klic[len(plny):][:1].isalnum():
            return zkratka
    if len(s) <= MAX_PARTY_LEN:
        return s
    first = s.split()[0]
    if len(first) >= 2 and first.isupper() and first.isalpha():
        return first
    short = ""
    for word in s.split():
        if len(short) + len(word) + 1 > MAX_PARTY_LEN:
            break
        short = f"{short} {word}".strip()
    return (short or s[:MAX_PARTY_LEN]) + "…"


def initials(name):
    """Zkrátí jméno fyzické osoby na iniciály: „Ing. Tomáš Seidl" ->
    „T. S." Titul před jménem i akademický titul za jménem se nejdřív
    odříznou, ať se nepočítají za slovo jména."""
    s = LEAD_TITLE_RE.sub("", " ".join(str(name).split()))
    while True:
        zkraceno = TRAILING_DEGREE_RE.sub("", s)
        if zkraceno == s:
            break
        s = zkraceno
    words = [w for w in s.split() if w]
    if not words:
        return name
    ini = lambda w: w[0].upper() + "." if w[0].isalpha() else w
    if len(words) == 1:
        return ini(words[0])
    return f"{ini(words[0])} {ini(words[-1])}"


def party_label(ucastnici, zobrazit=None):
    """Krátký popisek sporu ve tvaru „Xiaomi v. OSA".

    Přehledy soudů uvádějí účastníky jen jako plochý seznam bez rozlišení
    stran, takže první jméno bereme jako navrhovatele. Pokud hned následující
    jména sdílejí první slovo („Bayer AG", „Bayer Intellectual Property
    GmbH"), jde zjevně o tutéž stranu a spojí se dohromady; první odlišné
    jméno je protistrana, zbytek se schová do „a další".

    `zobrazit` mapuje účastníka na podobu, ve které se má vypsat (iniciály
    u fyzických osob). Které strany patří k sobě, se pozná pořád z původního
    jména – z iniciál ne: „K. J." a „K. Š." sdílejí první slovo, takže by
    ze dvou lidí udělaly jednu stranu a popisek by ukázal cizí protistranu.
    """
    zobrazit = zobrazit or {}
    parties = [(short_party(u), short_party(zobrazit.get(u, u)))
               for u in ucastnici if str(u).strip()]
    parties = [p for p in parties if p[0]]
    if not parties:
        return ""
    if len(parties) == 1:
        return parties[0][1]

    def head(p):
        return p.split()[0].casefold() if p.split() else p.casefold()

    i = 1
    while i < len(parties) and head(parties[i][0]) == head(parties[0][0]):
        i += 1
    if i >= len(parties):          # všechna jména jsou jedna strana
        return parties[0][1]
    label = f"{parties[0][1]} v. {parties[i][1]}"
    return label + " a další" if len(parties) > i + 1 else label


def make_item(datum, sin, predseda, spz, hodina, ucastnici):
    cislo, rejstrik, bc, rocnik = spz.group(1), spz.group(2), spz.group(3), spz.group(4)
    strany = merge_participant_lines(ucastnici)
    # Fyzické osoby mezi účastníky se na iniciály zkrátí až hromadně přes
    # redact_osoby(), která na to má celý seznam najednou.
    return {
        "datum": czech_date_to_iso(datum),
        "hodina": hodina or "",
        "sin": sin,
        "predseda": predseda,
        "spz": f"{int(cislo)} {norm_rejstrik(rejstrik)} {int(bc)}/{rocnik}",
        "nazev": party_label(strany),
        "senat": senat_key(cislo, rejstrik),
        "rejstrik": norm_rejstrik(rejstrik),
        "cislo_senatu": int(cislo),
        "bc": int(bc),
        "rocnik": int(rocnik),
        "ucastnici": strany,
    }


# --- AI záloha pro parsování přehledu ---

JEDNANI_AI_PROMPT = (
    "Toto je přehled nařízených soudních jednání českého soudu. Vytáhni z něj "
    "všechna jednání a odpověz POUZE platným JSON polem bez dalšího textu, "
    "každý prvek ve tvaru:\n"
    '{"datum": "DD.MM.RRRR", "sin": "číslo jednací síně", '
    '"predseda": "jméno předsedy senátu včetně titulů", '
    '"spisova_znacka": "např. 12 C 7/2026", "hodina": "HH:MM", '
    '"ucastnici": ["jméno", "jméno"]}\n'
    "Nic si nevymýšlej, přepisuj přesně z textu."
)


def parse_jednani_ai(text):
    raw = gemini_generate_raw(JEDNANI_AI_PROMPT, text[:60000], max_tokens=16384)
    data = extract_json(raw)
    items = []
    for row in data if isinstance(data, list) else []:
        try:
            spz = SPZ_RE.search(row.get("spisova_znacka", ""))
            if not spz or not czech_date_to_iso(row.get("datum", "")):
                continue
            items.append(make_item(
                row.get("datum", ""), str(row.get("sin", "")),
                row.get("predseda", ""), spz, row.get("hodina", ""),
                [u for u in row.get("ucastnici", []) if isinstance(u, str)],
            ))
        except (ValueError, AttributeError, TypeError):
            continue
    return items


def extract_json(raw):
    """Z odpovědi AI vyloupne první JSON blok (i z ```json ...``` ohrady)."""
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    start = min((i for i in (raw.find("["), raw.find("{")) if i >= 0), default=-1)
    if start < 0:
        return None
    for end in range(len(raw), start, -1):
        try:
            return json.loads(raw[start:end])
        except json.JSONDecodeError:
            continue
    return None


# --- Zkrácení fyzických osob mezi účastníky na iniciály (AI) ---

# Přehled soudu strany nijak netypuje (firma/osoba) a z tvaru jména se to
# uhádnout nedá – „Karolína Janáčková" a „Yunnan Tobacco" vypadají stejně.
# Rozhoduje proto jedině AI, která se na celý seznam podívá najednou.
OSOBY_AI_PROMPT = (
    "Toto je seznam účastníků soudních řízení (firmy, orgány veřejné moci "
    "i fyzické osoby smíchaně), tak jak je vypsal soud – bez rozlišení, co "
    "je co. U každé položky rozhodni, jestli je to FYZICKÁ OSOBA (člověk), "
    "nebo právnická osoba či orgán veřejné moci. Vodítko, ne pravidlo: "
    "jméno a příjmení člověka (často s titulem jako Ing., JUDr., Mgr.) je "
    "fyzická osoba; právní forma (s.r.o., a.s., z.s., GmbH, Ltd.), obor "
    "v názvu, zkratka nebo název úřadu značí právnickou osobu. Vypadá-li "
    "položka jako jméno člověka a nic nenasvědčuje firmě, ber ji jako "
    "fyzickou osobu – jde o ochranu osobních údajů, takže je horší "
    "člověka přehlédnout než zkrátit drobnou firmu.\n"
    "Odpověz POUZE platným JSON polem řetězců bez dalšího textu – obsahuje "
    "jen ty položky ze seznamu, které jsou fyzická osoba, přesně v podobě, "
    "v jaké jsou v seznamu (včetně titulu, pokud tam je)."
)

# Kolik jmen jde do jednoho dotazu. Do stropu odpovědi se musí vejít i to,
# co si Gemma „promyslí"; nad celým archivem najednou se nevejde (odpověď
# skončí na MAX_TOKENS) a klasifikace spadne celá, včetně jmen z čerstvého
# přehledu. Po dávkách roste s archivem počet volání, ne délka odpovědi.
OSOBY_DAVKA = 40


def ai_osoby(jmena):
    """Z jmen účastníků vybere přes AI ta, co jsou fyzická osoba.

    Vrací dvojici (osoby, nerozhodnutá): jména, která AI označila za
    fyzickou osobu, a jména, o kterých se nedozvěděla nic – protože AI
    neběží nebo se její dávka nedovolala. Nerozhodnuté jméno je něco jiného
    než zamítnuté („tohle je firma") a volající se podle toho musí zařídit.
    Jméno, které AI vrátí, ale v zadání nebylo (halucinace), se zahodí.

    Ptá se po dávkách (`OSOBY_DAVKA`), takže neúspěch jedné dávky zůstane
    v jejích jménech a ostatní se klasifikují dál.
    """
    jmena = list(jmena)
    if not jmena:
        return set(), set()
    if not gemini_enabled():
        return set(), set(jmena)
    osoby, nerozhodnuta = set(), set()
    for i in range(0, len(jmena), OSOBY_DAVKA):
        davka = jmena[i:i + OSOBY_DAVKA]
        raw = gemini_generate_raw(OSOBY_AI_PROMPT,
                                  "\n".join(f"- {j}" for j in davka))
        data = extract_json(raw)
        if not isinstance(data, list):
            nerozhodnuta.update(davka)
            continue
        platna = set(davka)
        osoby.update(j for j in data if isinstance(j, str) and j in platna)
    return osoby, nerozhodnuta


# Rozhodnutí AI „osoba/firma" se pamatují mezi běhy, ať se na tatáž jména
# (celý archiv) neptá každý den znovu. Soubor leží mimo docs/, nenasazuje se.
# Fyzické osoby jsou v něm jen jako hash jména – plné jméno se nikam
# neukládá. Firmy jsou v otevřeném tvaru (tak jako tak jsou na webu) a platí
# jen FIRMY_PLATNOST_DNU: kdyby AI jednou přehlédla člověka, další dotaz má
# šanci to napravit. Verdikt „osoba" platí napořád – omyl tím směrem jen
# zkrátí drobnou firmu. Po změně promptu se firmy zapomenou (`verze`).
# `rucne_firmy` je ruční seznam jmen, která jsou firma bez ptaní.
OSOBY_KES_FILE = "hearings_osoby.json"
FIRMY_PLATNOST_DNU = 7


def osoby_kes_verze():
    return hashlib.sha256(OSOBY_AI_PROMPT.encode("utf-8")).hexdigest()[:12]


def osoba_klic(jmeno):
    """Hash jména fyzické osoby pro keš (jméno samo se neukládá)."""
    norm = " ".join(str(jmeno).split())
    return hashlib.sha256(f"owl-osoby|{norm}".encode("utf-8")).hexdigest()


def nacti_osoby_kes(path=None):
    """Načte keš rozhodnutí; po změně promptu zapomene firmy."""
    try:
        kes = load_json(path or OSOBY_KES_FILE)
    except Exception:
        kes = {}
    if not isinstance(kes, dict):
        kes = {}
    kes.setdefault("osoby", {})
    kes.setdefault("firmy", {})
    kes.setdefault("rucne_firmy", [])
    if kes.get("verze") != osoby_kes_verze():
        kes["firmy"] = {}
        kes["verze"] = osoby_kes_verze()
    return kes


def uloz_osoby_kes(kes, path=None, dnes=None):
    """Uloží keš bez firem, kterým vypršela platnost."""
    dnes = dnes or date.today()
    hranice = (dnes - timedelta(days=FIRMY_PLATNOST_DNU)).isoformat()
    kes["firmy"] = {j: d for j, d in sorted(kes.get("firmy", {}).items())
                    if d > hranice}
    kes["osoby"] = dict(sorted(kes.get("osoby", {}).items()))
    save_json(path or OSOBY_KES_FILE, kes)


def verdikt_z_kese(kes, jmeno, dnes):
    """„osoba", „firma", nebo None (keš o jménu nic platného neví)."""
    if osoba_klic(jmeno) in kes.get("osoby", {}):
        return "osoba"
    if jmeno in (kes.get("rucne_firmy") or []):
        return "firma"
    zapsano = (kes.get("firmy") or {}).get(jmeno)
    hranice = (dnes - timedelta(days=FIRMY_PLATNOST_DNU)).isoformat()
    if zapsano and zapsano > hranice:
        return "firma"
    return None


def redact_osoby(nova, archiv=(), kes=None, dnes=None):
    """Účastníky, které AI označí za fyzickou osobu, nahradí iniciálami
    a dopočítá z nich zkrácený název sporu. Mění položky na místě.

    Do dotazu jdou i jména z archivu, ať se s každým během dočistí
    i jednání uložená dřív. Jména uložená už jako iniciály se ale dál
    zkracovat nedají, tak se na ně AI neptáme – jinak by dotaz s každým
    dalším přehledem nafukoval archiv.

    Když AI u jména nerozhodne, uloží se jeho jednání bez účastníků: celé
    jméno se radši nezveřejní a příští běh ho z přehledu soudu načte znovu.
    Archivu se to netýká – ten se z ničeho nedoplní, tak zůstane, jak je.

    S `kes` (viz nacti_osoby_kes) se AI ptá jen na jména, o kterých keš nic
    platného neví, a její rozhodnutí se do keše zapíšou (nerozhodnutá ne).
    main ji volá jednou za běh nad jednáními ze všech přehledů najednou.
    """
    dnes = dnes or date.today()
    polozky = list(nova) + list(archiv)
    jmena = sorted({u for it in polozky for u in it.get("ucastnici") or []
                    if initials(u) != u})
    osoby, k_dotazu = set(), jmena
    if kes is not None:
        k_dotazu = []
        for j in jmena:
            verdikt = verdikt_z_kese(kes, j, dnes)
            if verdikt == "osoba":
                osoby.add(j)
            elif verdikt is None:
                k_dotazu.append(j)
        print(f"    klasifikace osob: {len(jmena) - len(k_dotazu)} z {len(jmena)} "
              f"jmen z keše, na {len(k_dotazu)} se ptám AI")
    ai_osoby_, nerozhodnuta = ai_osoby(k_dotazu)
    osoby |= ai_osoby_
    if kes is not None:
        for j in k_dotazu:
            if j in nerozhodnuta:
                continue
            if j in ai_osoby_:
                kes["osoby"][osoba_klic(j)] = dnes.isoformat()
                kes["firmy"].pop(j, None)
            else:
                kes["firmy"][j] = dnes.isoformat()
    if nerozhodnuta:
        print(f"    AI nerozhodla u {len(nerozhodnuta)} z {len(k_dotazu)} jmen, "
              "kdo je fyzická osoba – jejich jednání ukládám bez účastníků")
        for it in nova:
            if any(u in nerozhodnuta for u in it.get("ucastnici") or []):
                it["ucastnici"] = []
                it["nazev"] = ""
    for it in polozky:
        strany = it.get("ucastnici") or []
        zobrazit = {u: initials(u) for u in strany
                    if u in osoby and initials(u) != u}
        # Beze změny se popisek nepřepočítává: u jednání, které má jména
        # zkrácená už z dřívějška, bychom strany párovali z iniciál, a to
        # nejde (viz party_label).
        if not zobrazit:
            continue
        it["nazev"] = party_label(strany, zobrazit)
        it["ucastnici"] = [zobrazit.get(u, u) for u in strany]


def prevzit_z_archivu(nova, archiv):
    """Jednání, u kterého AI v tomhle běhu nerozhodla, doplní ze záznamu,
    který o něm archiv už má. Mění položky na místě.

    Přehled soudu pokrývá pořád stejné období, takže každý běh čte tatáž
    jednání znovu, celými jmény, a klasifikuje je od nuly. Bez tohohle kroku
    stačí jeden výpadek AI, aby jednání, které už jednou zkrácené bylo,
    zase vypadlo na holou spisovou značku – a protože padá pokaždé jiná
    dávka, prázdná jednání se mezi běhy jen střídají, místo aby ubývala.

    Přebírá se jen to, co už jednou zveřejněné bylo (v archivu jsou fyzické
    osoby pod iniciálami), takže tím nic nového neuniká. Cenou je, že u
    jednání, kterému soud mezitím změnil účastníky, zůstane do příštího
    úspěšného rozhodnutí AI stará sestava – pořád lepší než nic.
    """
    podle_klice = {(j.get("spz"), j.get("datum")): j for j in archiv}
    prevzato = 0
    for it in nova:
        if it.get("ucastnici") or it.get("nazev"):
            continue
        byvale = podle_klice.get((it.get("spz"), it.get("datum")))
        if not byvale or not byvale.get("ucastnici"):
            continue
        it["ucastnici"] = list(byvale["ucastnici"])
        it["nazev"] = byvale.get("nazev", "")
        prevzato += 1
    if prevzato:
        print(f"    u {prevzato} jednání beru účastníky z minulého běhu")


# --- Aktualizace IP senátů z rozvrhu práce (AI) ---

IP_KEYWORDS_RE = re.compile(
    r"duševn|dusevn|autorsk|průmyslov|prumyslov|nekal\w{0,3}\s+sout|kolektivn\w+\s+správc"
    r"|kolektivn\w+\s+spravc|osobních údajů|osobnich udaju",
    re.IGNORECASE,
)

ROZVRH_AI_PROMPT = (
    "Toto jsou vybrané stránky rozvrhu práce českého soudu ({soud}). Najdi "
    "soudní oddělení (senáty) civilního úseku, která rozhodují věci duševního "
    "vlastnictví: autorské právo, kolektivní správci, průmyslové vlastnictví "
    "(ochranné známky, patenty…), nekalá soutěž, ochrana názvu a pověsti "
    "právnické osoby, spory z práva duševního vlastnictví; u odvolacího soudu "
    "i zpracování osobních údajů. NEpatří sem insolvence, veřejné rejstříky, "
    "cenné papíry, korporace, rozhodčí nálezy, trestní ani správní úsek.\n"
    "Ber jen senáty, které mají tuto agendu ve vlastním „Obor a vymezení "
    "působnosti“. Tabulka má u senátu i sloupec „Zastupuje senát“ (zastupující "
    "senáty či oddělení): ty IP senát jen zastupují, když nemůže jednat, samy "
    "IP agendu nemají – do výsledku je NEdávej.\n"
    "Odpověz POUZE platným JSON objektem bez dalšího textu ve tvaru:\n"
    '{{"senaty": [{{"senat": "<číslo> <rejstřík>", "predseda": "Jméno Příjmení",\n'
    '               "clenove": ["Jméno Příjmení", ...], "agenda": "stručně",\n'
    '               "poznamka": "stručně"}}],\n'
    '  "soudci": ["Jméno Příjmení", ...]}}\n'
    "Do „senaty“ dej KAŽDOU kombinaci čísla soudního oddělení a rejstříku "
    "(C, EC, Cm, ECm, Co, Cmo…), pod kterou tato oddělení vedou spisové "
    "značky – např. oddělení 12 s rejstříky Cm, ECm, C, EC = čtyři položky "
    "„12 Cm“, „12 ECm“, „12 C“, „12 EC“. Rejstříky Nc a EVCm vynech. "
    "U každé položky uveď předsedu senátu, v „clenove“ další soudce, kteří "
    "v senátu rozhodují (samosoudce = prázdný seznam), a v „agenda“ několika "
    "slovy, jaké věci senát podle rozvrhu soudí. Soudce na stáži nebo se "
    "zastaveným nápadem do „clenove“ nedávej, jen je zmiň v „poznamka“ "
    "(např. „na stáži bez nápadu: Jméno Příjmení“); jinak „poznamka“ vynech. "
    "Všechna jména bez titulů. Do „soudci“ dej předsedy těchto senátů. "
    "Nic si nevymýšlej."
)

# Od kdy rozvrh platí – z titulní strany („úplné znění s účinností od
# 15. 9. 2026", „změna od 1. 9. 2026", „s účinností ode dne 1. října 2026").
# Hledá se v textu bez diakritiky, protože pypdf ji u některých písem
# rozkládá.
MESICE = ("ledna", "unora", "brezna", "dubna", "kvetna", "cervna",
          "cervence", "srpna", "zari", "rijna", "listopadu", "prosince")
_DATUM = (r"(\d{1,2})\.\s*(?:(\d{1,2})\.|(" + "|".join(MESICE) + r"))\s*(\d{4})")
_OD = r"\bod(?:e)?(?:\s+dne)?\s+"
PLATNOST_RE = re.compile(
    r"\b(uplne\s+zneni|zmen[ay](?:\s+c\.?\s*\d+)?)[^()]{0,40}?" + _OD + _DATUM
)
UCINNOST_RE = re.compile(
    r"(?:ucinnost\w*\s*(?::\s*|" + _OD + r"|ke\s+dni\s+)"
    r"|\bplatn\w*\s+(?:od(?:e)?(?:\s+dne)?|ke\s+dni)\s+|\bplati\s+" + _OD + r")" + _DATUM
)


def _datum_z_shody(skupiny):
    """(den, měsíc číslem, měsíc slovem, rok) -> (d, m, r) jako čísla."""
    d, mes, slovem, rok = skupiny
    mes = MESICE.index(slovem) + 1 if slovem else int(mes)
    return int(d), mes, int(rok)


def platnost_rozvrhu(text):
    """Z titulní strany rozvrhu práce vytáhne, od kdy dokument platí.

    Vrací (popisek, datum ISO), např. („úplné znění od 15. 9. 2026",
    "2026-09-15"), nebo (None, None). Podle data se pozná, jestli soud
    vyvěsil novější rozvrh, než je v configu – ten mohl být sepsaný ručně
    z jiného souboru, takže hash sám nestačí."""
    t = " ".join(strip_diacritics(text or "").lower().split())
    m = PLATNOST_RE.search(t)
    if m:
        druh = "úplné znění" if m.group(1).startswith("uplne") else "změna"
        cislo = re.search(r"c\.?\s*(\d+)$", m.group(1))
        if cislo and druh == "změna":
            druh += f" č. {cislo.group(1)}"
        d, mes, rok = _datum_z_shody(m.groups()[1:])
    else:
        m = UCINNOST_RE.search(t)
        if not m:
            return None, None
        druh = "s účinností"
        d, mes, rok = _datum_z_shody(m.groups())
    try:
        iso = date(rok, mes, d).isoformat()
    except ValueError:
        return None, None
    return f"{druh} od {d}. {mes}. {rok}", iso


def jmeno_bez_titulu(jmeno):
    """„JUDr. Roman Horáček, MBA" -> „Roman Horáček" (pro zobrazení)."""
    j = TITLES_RE.sub(" ", str(jmeno or ""))
    return " ".join(re.sub(r"[.,;()]", " ", j).split())


def senat_poradi(k):
    """Řazení klíčů senátů: podle čísla, pak podle rejstříku."""
    cislo, _, rejstrik = k.partition(" ")
    return (int(cislo) if cislo.isdigit() else 0, rejstrik)


def rozvrh_z_odpovedi(data):
    """Z odpovědi AI na rozvrh práce vytáhne (senaty, soudci, sestavy).

    `senaty` a `soudci` jsou ploché seznamy jako dřív – podle nich se filtrují
    jednání (mark_ip). `sestavy` drží, kdo kterému senátu předsedá a kdo v něm
    sedí: senáty se stejným předsedou, členy, agendou i poznámkou (stáže,
    zastavený nápad) se slučují do jedné sestavy („1 Cmo, 2 Co – Horáček").
    Kalendář je ukazuje pod mřížkou."""
    senaty, soudci, skupiny = [], set(), {}
    if not isinstance(data, dict):
        return [], [], []
    for polozka in data.get("senaty", []):
        if not isinstance(polozka, dict):
            continue
        m = re.match(r"\s*(\d+)\s*([A-Za-z]+)", str(polozka.get("senat", "")))
        predseda = jmeno_bez_titulu(polozka.get("predseda"))
        if predseda:
            soudci.add(predseda)
        if not m:
            continue
        klic = senat_key(m.group(1), m.group(2))
        senaty.append(klic)
        clenove = polozka.get("clenove") if isinstance(polozka.get("clenove"), list) else []
        clenove = tuple(c for c in (jmeno_bez_titulu(c) for c in clenove)
                        if c and c != predseda)
        agenda = " ".join(str(polozka.get("agenda") or "").split())
        pozn = " ".join(str(polozka.get("poznamka") or "").split())
        skupiny.setdefault((predseda, clenove, agenda, pozn), []).append(klic)
    for j in data.get("soudci", []):
        j = " ".join(str(j).split())
        if j:
            soudci.add(j)
    sestavy = []
    for (predseda, clenove, agenda, pozn), klice in skupiny.items():
        sestava = {"senaty": sorted(set(klice), key=senat_poradi), "predseda": predseda,
                   "clenove": list(clenove), "agenda": agenda}
        if pozn:
            sestava["pozn"] = pozn
        sestavy.append(sestava)
    sestavy.sort(key=lambda x: senat_poradi(x["senaty"][0]))
    return sorted(set(senaty), key=senat_poradi), sorted(soudci), sestavy


# Kolik stran od začátku se hledá datum platnosti (titulní list nemusí být
# první) a kolik znaků IP stránek jde do AI.
PLATNOST_STRAN = 5
ROZVRH_AI_ZNAKU = 80000


def velka_zmena_senatu(stare, nove):
    """Změní se seznam IP senátů víc, než je u běžné změny rozvrhu obvyklé?
    Počítá se oběma směry – AI umí senáty přidat (zastupující senáty VS
    7. 9. 2026) stejně jako vynechat."""
    stare, nove = set(stare or []), set(nove or [])
    if not stare:
        return False
    return len(stare ^ nove) > max(3, len(stare) // 2)


def update_rozvrh(config, court, pdf_bytes, source_url, popisek_odkazu=None):
    """Z rozvrhu práce (PDF) nechá AI vytáhnout IP senáty a soudce; při
    neúspěchu nechá dosavadní konfiguraci být.

    Rozvrh, který není novější než ten zapsaný v configu, se AI neposílá:
    buď má stejný hash, nebo podle titulní strany neplatí od pozdějšího dne
    (`rozvrh_zdroj.platnost_od`). Config tak může být sepsaný ručně podle
    rozvrhu, který scraper nikdy nestáhl, a týdenní kontrola ho nepřepíše
    starším ani stejným dokumentem. Přepíše ho až rozvrh s pozdějším datem.

    Datum se hledá na prvních PLATNOST_STRAN stranách, a když tam není,
    v textu odkazu na dokument. Když ho config má a dokument ne, seznam se
    nepřepisuje (nejde říct, jestli je rozvrh novější) – jen se varuje,
    a hash se neukládá, ať se dokument čte znovu, dokud config někdo ručně
    nepotvrdí. Stejně tak se nezapíše výsledek AI, který seznam senátů
    změní víc než běžná změna rozvrhu (velka_zmena_senatu)."""
    cfg = config["courts"][court]
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    zdroj = cfg.get("rozvrh_zdroj") or {}
    # Sestavy senátů (kdo senátu předsedá, kdo v něm sedí) se ukládají až od
    # 9/2026. Kde ještě chybí, vytáhne je AI jednou i z nezměněného rozvrhu.
    if zdroj.get("hash") == digest and "sestavy" in cfg:
        print(f"  [{court}] rozvrh práce beze změny (hash sedí)")
        return False

    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    texty = []
    for page in reader.pages:
        try:
            texty.append(page.extract_text() or "")
        except Exception:
            texty.append("")

    platnost, platnost_od = platnost_rozvrhu("\n".join(texty[:PLATNOST_STRAN]))
    if not platnost_od and popisek_odkazu:
        platnost, platnost_od = platnost_rozvrhu(popisek_odkazu)
    zapsano_od = zdroj.get("platnost_od")
    if not platnost_od and zapsano_od and "sestavy" in cfg:
        warning(f"Rozvrh {court}: dokument se změnil, ale datum platnosti v něm "
                f"není k přečtení – seznam senátů (platný od {zapsano_od}) "
                f"nechávám, zkontroluj ručně: {source_url}")
        return False
    if platnost_od and zapsano_od and platnost_od <= zapsano_od and "sestavy" in cfg:
        print(f"  [{court}] rozvrh {platnost} není novější než zapsaný "
              f"({zdroj.get('platnost') or zapsano_od}) – nechávám")
        # Příště ho pozná už podle hashe a nemusí číst PDF.
        cfg["rozvrh_zdroj"] = {**zdroj, "url": source_url, "hash": digest}
        return False
    if not gemini_enabled():
        print(f"  [{court}] rozvrh se změnil, ale AI je vypnutá – nechávám starý seznam")
        return False

    pages = [f"--- strana {i + 1} ---\n{t}" for i, t in enumerate(texty)
             if IP_KEYWORDS_RE.search(t)]
    if not pages:
        print(f"  [{court}] v rozvrhu nejsou stránky s IP klíčovými slovy – nechávám starý seznam")
        return False

    text = "\n\n".join(pages)
    if len(text) > ROZVRH_AI_ZNAKU:
        warning(f"Rozvrh {court}: IP stránky mají {len(text)} znaků, do AI jde "
                f"jen prvních {ROZVRH_AI_ZNAKU} – seznam senátů může být neúplný")
        text = text[:ROZVRH_AI_ZNAKU]
    prompt = ROZVRH_AI_PROMPT.format(soud=cfg["nazev"])
    raw = gemini_generate_raw(prompt, text, max_tokens=8192)
    senaty, soudci_display, sestavy = rozvrh_z_odpovedi(extract_json(raw))
    if not senaty:
        print(f"  [{court}] AI z rozvrhu nic nevytáhla – nechávám starý seznam")
        return False
    if velka_zmena_senatu(cfg.get("senaty"), senaty):
        warning(f"Rozvrh {court}: AI vrátila seznam senátů hodně odlišný od "
                f"zapsaného (přibylo {sorted(set(senaty) - set(cfg.get('senaty', [])))}, "
                f"ubylo {sorted(set(cfg.get('senaty', [])) - set(senaty))}) – "
                "nezapisuji, zkontroluj ručně")
        return False

    cfg["senaty"] = senaty
    if soudci_display:
        cfg["soudci"] = soudci_display
    cfg["sestavy"] = sestavy
    # `popis` je ruční poznámka, odkud seznam pochází – tu si neseme dál.
    cfg["rozvrh_zdroj"] = {
        "popis": zdroj.get("popis"),
        "platnost": platnost,
        "platnost_od": platnost_od,
        "url": source_url,
        "hash": digest,
        "aktualizovano": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    config["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"  [{court}] IP senáty z rozvrhu obnoveny ({platnost or 'platnost neznámá'}): "
          f"{', '.join(senaty)}")
    return True


def zapis_sledovane(output, config):
    """Zapíše do výstupu, koho kalendář sleduje – z nich web skládá patičku
    „Koho kalendář sleduje".

    `senaty` a `soudci` jsou seznamy, podle kterých se jednání filtrují,
    `sestavy` kdo senátům předsedá a kdo v nich sedí. K sestavám se přidají
    `sestavy_navic`: senáty jen pro přehled, podle kterých se nefiltruje
    (správní oddělení MSPH, která soudí žaloby proti ÚPV vedle jiné agendy –
    ty se poznají podle žalovaného). `rozvrhy` je odkaz na stránku rozvrhu
    práce a verze, podle které je seznam sepsaný."""
    courts = config.get("courts", {})
    output["senaty"] = {c: cfg.get("senaty", []) for c, cfg in courts.items()}
    output["soudci"] = {c: cfg.get("soudci", []) for c, cfg in courts.items()}
    output["sestavy"] = {c: cfg.get("sestavy", []) + cfg.get("sestavy_navic", [])
                         for c, cfg in courts.items()}
    output["rozvrhy"] = {
        c: {"platnost": (cfg.get("rozvrh_zdroj") or {}).get("platnost"),
            "url": cfg.get("rozvrh_url")}
        for c, cfg in courts.items()
    }
    return output


# --- Filtr IP ---

def ucastnik_re(vzory):
    """Regex na účastníky z configu (`ucastnici_ip`): bez diakritiky
    a velikosti písmen a jen jako celá slova – „ÚPV" má sedět jako zkratka,
    ne uvnitř jména typu „SUPVOLT". Text účastníků se před hledáním upraví
    stejně (viz mark_ip)."""
    alts = [re.escape(" ".join(strip_diacritics(str(v)).casefold().split()))
            for v in vzory if str(v).strip()]
    if not alts:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)")


def mark_ip(items, cfg):
    """Označí jednání, která patří do agendy duševního vlastnictví.

    Tři pravidla, protože tři různé situace:

    1. Senát ze spisové značky je v seznamu IP senátů (běžné sporné věci
       civilního úseku).
    2. Mezi účastníky je Úřad průmyslového vlastnictví (`ucastnici_ip`).
       Žaloby proti jeho rozhodnutím (známky, patenty, vzory…) soudí úsek
       správního soudnictví MSPH a jeho oddělení mají vedle nich všechnu
       ostatní správní agendu, takže senát nic neříká. Kritérium je, že
       žalovaným je ÚPV; přehled strany nerozlišuje, ale ve správním
       soudnictví je úřad vždycky na straně žalované, takže stačí, že je
       mezi účastníky. Pravidlo platí napříč úseky i soudy – kde se ÚPV
       jako účastník objeví, je to IP věc. Řádek bez účastníků IP není:
       bez senátu, o který by se dalo opřít, by se jinak brala celá
       správní agenda.
    3. Rejstřík Nc (předběžná opatření, zajištění důkazu): číslo ve značce
       specializaci nerozlišuje – u obchodního „2 Nc" i civilního „1 Nc" je
       to číslo rejstříku, ne oddělení – takže rozhoduje předseda senátu.
       Tím se chytí PO v ochranných známkách i autorskoprávní PO.
    """
    # Config se edituje i ručně, takže se na velikost písmen rejstříku
    # nespoléháme („12 ECm" i „12 Ecm" musí platit stejně).
    senaty = {str(s).casefold() for s in cfg.get("senaty", [])}
    ucastnik = ucastnik_re(cfg.get("ucastnici_ip", []))
    soudci = {normalize_judge(j) for j in cfg.get("soudci", [])}

    for it in items:
        senat = it["senat"].casefold()
        strany = " ".join(strip_diacritics(
            " | ".join(it.get("ucastnici") or [])).casefold().split())
        if senat in senaty:
            it["ip"] = True
        elif ucastnik and ucastnik.search(strany):
            it["ip"] = True
        elif it["rejstrik"] == "Nc":
            it["ip"] = normalize_judge(it["predseda"]) in soudci
        else:
            it["ip"] = False
    return items


# --- Hlavní běh ---

def raw_text_of(data):
    """Prostý text dokumentu (pro kontroly kvality a AI zálohu)."""
    if data[:5] == b"%PDF-":
        return pdf_text(data)
    if data[:2] == b"PK":
        return docx_tables(data)[1]
    return ""


def ai_text_of(data):
    """Text dokumentu pro AI zálohu. U .docx po řádcích tabulky s oddělenými
    buňkami („ | ") a odstavci v buňce („; ") – z plochého textu by AI
    nepoznala, kde končí jeden účastník a začíná další, ani jeden řádek."""
    if data[:2] == b"PK":
        tables, text = docx_tables(data)
        radky = [" | ".join("; ".join(c) for c in row)
                 for rows in tables for row in rows]
        if radky:
            return "\n".join(radky)
        return text
    return raw_text_of(data)


def expected_rows(text):
    """Kolik jednání dokument nejspíš obsahuje – počítá data ve tvaru
    DD.MM.RRRR, na kterých každý řádek přehledu začíná. Hlavička uvádí
    období dvěma daty, ta se odečtou."""
    n = len(DATE_RE.findall(text or ""))
    return max(0, n - (2 if parse_period(text or "") else 0))


def ocekavane_radky(data, text):
    """Kolik jednání dokument nejspíš obsahuje.

    U .docx se počítají řádky tabulek, ve kterých je někde datum – nezávisle
    na tom, jestli hlavička s obdobím mimo tabulku má tvar, jaký zná
    parse_period (jinak by se její dvě data přičetla a krátký přehled by
    pokaždé vypadal neúplný). Přehozené sloupce to pozná pořád: datum je
    v řádku, jen jinde. U PDF jako dřív expected_rows."""
    if data[:2] == b"PK":
        try:
            tables, _ = docx_tables(data)
        except Exception:
            return expected_rows(text)
        return sum(1 for rows in tables for row in rows
                   if any(DATE_RE.search(p) for c in row for p in c))
    return expected_rows(text)


# Stav jednoho přehledu po scrape_jednani (čtvrtá hodnota):
PREHLED_OK = "ok"                 # naparsovaný celý
PREHLED_NEUPLNY = "neuplny"       # naparsovaná jen část – nic se nemaže
PREHLED_BEZ_DOKUMENTU = "bez_dokumentu"   # úsek dokument zrovna nevydal
PREHLED_CHYBA = "chyba"           # stažení/formát/parsování selhalo


def warning(zprava):
    """Varování do logu i do shrnutí běhu v GitHub Actions (anotace
    ::warning:: se čte ze stdoutu kroku)."""
    print(f"::warning::{zprava}")


def scrape_jednani(court, cfg, prehled, local_file=None):
    """Stáhne (nebo načte lokálně) jeden přehled jednání a naparsuje ho.

    Vrací (items, period, zdroj_url, stav); items je None, když se
    nepodařilo získat vůbec nic – volající pak nechá dosavadní data být.
    `stav` je jedno z PREHLED_*: odliší skutečné selhání od úseku, který
    dokument zrovna nevydal, a úplný přehled od neúplného.

    Kromě parsování hlídá i jeho úplnost: když se z dokumentu naparsuje
    výrazně méně řádků, než kolik je v něm dat, jde nejspíš o změnu formátu
    a nastupuje AI záloha. Když nepomůže ani ta, vrátí se, co se naparsovat
    dalo, se stavem PREHLED_NEUPLNY – volající z takového přehledu nic
    nemaže (jinak by jednání, která parser nepřečetl, tiše zmizela jako
    odvolaná).

    Když hlavička období neuvádí v podobě, jakou zná parse_period, vezme se
    z adresy dokumentu, a nakonec jako rozsah dnů naparsovaných jednání –
    to jen u úplného deterministického parsování (chybné datum z AI by
    okno rozšířilo a smazalo jednání, která v přehledu jen nebyla přečtená).
    """
    usek = prehled.get("usek", "")
    tag = f"{court}/{usek}" if usek else court
    data, zdroj = None, None
    if local_file:
        with open(local_file, "rb") as f:
            data = f.read()
        # Fixture není zveřejnitelný zdroj – ať se do publikovaných dat
        # nedostane název testovacího souboru místo odkazu na msp.gov.cz.
        zdroj = None
        print(f"  [{tag}] lokální dokument: {local_file}")
    else:
        try:
            links = find_document_links(cfg["jednani_url"])
            href, text = pick_link(
                links,
                keywords=tuple(prehled.get("keywords", ("jednani", "prehled"))),
                prefer=tuple(prehled.get("prefer", ())),
                # Víc přehledů na jedné stránce: každý jen podle vlastních
                # slov, ať se za chybějící dokument nevezme ten vedlejší.
                strict=len(cfg.get("prehledy", [])) > 1,
            )
            if not href:
                if links:
                    print(f"  [{tag}] na stránce není dokument tohoto úseku")
                    return None, None, None, PREHLED_BEZ_DOKUMENTU
                print(f"  [{tag}] na stránce nejsou odkazy na dokumenty")
                return None, None, None, PREHLED_CHYBA
            print(f"  [{tag}] stahuji: {text or href}")
            data = http_get(href).content
            zdroj = href
        except requests.RequestException as e:
            print(f"  [{tag}] stažení selhalo: {e}")
            return None, None, None, PREHLED_CHYBA

    is_docx = data[:2] == b"PK"
    is_pdf = data[:5] == b"%PDF-"
    if not (is_docx or is_pdf):
        print(f"  [{tag}] neznámý formát dokumentu – přeskočeno")
        return None, None, None, PREHLED_CHYBA

    items, period = [], None
    try:
        items, period = (parse_jednani_docx(data) if is_docx
                         else parse_jednani_pdf(data))
    except Exception as e:
        print(f"  [{tag}] deterministické parsování spadlo: {e}")

    items = [it for it in items if it.get("datum")]
    text = raw_text_of(data)
    ocekavano = ocekavane_radky(data, text)
    uplny = lambda n: not ocekavano or n >= ocekavano * MIN_PARSE_RATIO
    chybi = not uplny(len(items))
    if chybi:
        print(f"  [{tag}] POZOR: naparsováno {len(items)} z ~{ocekavano} "
              f"řádků – dokument nejspíš změnil formát")

    z_ai = False
    if (not items or chybi) and gemini_enabled():
        print(f"  [{tag}] zkouším AI parsování")
        try:
            ai_items = [it for it in parse_jednani_ai(ai_text_of(data))
                        if it.get("datum")]
            if len(ai_items) > len(items):
                items, z_ai = ai_items, True
                print(f"  [{tag}] AI naparsovala {len(items)} jednání")
        except Exception as e:
            print(f"  [{tag}] AI parsování selhalo: {e}")

    # Úplnost až po AI: i odpověď AI může být useknutá (strop tokenů).
    stav = PREHLED_OK if uplny(len(items)) else PREHLED_NEUPLNY
    if items and stav == PREHLED_NEUPLNY:
        warning(f"Jednání {tag}: naparsováno {len(items)} z ~{ocekavano} řádků – "
                "ukládám bez mazání a hledání změn, dokument nejspíš změnil formát")

    if items and not period:
        period = obdobi_z_odkazu(zdroj)
        odkud = "z adresy dokumentu"
        if not period and stav == PREHLED_OK and not z_ai:
            dny = sorted(it["datum"] for it in items)
            period, odkud = (dny[0], dny[-1]), "podle naparsovaných jednání"
        warning(f"Jednání {tag}: v hlavičce dokumentu není období"
                + (f" – beru ho {odkud}: {period[0]} – {period[1]}" if period
                   else " – bez něj se nehledají změny ani odvolaná jednání"))

    for it in items:
        it["usek"] = usek
    # Řádky bez účastníků do logu – ve správním úseku se filtruje podle
    # účastníků, takže bez nich by se žaloba proti ÚPV nepoznala.
    bez_stran = sum(1 for it in items if not it.get("ucastnici"))
    print(f"  [{tag}] jednání: {len(items)}/{ocekavano or '?'}"
          + (f", období {period[0]} – {period[1]}" if period else "")
          + (f", bez účastníků {bez_stran}" if bez_stran else ""))
    if not items:
        return None, period, zdroj, PREHLED_CHYBA
    return items, period, zdroj, stav


# --- Co se v přehledu změnilo od minule ---------------------------------

# Soud přehled jednání průběžně vydává znovu a jednání v něm přibývají,
# mizí i se stěhují. InfoSoud, kde by šel stav řízení ověřit, je celý
# vykreslený javascriptem a data z něj vytáhnout nejde, takže jediný
# spolehlivý zdroj je porovnání dvou po sobě jdoucích přehledů. Změny se
# ukládají do výstupu, aby bylo v kalendáři vidět, co se pohnulo.
ZMENY_DNU = 30        # jak dlouho se změny drží ve výstupu
ZMENY_MAX = 200       # strop, ať soubor neroste donekonečna


def porovnej_prehled(stare, nove, v_prehledu, court, usek, dnes):
    """Změny mezi uloženými jednáními a novým přehledem téhož období.

    `stare` i `nove` jsou jednání v agendě duševního vlastnictví ze stejného
    soudu, úseku a období. `v_prehledu` je celý nový dokument včetně neIP
    věcí – jednání, které v něm je, soud neodvolal, i když se nám do archivu
    neukládá.

    Přesun se pozná tak, že spisová značka zmizela z jednoho dne a objevila
    se v jiném; když se den nezměnil, hlídá se ještě hodina a jednací síň.
    """
    def podle_znacky(jednani):
        dle = {}
        for j in jednani:
            dle.setdefault(j.get("spz"), {})[j.get("datum")] = j
        return dle

    st, nv = podle_znacky(stare), podle_znacky(nove)
    zmeny = []
    for spz in sorted(set(st) | set(nv)):
        stare_dny, nove_dny = st.get(spz, {}), nv.get(spz, {})
        # Co soud v dokumentu vypsal (byť jako neIP věc), to neodvolal.
        zmizely = sorted(d for d in stare_dny if (spz, d) not in v_prehledu)
        pribyly = sorted(set(nove_dny) - set(stare_dny))
        for i in range(max(len(zmizely), len(pribyly))):
            z = zmizely[i] if i < len(zmizely) else None
            na = pribyly[i] if i < len(pribyly) else None
            zaznam = {"soud": court, "usek": usek, "spz": spz,
                      "typ": "presun" if z and na else ("nove" if na else "zruseno"),
                      "datum": na or z}
            if z and na:
                zaznam["z"], zaznam["na"] = z, na
            elif z:
                # Odvolané jednání z archivu zmizí, takže stránka ho u
                # změny nemá odkud dohledat – popisek a hodina jdou s ní.
                # Bere se z archivu, kde jsou fyzické osoby pod iniciálami.
                for pole in ("nazev", "hodina"):
                    if stare_dny[z].get(pole):
                        zaznam[pole] = stare_dny[z][pole]
            zmeny.append(zaznam)

        for den in sorted(set(stare_dny) & set(nove_dny)):
            a, b = stare_dny[den], nove_dny[den]
            for pole, typ in (("hodina", "cas"), ("sin", "sin")):
                if (a.get(pole) or "") != (b.get(pole) or "") and b.get(pole):
                    zmeny.append({"soud": court, "usek": usek, "spz": spz,
                                  "typ": typ, "datum": den,
                                  "z": a.get(pole) or "", "na": b.get(pole)})
    for z in zmeny:
        z["kdy"] = dnes.isoformat()
        # Slovní popis jde s daty, ať ho stránka nemusí mít opsaný podruhé.
        z["popis"] = ZMENA_POPIS[z["typ"]]
    return zmeny


ZMENA_POPIS = {
    "nove": "nové jednání",
    "presun": "přeloženo",
    "zruseno": "vypadlo z přehledu",
    "cas": "jiný čas",
    "sin": "jiná síň",
}


def vypis_zmeny(zmeny, court, usek):
    """Změny do logu – ať je v běhu vidět, co se v přehledu pohnulo."""
    if not zmeny:
        print(f"  [{court}/{usek}] proti minulému přehledu beze změn")
        return
    print(f"  [{court}/{usek}] změn proti minulému přehledu: {len(zmeny)}")
    for z in zmeny:
        detail = f" {z['z']} → {z['na']}" if z.get("na") and z.get("z") else ""
        print(f"    {z['spz']} ({z['datum']}): {z['popis']}{detail}")


def orez_zmeny(zmeny):
    """Nechá jen změny za poslední měsíc, od nejnovější, bez duplicit.

    Stejná změna se najde znovu pokaždé, co soud přehled vydá – ukládá se
    proto jen jednou, s datem, kdy se objevila poprvé.
    """
    hranice = (date.today() - timedelta(days=ZMENY_DNU)).isoformat()
    videne, ponechane = set(), []
    # Od nejstarší, ať z duplicit zůstane ta s datem prvního výskytu.
    for z in sorted(zmeny, key=lambda z: z.get("kdy") or ""):
        klic = (z.get("soud"), z.get("spz"), z.get("typ"), z.get("datum"),
                z.get("z"), z.get("na"))
        if klic in videne or (z.get("kdy") or "") < hranice:
            continue
        videne.add(klic)
        ponechane.append(z)
    ponechane.sort(key=lambda z: (z.get("kdy") or "", z.get("datum") or ""),
                   reverse=True)
    return ponechane[:ZMENY_MAX]


# Úsek, který se u jednání nepopisuje – je to většina kalendáře a všude
# by jen překážel. Ostatní (správní soudnictví) se ukazují jménem.
VYCHOZI_USEK = "civilni"


def usek_nazev(cfg, usek):
    """Jméno úseku z configu („Úsek správního soudnictví"), None bez něj."""
    for p in cfg.get("prehledy", []):
        if p.get("usek", "") == usek and p.get("nazev"):
            return p["nazev"]
    return None


def merge_output(existing, court, items, period, zdroj_url, cfg,
                 mazat=True, redigovat=True):
    """Zanese nový přehled do výstupu.

    Ukládají se jen jednání v agendě duševního vlastnictví. Ostatní věci
    soud v přehledu zveřejňuje také, ale do tohohle archivu nepatří a není
    důvod rozepisovat jejich účastníky.

    Proběhlá jednání zůstávají – kalendář je archiv. Když ale jednání zmizí
    z nově vydaného přehledu, který jeho den pokrývá, soud ho odvolal nebo
    přeložil; takový záznam se smaže. Přeložené jednání se vrátí samo pod
    novým datem, jakmile ho soud v některém přehledu vypíše.

    `mazat=False` je pro neúplně naparsovaný přehled: jednání z něj se
    zapíšou a čerstvé verze nahradí staré, ale nic se nemaže ani nehlásí
    jako změna – chybějící řádek tu nic neznamená. Období úseku se zapíše
    i tak (stránka podle něj ukazuje, dokdy je kalendář zveřejněný).

    `redigovat=False`, když volající už fyzické osoby zkrátil sám (main to
    dělá jednou za běh pro všechny přehledy, viz redact_osoby).
    """
    jednani = [j for j in existing.get("jednani", []) if isinstance(j, dict)]
    # Nahrazuje se vždy jen jeden úsek jednoho soudu – civilní a správní
    # přehled pokrývají stejné dny, ale každý jiné senáty.
    usek = items[0].get("usek", "") if items else ""
    # Jestli jednání z přehledu zmizelo, se posuzuje proti celému dokumentu,
    # ne jen proti jeho IP části: když oddělení vypadne ze seznamu IP senátů,
    # jeho jednání z archivu odejde, ne že se označí za odvolané.
    v_prehledu = {(j.get("spz"), j.get("datum")) for j in items}
    items = [it for it in items if it.get("ip")]
    # Fyzické osoby mezi účastníky na iniciály – jen pro jednání, co se
    # opravdu uloží (viz redact_osoby).
    if redigovat:
        redact_osoby(items, jednani)
    prevzit_z_archivu(items, jednani)
    od, do = period if period else (None, None)

    # Změny se hledají jen v období, které nový přehled pokrývá – mimo něj
    # o jednání nic neříká a jeho nepřítomnost nic neznamená.
    if period and mazat:
        v_obdobi = [j for j in jednani
                    if j.get("soud") == court and j.get("usek", "") == usek
                    and od <= (j.get("datum") or "") <= do]
        zmeny = porovnej_prehled(v_obdobi, items, v_prehledu, court, usek,
                                 date.today())
        vypis_zmeny(zmeny, court, usek)
        # Nová jednání stránka mezi změnami neukazuje (jsou vidět v mřížce),
        # tak se do výstupu neukládají – jen do logu.
        existing.setdefault("zmeny", []).extend(
            z for z in zmeny if z["typ"] != "nove")

    zachovane = []
    for j in jednani:
        stejny_zdroj = j.get("soud") == court and j.get("usek", "") == usek
        if stejny_zdroj and (j.get("spz"), j.get("datum")) in v_prehledu:
            # Přepíše ho čerstvá verze níže – a pokud už IP není, vypadne.
            continue
        if (stejny_zdroj and period and mazat
                and od <= (j.get("datum") or "") <= do):
            # Den spadá do nového přehledu, ale jednání v něm není – soud
            # ho odvolal nebo přeložil, takže v kalendáři nemá co dělat.
            continue
        zachovane.append(j)

    for it in items:
        it["soud"] = court
    jednani = zachovane + items
    jednani.sort(key=lambda j: (j.get("datum") or "", j.get("hodina") or "", j.get("spz") or ""))
    existing["jednani"] = jednani

    courts = existing.setdefault("courts", {})
    meta = courts.setdefault(court, {})
    meta["nazev"] = cfg["nazev"]
    meta["infosoud_org"] = cfg["infosoud_org"]
    # Každý úsek má vlastní dokument, a tedy i vlastní období a čas stažení.
    # Jméno úseku jde s sebou, ať ho stránka a .ics můžou ukázat u jednání
    # (u žaloby proti ÚPV vysvětluje, proč je v IP kalendáři senát „15 A").
    meta.setdefault("useky", {})[usek] = {
        "nazev": usek_nazev(cfg, usek),
        "obdobi": {"od": period[0], "do": period[1]} if period else None,
        "stazeno": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "zdroj": zdroj_url,
    }


# --- iCalendar export (přihlášení v Google Kalendáři přes URL) ---

# Google si externí kalendář tahá sám, jednou za několik hodin; proto stačí,
# že soubor leží vedle stránky.
ICS_FILE = "docs/hearings.ics"
# Doména webu (odkaz na kalendář v hearings.json); jde přepsat SITE_HOST.
SITE_DEFAULT_HOST = "owl.davidzavada.cz"
# Doména v UID událostí je jen identifikátor – zůstává z doby, kdy web běžel
# na rss.davidzavada.cz. Kdyby se změnila, kalendáře, které si soubor
# stáhly, by viděly každé jednání dvakrát.
ICS_UID_HOST = "rss.davidzavada.cz"

# Pražská zóna napsaná ručně – jednání jsou vždy v místním čase a bez VTIMEZONE
# by je klienti mimo ČR posunuli.
VTIMEZONE = [
    "BEGIN:VTIMEZONE",
    "TZID:Europe/Prague",
    "BEGIN:STANDARD",
    "DTSTART:19701025T030000",
    "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
    "TZOFFSETFROM:+0200",
    "TZOFFSETTO:+0100",
    "TZNAME:CET",
    "END:STANDARD",
    "BEGIN:DAYLIGHT",
    "DTSTART:19700329T020000",
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
    "TZOFFSETFROM:+0100",
    "TZOFFSETTO:+0200",
    "TZNAME:CEST",
    "END:DAYLIGHT",
    "END:VTIMEZONE",
]


def ics_escape(s):
    return (str(s).replace("\\", "\\\\").replace(";", r"\;")
            .replace(",", r"\,").replace("\n", r"\n"))


def ics_fold(line):
    """RFC 5545: řádek nejvýše 75 oktetů, pokračování začíná mezerou."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    if cur:
        out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def site_host():
    """Doména webu: proměnná SITE_HOST, jinak SITE_DEFAULT_HOST. Na UID
    událostí nemá vliv (ICS_UID_HOST)."""
    return os.environ.get("SITE_HOST", "").strip() or SITE_DEFAULT_HOST


def infosoud_url(j, courts):
    """Odkaz na detail řízení v InfoSoudu. Rejstřík se posílá malými písmeny
    (`druhVeci=co`), jinak řízení nenajde."""
    org = (courts.get(j.get("soud"), {}) or {}).get("infosoud_org", "")
    return (
        "https://infosoud.gov.cz/InfoSoud/detail-rizeni"
        "?typOrganizace=VSECHNY_KRAJE"
        f"&druhOrganizace={org}"
        f"&cisloSenatu={j.get('cislo_senatu')}"
        f"&druhVeci={str(j.get('rejstrik') or '').lower()}"
        f"&bcVec={j.get('bc')}&rocnik={j.get('rocnik')}"
    )



def ics_uid(j):
    """Stálý identifikátor události: SHA-1 ze soudu, značky, dne a hodiny.
    Ukládá se i do hearings.json – stránka podle něj vyřízne jedno jednání
    z hotového hearings.ics, takže si ho nemusí skládat sama."""
    return hashlib.sha1(
        f"{j.get('soud')}|{j.get('spz')}|{j.get('datum')}|{j.get('hodina')}"
        .encode("utf-8")
    ).hexdigest()


def write_ics(output, path=None):
    """Zapíše IP jednání jako iCalendar – na tenhle soubor se dá přihlásit
    v Google Kalendáři (Jiné kalendáře → Přidat → Z adresy URL). Každému
    jednání zároveň doplní `uid`."""
    path = path or ICS_FILE
    courts = output.get("courts", {})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    host = ICS_UID_HOST

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//IP-rss-feed//Kalendar jednani IP//CS",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Jednání IP – MS a VS Praha",
        "X-WR-TIMEZONE:Europe/Prague",
        "X-WR-CALDESC:Nařízená jednání v agendě duševního vlastnictví "
        "u Městského a Vrchního soudu v Praze.",
        # Google se u externích kalendářů stejně řídí vlastním intervalem,
        # ostatní klienti si vezmou tuhle nápovědu.
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
        "X-PUBLISHED-TTL:PT12H",
    ] + VTIMEZONE

    count = 0
    for j in output.get("jednani", []):
        if not j.get("ip") or not j.get("datum"):
            continue
        count += 1
        ymd = j["datum"].replace("-", "")
        uid = j["uid"] = ics_uid(j)
        court = (courts.get(j.get("soud"), {}) or {}).get("nazev", j.get("soud", ""))

        lines += ["BEGIN:VEVENT", f"UID:{uid}@{host}", f"DTSTAMP:{stamp}"]
        m = TIME_RE.match(j.get("hodina") or "")
        if m:
            hh, mm = (int(x) for x in m.group(0).split(":"))
            start = datetime(2000, 1, 1, hh, mm)
            end = start + timedelta(hours=1)
            lines += [
                f"DTSTART;TZID=Europe/Prague:{ymd}T{start:%H%M}00",
                f"DTEND;TZID=Europe/Prague:{ymd}T{end:%H%M}00",
            ]
        else:
            # Bez hodiny nemá smysl předstírat čas – celodenní záznam.
            nxt = (date.fromisoformat(j["datum"]) + timedelta(days=1)).isoformat()
            lines += [
                f"DTSTART;VALUE=DATE:{ymd}",
                f"DTEND;VALUE=DATE:{nxt.replace('-', '')}",
            ]

        summary = j.get("nazev") or j.get("spz") or "Jednání"
        location = court + (f", jednací síň {j['sin']}" if j.get("sin") else "")
        desc = [f"Spisová značka: {j.get('spz', '')}"]
        if j.get("predseda"):
            desc.append(f"Předseda senátu: {j['predseda']}")
        if j.get("senat"):
            desc.append(f"Senát: {j['senat']}")
        if j.get("ucastnici"):
            desc.append("Účastníci: " + "; ".join(j["ucastnici"]))
        usek = j.get("usek") or ""
        if usek and usek != VYCHOZI_USEK:
            useky = (courts.get(j.get("soud"), {}) or {}).get("useky", {}) or {}
            desc.append("Úsek: " + ((useky.get(usek) or {}).get("nazev") or usek))
        desc.append("Stav řízení: " + infosoud_url(j, courts))
        desc.append(
            "Údaje jsou platné ke dni zpracování přehledu soudem a v průběhu "
            "období se neaktualizují."
        )

        # Lámat se bude až při zápisu – kdyby se řádek zalomil i tady,
        # druhé lámání by počítalo „\r\n " jako obsah a rozsekalo ho podruhé.
        lines += [
            "SUMMARY:" + ics_escape(summary),
            "LOCATION:" + ics_escape(location),
            "DESCRIPTION:" + ics_escape("\n".join(desc)),
            "URL:" + infosoud_url(j, courts),
            "STATUS:CONFIRMED",
            "TRANSP:OPAQUE",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(ics_fold(x) for x in lines) + "\r\n")
    print(f"Kalendář: {count} IP jednání -> {path} (https://{site_host()}/hearings.ics)")
    return f"https://{site_host()}/hearings.ics"


def parse_kv(pairs):
    """„MS=a.docx" i „MS:spravni=b.pdf" -> {"MS": …, "MS:spravni": …}."""
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"Čekám SOUD[:ÚSEK]=cesta, dostal jsem: {p}")
        k, v = p.split("=", 1)
        soud, _, usek = k.partition(":")
        out[soud.upper() + (f":{usek}" if usek else "")] = v
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--local-jednani", nargs="*", metavar="SOUD[:ÚSEK]=CESTA",
                    help="místo stahování použít lokální přehled jednání "
                         "(např. MS:spravni=prehled.pdf)")
    ap.add_argument("--local-rozvrh", nargs="*", metavar="SOUD=CESTA",
                    help="místo stahování použít lokální rozvrh práce (PDF)")
    ap.add_argument("--force-rozvrh", action="store_true",
                    help="obnovit IP senáty z rozvrhů i mimo týdenní interval")
    args = ap.parse_args()
    local_jednani = parse_kv(args.local_jednani)
    local_rozvrh = parse_kv(args.local_rozvrh)

    config = load_json(CONFIG_FILE)
    if not config.get("courts"):
        sys.exit(f"Chybí {CONFIG_FILE} – bez seznamu IP senátů nemá běh smysl.")

    # 1) Případná obnova IP senátů z rozvrhů práce.
    updated_str = (config.get("updated") or "")[:10]
    try:
        stari = (date.today() - date.fromisoformat(updated_str)).days
    except ValueError:
        stari = ROZVRH_REFRESH_DAYS + 1
    refresh = args.force_rozvrh or local_rozvrh or stari >= ROZVRH_REFRESH_DAYS
    if refresh:
        print("Kontrola rozvrhů práce…")
        changed = False
        for court, cfg in config["courts"].items():
            try:
                if court in local_rozvrh:
                    with open(local_rozvrh[court], "rb") as f:
                        data = f.read()
                    changed |= update_rozvrh(config, court, data, local_rozvrh[court])
                    continue
                links = find_document_links(cfg["rozvrh_url"])
                href, text = pick_link(
                    links, keywords=("rozvrh",), prefer=("uplne zneni", "zmena"))
                if not href:
                    print(f"  [{court}] na stránce rozvrhu nejsou dokumenty")
                    continue
                data = http_get(href).content
                if data[:5] != b"%PDF-":
                    print(f"  [{court}] rozvrh není PDF ({text or href}) – přeskočeno")
                    continue
                changed |= update_rozvrh(config, court, data, href, text)
            except requests.RequestException as e:
                print(f"  [{court}] rozvrh nedostupný: {e}")
        if local_rozvrh:
            # Lokální běh je test extrakce – config (včetně ručně sepsaného
            # seznamu senátů) se z fixture souborů nepřepisuje.
            print("  (lokální rozvrh: config se neukládá)")
        else:
            # `updated` posunout i bez změny, ať se rozvrhy nezkouší každý běh.
            config["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            save_json(CONFIG_FILE, config)

    # 2) Přehledy jednání – soud jich může zveřejňovat víc (civilní úsek,
    #    správní úsek s žalobami proti ÚPV), každý jako vlastní dokument.
    output = load_json(OUTPUT_FILE)
    output["jednani"] = [j for j in output.get("jednani", [])
                         if isinstance(j, dict) and j.get("ip")]
    print("Přehledy jednání…")
    prehledy, selhaly, neuplne = [], [], []
    for court, cfg in config["courts"].items():
        for prehled in cfg.get("prehledy", [{}]):
            usek = prehled.get("usek", "")
            tag = f"{court}/{usek}" if usek else court
            local = local_jednani.get(f"{court}:{usek}") or (
                local_jednani.get(court) if len(cfg.get("prehledy", [{}])) == 1
                or usek == cfg.get("prehledy", [{}])[0].get("usek") else None)
            items, period, zdroj, stav = scrape_jednani(court, cfg, prehled, local)
            if stav == PREHLED_CHYBA:
                selhaly.append(tag)
            elif stav == PREHLED_NEUPLNY:
                neuplne.append(tag)
            if items is None:
                continue
            mark_ip(items, cfg)
            print(f"  [{tag}] v agendě IP: "
                  f"{sum(1 for it in items if it.get('ip'))} z {len(items)}")
            prehledy.append((court, cfg, items, period, zdroj, stav))

    if not prehledy:
        # Nic nového – hearings.json ani .ics se nepřepisují, ať se jen
        # kvůli časovým razítkům nezměnily a stará data se netvářila čerstvě.
        if selhaly:
            sys.exit(f"Nepodařilo se získat žádný přehled jednání "
                     f"(selhalo: {', '.join(selhaly)}) – výstup nechávám.")
        print("Žádný přehled jednání – výstup nechávám.")
        return

    # 3) Fyzické osoby na iniciály jednou za běh, nad IP jednáními ze všech
    #    přehledů a archivem najednou, s keší rozhodnutí z minulých běhů.
    print("Klasifikace fyzických osob…")
    kes = nacti_osoby_kes()
    redact_osoby([it for p in prehledy for it in p[2] if it.get("ip")],
                 output["jednani"], kes)
    uloz_osoby_kes(kes)

    for court, cfg, items, period, zdroj, stav in prehledy:
        merge_output(output, court, items, period, zdroj, cfg,
                     mazat=stav == PREHLED_OK, redigovat=False)

    # 4) Změny proti minulým přehledům držíme jen měsíc zpět. Nová jednání
    #    stránka mezi změnami neukazuje – starší záznamy tohoto typu pryč.
    output["zmeny"] = orez_zmeny([z for z in output.get("zmeny", [])
                                  if z.get("typ") != "nove"])

    zapis_sledovane(output, config)
    output["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    output["ics"] = write_ics(output)
    save_json(OUTPUT_FILE, output)
    print(f"Hotovo: {len(output['jednani'])} IP jednání -> {OUTPUT_FILE}")

    if selhaly:
        warning(f"Jednání: nepodařilo se získat {', '.join(selhaly)} – "
                "tyto úseky zůstávají ve stavu z minulého běhu")
    if selhaly or neuplne:
        # Až po zápisu, ať se neztratí, co se z ostatních přehledů získat
        # dalo. Workflow má u kroku continue-on-error a ohlásí to varováním.
        sys.exit(1)


if __name__ == "__main__":
    main()
