#!/usr/bin/env python3
"""Kalendář jednání MSPH a VS Praha v civilním úseku – filtr na duševní
vlastnictví.

Oba soudy zveřejňují přehledy nařízených jednání jako dokumenty (MSPH .docx,
VS .pdf) na portálu justice. Tenhle scraper je stáhne, vytáhne z nich
jednotlivá jednání a označí ta, která patří IP senátům. Výstup jde do
docs/hearings.json, který čte kalendář na webu.

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
KEEP_PAST_DAYS = 60
ROZVRH_REFRESH_DAYS = 7

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


def senat_key(cislo, rejstrik):
    """Klíč senátu: číslo + rejstřík v jednotné velikosti („12 C", „3 Cmo")."""
    return f"{int(cislo)} {rejstrik.capitalize()}"


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
    """Vybere první odkaz, jehož URL/text obsahuje některé z klíčových slov
    (bez diakritiky). `prefer` mají přednost."""
    for kws in (prefer, keywords):
        if not kws:
            continue
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
    """Přehled VS: text po řádcích; záznam začíná datem, spisová značka
    s hodinou ho dělí na hlavičku (síň, předseda) a účastníky. Účastníci
    můžou pokračovat na dalších řádcích až do dalšího data."""
    text = pdf_text(data)
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
    """Slepí zalomené/oddělené kusy jmen účastníků: řádek začínající malým
    písmenem („s r.o.", „z.ú.", „a.s.") je pokračování předchozího jména.
    Nepřesnosti u zalomení uprostřed velkých písmen (samotné „GmbH") jsou
    jen kosmetické."""
    out = []
    for ln in lines:
        ln = " ".join(str(ln).split())
        if not ln:
            continue
        if out and (ln[0].islower() or ln.startswith(("&", "-"))):
            out[-1] += " " + ln
        else:
            out.append(ln)
    return out


def make_item(datum, sin, predseda, spz, hodina, ucastnici):
    cislo, rejstrik, bc, rocnik = spz.group(1), spz.group(2), spz.group(3), spz.group(4)
    return {
        "datum": czech_date_to_iso(datum),
        "hodina": hodina or "",
        "sin": sin,
        "predseda": predseda,
        "spz": f"{int(cislo)} {rejstrik.capitalize()} {int(bc)}/{rocnik}",
        "senat": senat_key(cislo, rejstrik),
        "rejstrik": rejstrik.capitalize(),
        "cislo_senatu": int(cislo),
        "bc": int(bc),
        "rocnik": int(rocnik),
        "ucastnici": merge_participant_lines(ucastnici),
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
    cfg["rozvrh_zdroj"] = {
        "url": source_url,
        "hash": digest,
        "aktualizovano": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    config["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"  [{court}] IP senáty z rozvrhu obnoveny: {', '.join(senaty)}")
    return True


# --- Filtr IP ---

def mark_ip(items, cfg):
    senaty = set(cfg.get("senaty", []))
    soudci = {normalize_judge(j) for j in cfg.get("soudci", [])}
    for it in items:
        if it["senat"] in senaty:
            it["ip"] = True
        elif it["rejstrik"] == "Nc":
            # U Nc číslo senátu specializaci nerozlišuje – rozhoduje předseda.
            it["ip"] = normalize_judge(it["predseda"]) in soudci
        else:
            it["ip"] = False
    return items


# --- Hlavní běh ---

def scrape_court_jednani(court, cfg, local_file=None):
    """Stáhne (nebo načte lokálně) přehled jednání soudu a naparsuje ho.
    Vrací (items, period, zdroj_url) nebo (None, None, None) při neúspěchu."""
    data, zdroj = None, None
    if local_file:
        with open(local_file, "rb") as f:
            data = f.read()
        zdroj = os.path.basename(local_file)
        print(f"  [{court}] lokální dokument: {local_file}")
    else:
        try:
            links = find_document_links(cfg["jednani_url"])
            href, text = pick_link(
                links,
                keywords=("jednani", "prehled"),
                prefer=("civil", " cu", "cu.", "cu_", "_cu"),
            )
            if not href:
                print(f"  [{court}] na stránce nejsou odkazy na dokumenty")
                return None, None, None
            print(f"  [{court}] stahuji: {text or href}")
            data = http_get(href).content
            zdroj = href
        except requests.RequestException as e:
            print(f"  [{court}] stažení selhalo: {e}")
            return None, None, None

    is_docx = data[:2] == b"PK"
    is_pdf = data[:5] == b"%PDF-"
    items, period = [], None
    try:
        if is_docx:
            items, period = parse_jednani_docx(data)
        elif is_pdf:
            items, period = parse_jednani_pdf(data)
    except Exception as e:
        print(f"  [{court}] deterministické parsování spadlo: {e}")

    if not items and gemini_enabled():
        print(f"  [{court}] zkouším AI parsování")
        try:
            text = pdf_text(data) if is_pdf else docx_tables(data)[1]
            items = parse_jednani_ai(text)
        except Exception as e:
            print(f"  [{court}] AI parsování selhalo: {e}")
    items = [it for it in items if it["datum"]]
    print(f"  [{court}] jednání: {len(items)}" + (f", období {period[0]} – {period[1]}" if period else ""))
    return (items or None), period, zdroj


def merge_output(existing, court, items, period, zdroj_url, cfg):
    """Vloží nová jednání do výstupu: uvnitř období dokumentu nahradí vše
    (zrušená jednání z novějšího přehledu zmizí), mimo období ponechá."""
    jednani = [j for j in existing.get("jednani", []) if isinstance(j, dict)]
    if period:
        od, do = period
        jednani = [
            j for j in jednani
            if not (j.get("soud") == court and od <= (j.get("datum") or "") <= do)
        ]
    else:
        stare = {(j.get("spz"), j.get("datum")) for j in items}
        jednani = [
            j for j in jednani
            if not (j.get("soud") == court and (j.get("spz"), j.get("datum")) in stare)
        ]
    for it in items:
        it["soud"] = court
    jednani.extend(items)

    cutoff = (date.today() - timedelta(days=KEEP_PAST_DAYS)).isoformat()
    jednani = [j for j in jednani if (j.get("datum") or "") >= cutoff]
    jednani.sort(key=lambda j: (j.get("datum") or "", j.get("hodina") or "", j.get("spz") or ""))
    existing["jednani"] = jednani

    courts = existing.setdefault("courts", {})
    courts[court] = {
        "nazev": cfg["nazev"],
        "infosoud_org": cfg["infosoud_org"],
        "obdobi": {"od": period[0], "do": period[1]} if period else None,
        "stazeno": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "zdroj": zdroj_url,
    }


def parse_kv(pairs):
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"Čekám SOUD=cesta, dostal jsem: {p}")
        k, v = p.split("=", 1)
        out[k.upper()] = v
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--local-jednani", nargs="*", metavar="SOUD=CESTA",
                    help="místo stahování použít lokální přehled jednání")
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
        # `updated` posunout i bez změny, ať se rozvrhy nezkouší každý běh.
        if not local_rozvrh:
            config["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_json(CONFIG_FILE, config)

    # 2) Přehledy jednání.
    output = load_json(OUTPUT_FILE)
    ok = False
    print("Přehledy jednání…")
    for court, cfg in config["courts"].items():
        items, period, zdroj = scrape_court_jednani(
            court, cfg, local_jednani.get(court))
        if items is None:
            continue
        mark_ip(items, cfg)
        merge_output(output, court, items, period, zdroj, cfg)
        ok = True

    if not ok and not output.get("jednani"):
        sys.exit("Nepodařilo se získat žádná jednání.")

    # Seznam senátů a soudců do výstupu – kalendář je ukazuje u filtru.
    output["senaty"] = {c: cfg.get("senaty", []) for c, cfg in config["courts"].items()}
    output["soudci"] = {c: cfg.get("soudci", []) for c, cfg in config["courts"].items()}
    output["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_json(OUTPUT_FILE, output)
    ip_count = sum(1 for j in output["jednani"] if j.get("ip"))
    print(f"Hotovo: {len(output['jednani'])} jednání, z toho {ip_count} IP -> {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
