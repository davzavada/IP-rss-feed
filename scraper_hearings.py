#!/usr/bin/env python3
"""Kalendář jednání MSPH a VS Praha v civilním úseku – filtr na duševní
vlastnictví.

Oba soudy zveřejňují přehledy nařízených jednání jako dokumenty (MSPH .docx,
VS .pdf) na portálu justice. Tenhle scraper je stáhne, vytáhne z nich
jednotlivá jednání a nechá si ta, která patří IP senátům. Výstup jde do
docs/hearings.json, který čte kalendář na webu; ostatní jednání se zahodí,
ať se veřejně nerozepisují účastníci nesouvisejících sporů.

Které senáty jsou IP se bere z rozvrhů práce obou soudů. Rozvrhy se často
mění, proto je v hearings_config.json uložený aktuální seznam senátů a soudců
a scraper ho umí obnovit: stáhne rozvrh (PDF o stovkách stran), najde stránky
o duševním vlastnictví a nechá AI (Gemma) vytáhnout senáty a předsedy.
Když AI extrakce selže, zůstává v platnosti poslední známý seznam.

Filtr jde primárně přes senát ze spisové značky (např. „12 C" je na MSPH IP,
ale „12 Co" je odvolací neIP agenda) – samotné jméno soudce nestačí, protože
titíž soudci soudí i neIP rejstříky (EPR, ICm…). Jméno předsedy se používá
jen u rejstříku Nc, kde číslo senátu specializaci nerozlišuje.

Parsování přehledů je deterministické (tabulka v .docx, regex nad textem
.pdf); AI nastupuje jako záloha, kdyby soud změnil formát dokumentu.

Lokální testování bez přístupu k msp.gov.cz:
    python scraper_hearings.py --local-jednani MS=cesta.docx VS=cesta.pdf \
                               --local-rozvrh VS=rozvrh.pdf
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
import unicodedata
import zipfile
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from feed_common import gemini_enabled, gemini_generate_raw, load_json, save_json

CONFIG_FILE = "hearings_config.json"
OUTPUT_FILE = "docs/hearings.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8"}

# Jak staré termíny v hearings.json ještě držet (kalendář ukazuje i nedávnou
# minulost) a po kolika dnech zkusit obnovit IP senáty z rozvrhu práce.
# Proběhlá jednání se nemažou – kalendář slouží i jako archiv.
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


def pick_link(links, keywords, prefer=()):
    """Vybere odkaz na dokument podle klíčových slov v URL/textu (bez
    diakritiky). Slova v `prefer` se zkoušejí JEDNO PO DRUHÉM v pořadí, jak
    jsou zapsaná – stránka rozvrhu nese vedle úplného znění i jednotlivé
    změny a při společném průchodu by rozhodovalo jen pořadí v DOM."""
    for kws in ([(p,) for p in prefer] + [tuple(keywords)]):
        for href, text, low in links:
            if any(k in low for k in kws):
                return href, text
    return (links[0][0], links[0][1]) if links else (None, None)


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
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


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
    """„v období od 16.08.2026 do 31.08.2026" -> (iso_od, iso_do)."""
    m = re.search(
        r"od\s+(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})\s+do\s+(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})",
        text,
    )
    if not m:
        return None
    return tuple(czech_date_to_iso(x) for x in m.groups())


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
    sporu. Zalomení bez rozpoznatelné přípony („Zákupy-Brenná") rozlepené
    zůstane; to je jen kosmetika."""
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
FORM_RE = re.compile(
    r"(?:,?\s*(?:spol\.\s*s\s*r\.?\s*o\.?|s\.?\s*r\.?\s*o\.?|a\.?\s*s\.?|k\.?\s*s\.?"
    r"|v\.?\s*o\.?\s*s\.?|z\.?\s*s\.?|z\.?\s*ú\.?|o\.?\s*p\.?\s*s\.?|s\.?\s*p\.?"
    r"|GmbH|AG|SE|KG|LIMITED|Ltd\.?|LLC|Inc\.?|N\.V\.|B\.V\.|S\.L\.U\.|Corp\.?"
    r"|v\s+likvidaci|příspěvková\s+organizace|státní\s+podnik))+\s*$",
    re.IGNORECASE,
)
LEAD_TITLE_RE = re.compile(
    r"^(?:(?:JUDr|Mgr|Bc|Ing|MgA|PhDr|MUDr|RNDr|PaedDr|Dr|doc|prof)\.?\s+)+",
    re.IGNORECASE,
)


MAX_PARTY_LEN = 32


def short_party(name):
    """Zkrátí název strany pro popisek v kalendáři: „OSA z.s." -> „OSA",
    „Ing. Tomáš Seidl" -> „Tomáš Seidl".

    Kolektivní správci vystupují pod dlouhým názvem z rejstříku („INTERGRAM
    nezávislá společnost umělců a…"); ten se zkrátí na úvodní zkratku, a když
    žádná není, ořízne se na hranici slova.
    """
    s = LEAD_TITLE_RE.sub("", " ".join(str(name).split()))
    s = FORM_RE.sub("", s).strip(" ,-–") or " ".join(str(name).split())
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


def party_label(ucastnici):
    """Krátký popisek sporu ve tvaru „Xiaomi v. OSA".

    Přehledy soudů uvádějí účastníky jen jako plochý seznam bez rozlišení
    stran, takže první jméno bereme jako navrhovatele. Pokud hned následující
    jména sdílejí první slovo („Bayer AG", „Bayer Intellectual Property
    GmbH"), jde zjevně o tutéž stranu a spojí se dohromady; první odlišné
    jméno je protistrana, zbytek se schová do „a další".
    """
    parties = [short_party(u) for u in ucastnici if str(u).strip()]
    parties = [p for p in parties if p]
    if not parties:
        return ""
    if len(parties) == 1:
        return parties[0]

    def head(p):
        return p.split()[0].casefold() if p.split() else p.casefold()

    i = 1
    while i < len(parties) and head(parties[i]) == head(parties[0]):
        i += 1
    if i >= len(parties):          # všechna jména jsou jedna strana
        return parties[0]
    label = f"{parties[0]} v. {parties[i]}"
    return label + " a další" if len(parties) > i + 1 else label


def make_item(datum, sin, predseda, spz, hodina, ucastnici):
    cislo, rejstrik, bc, rocnik = spz.group(1), spz.group(2), spz.group(3), spz.group(4)
    strany = merge_participant_lines(ucastnici)
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
    "Odpověz POUZE platným JSON objektem bez dalšího textu ve tvaru:\n"
    '{{"senaty": [{{"senat": "<číslo> <rejstřík>", "predseda": "Jméno Příjmení"}}],\n'
    '  "soudci": ["Jméno Příjmení", ...]}}\n'
    "Do „senaty“ dej KAŽDOU kombinaci čísla soudního oddělení a rejstříku "
    "(C, EC, Cm, ECm, Co, Cmo…), pod kterou tato oddělení vedou spisové "
    "značky – např. oddělení 12 s rejstříky Cm, ECm, C, EC = čtyři položky "
    "„12 Cm“, „12 ECm“, „12 C“, „12 EC“. Rejstříky Nc a EVCm vynech. "
    "Do „soudci“ dej předsedy těchto senátů bez titulů. Nic si nevymýšlej."
)


def update_rozvrh(config, court, pdf_bytes, source_url):
    """Z rozvrhu práce (PDF) nechá AI vytáhnout IP senáty a soudce; při
    neúspěchu nechá dosavadní konfiguraci být."""
    cfg = config["courts"][court]
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    zdroj = cfg.get("rozvrh_zdroj") or {}
    if zdroj.get("hash") == digest:
        print(f"  [{court}] rozvrh práce beze změny (hash sedí)")
        return False
    if not gemini_enabled():
        print(f"  [{court}] rozvrh se změnil, ale AI je vypnutá – nechávám starý seznam")
        return False

    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []
    for i, page in enumerate(reader.pages):
        try:
            t = page.extract_text() or ""
        except Exception:
            continue
        if IP_KEYWORDS_RE.search(t):
            pages.append(f"--- strana {i + 1} ---\n{t}")
    if not pages:
        print(f"  [{court}] v rozvrhu nejsou stránky s IP klíčovými slovy – nechávám starý seznam")
        return False

    text = "\n\n".join(pages)[:80000]
    prompt = ROZVRH_AI_PROMPT.format(soud=cfg["nazev"])
    raw = gemini_generate_raw(prompt, text, max_tokens=8192)
    data = extract_json(raw)
    senaty, soudci_display = [], set()
    if isinstance(data, dict):
        for s in data.get("senaty", []):
            if not isinstance(s, dict):
                continue
            m = re.match(r"\s*(\d+)\s*([A-Za-z]+)", str(s.get("senat", "")))
            if m:
                senaty.append(senat_key(m.group(1), m.group(2)))
            p = TITLES_RE.sub(" ", str(s.get("predseda", "")))
            p = " ".join(re.sub(r"[.,;]", " ", p).split())
            if p:
                soudci_display.add(p)
        for j in data.get("soudci", []):
            j = " ".join(str(j).split())
            if j:
                soudci_display.add(j)
    senaty = sorted(set(senaty), key=lambda k: (int(k.split()[0]), k.split()[1]))
    soudci_display = sorted(soudci_display)
    if not senaty:
        print(f"  [{court}] AI z rozvrhu nic nevytáhla – nechávám starý seznam")
        return False

    cfg["senaty"] = senaty
    if soudci_display:
        cfg["soudci"] = soudci_display
    # `popis` je ruční poznámka, odkud seznam pochází – tu si neseme dál.
    cfg["rozvrh_zdroj"] = {
        "popis": (cfg.get("rozvrh_zdroj") or {}).get("popis"),
        "url": source_url,
        "hash": digest,
        "aktualizovano": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    config["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"  [{court}] IP senáty z rozvrhu obnoveny: {', '.join(senaty)}")
    return True


# --- Filtr IP ---

def mark_ip(items, cfg):
    """Označí jednání, která patří do agendy duševního vlastnictví.

    Tři pravidla, protože tři různé situace:

    1. Senát ze spisové značky je v seznamu IP senátů (běžné sporné věci).
    2. Rejstřík Nc (předběžná opatření, zajištění důkazu): číslo ve značce
       specializaci nerozlišuje – u obchodního „2 Nc" i civilního „1 Nc" je
       to číslo rejstříku, ne oddělení – takže rozhoduje předseda senátu.
       Tím se chytí PO v ochranných známkách i autorskoprávní PO.
    3. Správní senáty vyřizující žaloby proti Úřadu průmyslového vlastnictví
       (`senaty_ucastnik`): tyhle senáty soudí i běžnou správní agendu, takže
       samotné číslo senátu nestačí a hledá se ÚPV mezi účastníky. Když
       přehled u řádku účastníky neuvádí vůbec, bereme ho radši jako IP –
       přehlédnout jednání o známce je horší než jeden falešný poplach.
    """
    # Config se edituje i ručně, takže se na velikost písmen rejstříku
    # nespoléháme („12 ECm" i „12 Ecm" musí platit stejně).
    senaty = {str(s).casefold() for s in cfg.get("senaty", [])}
    podle_ucastnika = {str(s).casefold() for s in cfg.get("senaty_ucastnik", [])}
    vzory = [strip_diacritics(str(u)).casefold()
             for u in cfg.get("ucastnici_ip", []) if str(u).strip()]
    soudci = {normalize_judge(j) for j in cfg.get("soudci", [])}

    for it in items:
        senat = it["senat"].casefold()
        if senat in senaty:
            it["ip"] = True
        elif senat in podle_ucastnika:
            strany = strip_diacritics(" | ".join(it.get("ucastnici") or [])).casefold()
            it["ip"] = (not strany) or any(v in strany for v in vzory)
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


def expected_rows(text):
    """Kolik jednání dokument nejspíš obsahuje – počítá data ve tvaru
    DD.MM.RRRR, na kterých každý řádek přehledu začíná. Hlavička uvádí
    období dvěma daty, ta se odečtou."""
    n = len(DATE_RE.findall(text or ""))
    return max(0, n - (2 if parse_period(text or "") else 0))


def scrape_jednani(court, cfg, prehled, local_file=None):
    """Stáhne (nebo načte lokálně) jeden přehled jednání a naparsuje ho.

    Vrací (items, period, zdroj_url); items je None, když se nepodařilo
    získat vůbec nic – volající pak nechá dosavadní data být.

    Kromě parsování hlídá i jeho úplnost: když se z dokumentu naparsuje
    výrazně méně řádků, než kolik je v něm dat, jde nejspíš o změnu formátu
    a nastupuje AI záloha. Bez téhle kontroly by se částečné selhání
    (např. přejmenovaný sloupec) projevilo jen tak, že by jednání tiše
    zmizela.
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
            )
            if not href:
                print(f"  [{tag}] na stránce nejsou odkazy na dokumenty")
                return None, None, None
            print(f"  [{tag}] stahuji: {text or href}")
            data = http_get(href).content
            zdroj = href
        except requests.RequestException as e:
            print(f"  [{tag}] stažení selhalo: {e}")
            return None, None, None

    is_docx = data[:2] == b"PK"
    is_pdf = data[:5] == b"%PDF-"
    if not (is_docx or is_pdf):
        print(f"  [{tag}] neznámý formát dokumentu – přeskočeno")
        return None, None, None

    items, period = [], None
    try:
        items, period = (parse_jednani_docx(data) if is_docx
                         else parse_jednani_pdf(data))
    except Exception as e:
        print(f"  [{tag}] deterministické parsování spadlo: {e}")

    items = [it for it in items if it.get("datum")]
    text = raw_text_of(data)
    ocekavano = expected_rows(text)
    chybi = ocekavano and len(items) < ocekavano * MIN_PARSE_RATIO
    if chybi:
        print(f"  [{tag}] POZOR: naparsováno {len(items)} z ~{ocekavano} "
              f"řádků – dokument nejspíš změnil formát")

    if (not items or chybi) and gemini_enabled():
        print(f"  [{tag}] zkouším AI parsování")
        try:
            ai_items = [it for it in parse_jednani_ai(text) if it.get("datum")]
            if len(ai_items) > len(items):
                items = ai_items
                print(f"  [{tag}] AI naparsovala {len(items)} jednání")
        except Exception as e:
            print(f"  [{tag}] AI parsování selhalo: {e}")

    for it in items:
        it["usek"] = usek
    print(f"  [{tag}] jednání: {len(items)}/{ocekavano or '?'}"
          + (f", období {period[0]} – {period[1]}" if period else ""))
    return (items or None), period, zdroj


def merge_output(existing, court, items, period, zdroj_url, cfg):
    """Zanese nový přehled do výstupu.

    Ukládají se jen jednání v agendě duševního vlastnictví. Ostatní věci
    soud v přehledu zveřejňuje také, ale do tohohle archivu nepatří a není
    důvod rozepisovat jejich účastníky.

    Jednání se nikdy nemažou – kalendář je archiv, takže proběhlé termíny
    zůstávají. Když ale jednání zmizí z nově vydaného přehledu, který jeho
    den pokrývá, soud ho mezitím odvolal nebo přeložil; takový záznam se
    označí `zruseno` a v kalendáři se jen přeškrtne. Že jde o odvolání
    odvozené ze zmizení řádku, ne z InfoSoudu, si uživatel ověří odkazem
    na detail řízení.
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
    od, do = period if period else (None, None)

    zachovane = []
    for j in jednani:
        stejny_zdroj = j.get("soud") == court and j.get("usek", "") == usek
        if stejny_zdroj and (j.get("spz"), j.get("datum")) in v_prehledu:
            # Přepíše ho čerstvá verze níže – a pokud už IP není, vypadne.
            continue
        if (stejny_zdroj and period
                and od <= (j.get("datum") or "") <= do):
            # Den spadá do nového přehledu, ale jednání v něm není.
            j["zruseno"] = True
        zachovane.append(j)

    for it in items:
        it["soud"] = court
        it["zruseno"] = False
    jednani = zachovane + items
    jednani.sort(key=lambda j: (j.get("datum") or "", j.get("hodina") or "", j.get("spz") or ""))
    existing["jednani"] = jednani

    courts = existing.setdefault("courts", {})
    meta = courts.setdefault(court, {})
    meta["nazev"] = cfg["nazev"]
    meta["infosoud_org"] = cfg["infosoud_org"]
    # Každý úsek má vlastní dokument, a tedy i vlastní období a čas stažení.
    meta.setdefault("useky", {})[usek] = {
        "obdobi": {"od": period[0], "do": period[1]} if period else None,
        "stazeno": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "zdroj": zdroj_url,
    }


# --- iCalendar export (přihlášení v Google Kalendáři přes URL) ---

# Google si externí kalendář tahá sám, jednou za několik hodin; proto stačí,
# že soubor leží vedle stránky na GitHub Pages.
ICS_FILE = "docs/hearings.ics"
CNAME_FILE = "docs/CNAME"
ICS_DEFAULT_HOST = "rss.davidzavada.cz"

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
    try:
        with open(CNAME_FILE, encoding="utf-8") as f:
            host = f.read().strip()
        return host or ICS_DEFAULT_HOST
    except OSError:
        return ICS_DEFAULT_HOST


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



# --- Stav řízení z InfoSoudu ------------------------------------------

# InfoSoud je veřejná služba justice, ale je to jeden server – chodíme na něj
# po jednom a jen kvůli jednáním, u kterých se stav ještě může měnit.
INFOSOUD_PAUZA = 1.0          # vteřin mezi dotazy
INFOSOUD_MAX_DOTAZU = 40      # kolik řízení nejvýš obnovit v jednom běhu
INFOSOUD_OBNOVA_DNU = 21      # jak starý stav se bere jako čerstvý
INFOSOUD_OKNO_DNU = 60        # jak dlouho po jednání stav ještě sledovat
INFOSOUD_MAX_RADKU = 60       # strop na velikost uložené tabulky
INFOSOUD_MAX_BUNKA = 400      # strop na délku textu v buňce

DATUM_V_TEXTU_RE = re.compile(r"\b\d{1,2}\.\s*\d{1,2}\.\s*\d{4}\b")


def cell_text(el):
    """Text buňky včetně částí, které stránka schovává za proklik – právě
    v nich InfoSoud drží popisy úkonů."""
    t = " ".join(el.get_text(" ", strip=True).split())
    return t[:INFOSOUD_MAX_BUNKA]


def table_heading(table):
    """Nadpis nad tabulkou – nejbližší předchozí nadpis nebo <caption>."""
    caption = table.find("caption")
    if caption:
        return cell_text(caption)
    el = table
    for _ in range(6):
        el = el.find_previous(["h1", "h2", "h3", "h4", "h5", "legend", "caption"])
        if el is None:
            return ""
        text = cell_text(el)
        if text:
            return text[:120]
    return ""


def read_table(table):
    """Tabulku převede na {nadpis, hlavicka, radky}. Vrací None, když to
    není tabulka s daty (rozvržení stránky, prázdná, jednosloupcová)."""
    if table.find("table"):
        return None                       # obal rozvržení, ne data
    radky = []
    for tr in table.find_all("tr"):
        bunky = [cell_text(td) for td in tr.find_all(["td", "th"])]
        if any(b for b in bunky):
            radky.append(bunky)
    if len(radky) < 2 or max(len(r) for r in radky) < 2:
        return None
    # Záhlaví: buď <th>, nebo první řádek, ve kterém není datum.
    prvni = table.find("tr")
    ma_th = bool(prvni and prvni.find("th"))
    hlavicka = []
    if ma_th or not DATUM_V_TEXTU_RE.search(" ".join(radky[0])):
        hlavicka = radky[0]
        radky = radky[1:]
    if not radky:
        return None
    sirka = max(len(r) for r in radky + ([hlavicka] if hlavicka else []))
    dorovnat = lambda r: r + [""] * (sirka - len(r))
    return {
        "nadpis": table_heading(table),
        "hlavicka": dorovnat(hlavicka) if hlavicka else [],
        "radky": [dorovnat(r) for r in radky[:INFOSOUD_MAX_RADKU]],
    }


def parse_infosoud(html):
    """Vytáhne z detailu řízení tabulky tak, jak jsou – jejich podobu
    neznáme dopředu a InfoSoud ji může změnit, tak se nic nepřejmenovává.
    Nejdřív jdou tabulky s daty (průběh řízení), pak zbytek."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    tabulky = [t for t in (read_table(t) for t in soup.find_all("table")) if t]
    if not tabulky:
        return None
    def datumu(t):
        return sum(1 for r in t["radky"] if DATUM_V_TEXTU_RE.search(" ".join(r)))
    tabulky.sort(key=datumu, reverse=True)
    return tabulky[:3]


def popis_stranky(j, url, html):
    """Když se z odpovědi nedá nic vytáhnout, popiš, co vlastně přišlo –
    na InfoSoud se z vývojového prostředí nedá dosáhnout, tak je log
    jediná cesta, jak zjistit, čím se liší od očekávání."""
    soup = BeautifulSoup(html, "html.parser")
    titulek = cell_text(soup.title) if soup.title else ""
    telo = cell_text(soup.body) if soup.body else cell_text(soup)
    tabulek = len(soup.find_all("table"))
    radku = len(soup.find_all("tr"))
    print(f"  [diag] {j.get('spz')} – {url}")
    print(f"  [diag] {len(html)} znaků, titulek: {titulek!r}, "
          f"tabulek: {tabulek}, řádků: {radku}, "
          f"divů s class: {len(soup.select('div[class]'))}")
    print(f"  [diag] text: {telo[:400]!r}")
    # Prázdná stránka s nulou tabulek = slupka javascriptové aplikace.
    # Data si dotahuje odjinud, tak vypíšeme, co v té slupce je.
    skripty = [s.get("src") for s in soup.find_all("script") if s.get("src")]
    print(f"  [diag] skripty: {skripty[:10]}")
    inline = " ".join(s.get_text() for s in soup.find_all("script") if not s.get("src"))
    adresy = sorted(set(re.findall(
        r"""['"](https?://[^'"\s]{6,}|/[A-Za-z0-9_\-./]{4,})['"]""", inline)))
    print(f"  [diag] adresy v kódu: {adresy[:20]}")


def potrebuje_stav(j, dnes):
    """Stav řízení obnovujeme jen u jednání, kde se ještě může měnit."""
    if not (j.get("cislo_senatu") and j.get("rejstrik") and j.get("bc") and j.get("rocnik")):
        return False
    try:
        stari_jednani = (dnes - date.fromisoformat(j["datum"])).days
    except (TypeError, ValueError):
        return False
    if stari_jednani > INFOSOUD_OKNO_DNU:
        return False
    stav = j.get("stav") or {}
    try:
        stazeno = datetime.fromisoformat(stav["stazeno"]).date()
    except (KeyError, TypeError, ValueError):
        return True
    return (dnes - stazeno).days >= INFOSOUD_OBNOVA_DNU


def update_stav(output):
    """Doplní k jednáním tabulku průběhu řízení z InfoSoudu."""
    courts = output.get("courts", {})
    dnes = date.today()
    fronta = [j for j in output.get("jednani", []) if potrebuje_stav(j, dnes)]
    # Napřed ta, která stav ještě nemají, pak podle stáří jednání.
    fronta.sort(key=lambda j: (bool(j.get("stav")), j.get("datum") or ""))
    fronta = fronta[:INFOSOUD_MAX_DOTAZU]
    if not fronta:
        print("InfoSoud: stav řízení je aktuální, nic se nestahuje")
        return
    print(f"InfoSoud: stav řízení u {len(fronta)} jednání…")
    ok = chyby = 0
    diag = 2                     # u prvních dvou neúspěchů popíšeme stránku
    for i, j in enumerate(fronta):
        url = infosoud_url(j, courts)
        try:
            html = http_get(url, timeout=30).text
            tabulky = parse_infosoud(html)
            if not tabulky and diag:
                diag -= 1
                popis_stranky(j, url, html)
        except requests.RequestException as e:
            chyby += 1
            print(f"  [{j.get('spz')}] nedostupné: {e}")
            tabulky = None
        except Exception as e:                       # rozbitá stránka
            chyby += 1
            print(f"  [{j.get('spz')}] nepřečteno: {type(e).__name__}: {e}")
            tabulky = None
        if tabulky:
            ok += 1
            j["stav"] = {
                "stazeno": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "url": url,
                "tabulky": tabulky,
            }
        if i + 1 < len(fronta):
            time.sleep(INFOSOUD_PAUZA)
    print(f"InfoSoud: staženo {ok}, nepovedlo se {chyby}")
    if fronta and not ok:
        print("::warning::InfoSoud nevrátil ani jednu tabulku – "
              "nejspíš změnil podobu stránky")


def write_ics(output, path=None):
    """Zapíše IP jednání jako iCalendar – na tenhle soubor se dá přihlásit
    v Google Kalendáři (Jiné kalendáře → Přidat → Z adresy URL)."""
    path = path or ICS_FILE
    courts = output.get("courts", {})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    host = site_host()

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
        uid = hashlib.sha1(
            f"{j.get('soud')}|{j.get('spz')}|{j['datum']}|{j.get('hodina')}"
            .encode("utf-8")
        ).hexdigest()
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
        if j.get("zruseno"):
            summary = "ZRUŠENO: " + summary
        location = court + (f", jednací síň {j['sin']}" if j.get("sin") else "")
        desc = [f"Spisová značka: {j.get('spz', '')}"]
        if j.get("predseda"):
            desc.append(f"Předseda senátu: {j['predseda']}")
        if j.get("senat"):
            desc.append(f"Senát: {j['senat']}")
        if j.get("ucastnici"):
            desc.append("Účastníci: " + "; ".join(j["ucastnici"]))
        desc.append("Stav řízení: " + infosoud_url(j, courts))
        desc.append(
            "Údaje jsou platné ke dni zpracování přehledu soudem a v průběhu "
            "období se neaktualizují."
        )

        lines += [
            ics_fold("SUMMARY:" + ics_escape(summary)),
            ics_fold("LOCATION:" + ics_escape(location)),
            ics_fold("DESCRIPTION:" + ics_escape("\n".join(desc))),
            ics_fold("URL:" + infosoud_url(j, courts)),
            # Odvolané jednání se z kalendáře nemaže, jen zešedne –
            # v Google i Apple Kalendáři je vidět jako zrušené.
            "STATUS:CANCELLED" if j.get("zruseno") else "STATUS:CONFIRMED",
            "TRANSP:TRANSPARENT" if j.get("zruseno") else "TRANSP:OPAQUE",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(ics_fold(x) for x in lines) + "\r\n")
    print(f"Kalendář: {count} IP jednání -> {path} (https://{host}/hearings.ics)")
    return f"https://{host}/hearings.ics"


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
                changed |= update_rozvrh(config, court, data, href)
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
    # Dřívější běhy ukládaly celý přehled; archiv drží jen IP agendu.
    ulozena = [j for j in output.get("jednani", []) if isinstance(j, dict)]
    output["jednani"] = [j for j in ulozena if j.get("ip")]
    if len(output["jednani"]) != len(ulozena):
        print(f"Z archivu odebráno {len(ulozena) - len(output['jednani'])} "
              f"jednání mimo IP agendu.")
    ok = False
    print("Přehledy jednání…")
    for court, cfg in config["courts"].items():
        for prehled in cfg.get("prehledy", [{}]):
            usek = prehled.get("usek", "")
            local = local_jednani.get(f"{court}:{usek}") or (
                local_jednani.get(court) if len(cfg.get("prehledy", [{}])) == 1
                or usek == cfg.get("prehledy", [{}])[0].get("usek") else None)
            items, period, zdroj = scrape_jednani(court, cfg, prehled, local)
            if items is None:
                continue
            mark_ip(items, cfg)
            merge_output(output, court, items, period, zdroj, cfg)
            ok = True

    if not ok and not output.get("jednani"):
        sys.exit("Nepodařilo se získat žádná jednání.")

    # 3) Stav řízení z InfoSoudu k jednáním, u kterých se ještě může měnit.
    update_stav(output)

    # Seznam senátů a soudců do výstupu – kalendář je ukazuje u filtru.
    output["senaty"] = {c: cfg.get("senaty", []) for c, cfg in config["courts"].items()}
    output["soudci"] = {c: cfg.get("soudci", []) for c, cfg in config["courts"].items()}
    output["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    output["ics"] = write_ics(output)
    save_json(OUTPUT_FILE, output)
    print(f"Hotovo: {len(output['jednani'])} IP jednání -> {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
