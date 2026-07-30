#!/usr/bin/env python3
"""Scraper rozhodnutí senátu 23 Cdo NS ČR – generuje RSS feed.

Spojuje dva zdroje:
  1. Úřední deska (www.nsoud.cz) – vyhlašovaná rozhodnutí (jen rozsudky).
  2. Databáze judikatury (rozhodnuti.nsoud.cz) – všechna zveřejněná
     rozhodnutí senátu 23 Cdo včetně usnesení, která na úřední desce nejsou.

Položky se deduplikují podle spisové značky.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from xml.etree.ElementTree import Element, SubElement, ElementTree, indent

import requests
from bs4 import BeautifulSoup

from feed_common import (
    JUDIKATURA_PROMPT,
    gemini_enabled,
    gemini_summarize_pdf,
)

# --- Zdroj 1: úřední deska ---
URL = "https://www.nsoud.cz/uredni-deska/obcanskopravni-a-obchodni-kolegium/vyhlasovana-rozhodnuti"
BASE_URL = "https://www.nsoud.cz"

# --- Zdroj 2: databáze judikatury (Lotus Domino fulltext search) ---
JUDIKATURA_URL = "https://rozhodnuti.nsoud.cz/judikatura/judikatura_ns.nsf/$$WebSearch1"
SENAT_NUM = "23"      # pole [spzn1] – číslo senátu
REJSTRIK = "cdo"      # pole [spzn2] – rejstřík (cdo = civilní dovolání)
JUDIKATURA_DAYS = 30  # okno [datum_predani_na_web] >= dnes - N dní

SENAT = "23 Cdo"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
JUDIKATURA_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
    "Referer": "https://rozhodnuti.nsoud.cz/",
}
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "feed.xml")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed_seen.json")
# Cache metadat detailu judikatury podle UNID (datum zveřejnění, heslo, …)
META_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed_meta.json")


JUDIKATURA_HOST = "https://rozhodnuti.nsoud.cz"

# AI shrnutí rozhodnutí používá sdílený Gemma klient z feed_common
# (JUDIKATURA_PROMPT, gemini_summarize_pdf) – viz feed_common.py.


def normalize_case(case_number):
    """Sjednotí mezery ve spisové značce pro porovnání/deduplikaci."""
    return re.sub(r"\s+", " ", case_number).strip()


def abs_url(href):
    """Doplní host k relativní cestě a zakóduje mezery (syrový Domino výstup)."""
    if not href:
        return ""
    if href.startswith("/"):
        href = JUDIKATURA_HOST + href
    return href.replace(" ", "%20")


# --- Stav: kdy jsme položku poprvé viděli (pro datum u judikatury) ---

def load_seen():
    """Načte {identifikátor: iso_datum_prvniho_vyskytu}."""
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_seen(seen):
    """Uloží stav, vyhodí záznamy starší 120 dní."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=120)
    pruned = {
        ident: ts for ident, ts in seen.items()
        if datetime.fromisoformat(ts) >= cutoff
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(pruned, f, ensure_ascii=False, indent=2)


# --- Zdroj 1: úřední deska ---

def fetch_uredni_deska():
    """Stáhne úřední desku a vrátí rozhodnutí senátu 23 Cdo."""
    resp = requests.get(URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    decisions = []
    for row in soup.select("table tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue

        case_normalized = normalize_case(cells[0].get_text(strip=True))
        if SENAT not in case_normalized:
            continue

        date_text = cells[1].get_text(strip=True)

        pdf_link = ""
        link_tag = row.find("a", href=True)
        if link_tag:
            href = link_tag["href"]
            pdf_link = href if href.startswith("http") else BASE_URL + href

        decisions.append({
            "case_number": case_normalized,
            "date": date_text,
            "pdf_url": pdf_link,
            "detail_url": "",
            "category": "",
            "unid": "",
            "source": "uredni",
        })

    return decisions


# --- Zdroj 2: databáze judikatury ---

def fetch_judikatura(days=JUDIKATURA_DAYS):
    """Stáhne rozhodnutí senátu 23 Cdo z databáze judikatury (vč. usnesení)."""
    date_from = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d.%m.%Y")
    query = (
        f"[spzn1] = {SENAT_NUM} AND [spzn2]={REJSTRIK} "
        f"AND [datum_predani_na_web]>={date_from}"
    )
    url = (
        f"{JUDIKATURA_URL}?SearchView&Query={quote(query)}"
        f"&SearchMax=1000&SearchOrder=4&Start=0&Count=200&pohled=1"
    )

    # Domino může vyžadovat session cookie – nejprve navštívíme úvodní stránku.
    session = requests.Session()
    try:
        session.get("https://rozhodnuti.nsoud.cz/", headers=JUDIKATURA_HEADERS, timeout=30)
    except Exception:
        pass

    resp = session.get(url, headers=JUDIKATURA_HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Pozn.: syrová odpověď Domina nemá <tbody>, proto selektujeme řádky přímo
    # pod tabulkou; hlavičkový řádek (<th>, bez a.odk) se přeskočí níže.
    rows = soup.select("table#tabl tr")
    if not rows:
        # Diagnostika – proč nejsou výsledky
        text = resp.text
        print(f"    [diag] HTTP {resp.status_code}, finální URL: {resp.url}")
        print(f"    [diag] délka odpovědi: {len(text)} B, tabulek: {len(soup.find_all('table'))}")
        print(f"    [diag] 'id=\"tabl\"' přítomno: {'tabl' in text}; "
              f"'Výsledky' přítomno: {'Výsledky' in text}")
        snippet = re.sub(r'\s+', ' ', text[:600])
        print(f"    [diag] začátek: {snippet}")

    decisions = []
    for row in rows:
        link = row.select_one("a.odk")
        if not link:
            continue

        case_number = normalize_case(link.get_text(strip=True))
        detail_url = abs_url(link.get("href", ""))

        pdf_url = ""
        rtf_url = ""
        for a in row.select("td.icons a[href]"):
            href = a["href"]
            low = href.lower()
            if ".pdf?openelement" in low or low.endswith(".pdf"):
                pdf_url = abs_url(href)
            elif ".rtf?openelement" in low or low.endswith(".rtf"):
                rtf_url = abs_url(href)

        cat_el = row.select_one("td.category")
        category = cat_el.get_text(strip=True) if cat_el else ""

        cb = row.select_one("input[name='ids']")
        unid = cb.get("value", "") if cb else ""

        decisions.append({
            "case_number": case_number,
            "date": "",  # výpis datum neobsahuje – řeší se prvním výskytem
            "pdf_url": pdf_url or detail_url,
            "detail_url": detail_url,
            "rtf_url": rtf_url,
            "category": category,
            "unid": unid,
            "source": "judikatura",
        })

    return decisions


# --- Detail rozhodnutí: datum zveřejnění a Heslo ---

def load_meta():
    """Načte cache metadat detailu {unid: {...}}."""
    if not os.path.exists(META_FILE):
        return {}
    with open(META_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_meta(meta):
    """Uloží cache metadat."""
    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def _label(text):
    """Normalizuje popisek z levého sloupce (bez koncové dvojtečky)."""
    return re.sub(r"\s+", " ", text).strip().rstrip(":").strip()


def _cz_date_to_iso(text):
    """'20. 5. 2026' -> '2026-05-20'. Vrátí '' když se nepodaří."""
    m = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})", text)
    if not m:
        return ""
    day, month, year = (int(x) for x in m.groups())
    return f"{year:04d}-{month:02d}-{day:02d}"


def parse_detail(html):
    """Z detailu rozhodnutí vytáhne metadata z tabulky popisek/hodnota."""
    soup = BeautifulSoup(html, "html.parser")
    info = {}
    for row in soup.select("table#tabl tr"):
        left = row.select_one("td.left-part")
        right = row.select_one("td.right-part")
        if not left or not right:
            continue
        label = _label(left.get_text(" ", strip=True))
        value = re.sub(r"\s+", " ", right.get_text(" ", strip=True)).strip()
        if label:
            info[label] = value
    return {
        "published": _cz_date_to_iso(info.get("Zveřejněno na webu", "")),
        "decided": _cz_date_to_iso(info.get("Datum rozhodnutí", "")),
        "heslo": info.get("Heslo", ""),
        "typ": info.get("Typ rozhodnutí", ""),
    }


def enrich_metadata(decisions):
    """Doplní judikaturním rozhodnutím datum zveřejnění, Heslo a typ z detailu.

    Metadata se cachují podle UNID, takže detail se stahuje jen u nových.
    Levné – běží před filtrem na 2 týdny (kvůli datu zveřejnění do pub_dt).
    """
    meta = load_meta()
    session = requests.Session()
    fetched = 0

    for d in decisions:
        unid = d.get("unid")
        if not unid:
            continue
        if unid not in meta:
            detail_url = d.get("detail_url") or (
                f"{JUDIKATURA_HOST}/Judikatura/judikatura_ns.nsf/WebSearch/{unid}?openDocument"
            )
            try:
                r = session.get(detail_url, headers=JUDIKATURA_HEADERS, timeout=30)
                r.raise_for_status()
                meta[unid] = parse_detail(r.text)
                fetched += 1
            except Exception as e:
                print(f"    CHYBA detailu {d['case_number']}: {e}")
                continue  # necachujeme, příště zkusíme znovu

        m = meta[unid]
        d["published"] = m.get("published", "")
        d["decided"] = m.get("decided", "")
        d["heslo"] = m.get("heslo", "")
        d["typ"] = m.get("typ", "")
        d["summary"] = m.get("summary", "")
        d["tag"] = m.get("tag", "")

    save_meta(meta)
    if fetched:
        print(f"    Staženo {fetched} nových detailů (datum zveřejnění + Heslo)")
    return decisions


def enrich_summaries(decisions):
    """Vygeneruje AI shrnutí přes Gemini – pro předané (už filtrované) položky
    s PDF, které shrnutí ještě nemají.

    Cachuje se podle UNID; když UNID není (čerstvě vyhlášené rozhodnutí jen
    z úřední desky, které ještě není v databázi judikatury), podle spisové
    značky – jinak by takové položky shrnutí nikdy nedostaly.
    """
    if not gemini_enabled():
        return decisions

    meta = load_meta()
    session = requests.Session()
    summarized = 0
    gemini_calls = 0

    for d in decisions:
        key = d.get("unid") or normalize_case(d["case_number"])
        if not key or not d.get("pdf_url"):
            continue
        m = meta.setdefault(key, {})
        if m.get("summary"):
            d["summary"] = m.get("summary", "")
            d["tag"] = m.get("tag", "")
            continue
        try:
            pr = session.get(d["pdf_url"], headers=JUDIKATURA_HEADERS, timeout=60)
            pr.raise_for_status()
            gemini_calls += 1
            summary, tag = gemini_summarize_pdf(pr.content, JUDIKATURA_PROMPT)
            if summary:
                m["summary"] = summary
                m["tag"] = tag
                summarized += 1
        except Exception as e:
            print(f"    CHYBA stahování PDF {d['case_number']}: {e}")
        d["summary"] = m.get("summary", "")
        d["tag"] = m.get("tag", "")

    save_meta(meta)
    if gemini_calls:
        print(f"    AI: {summarized}/{gemini_calls} shrnutí vygenerováno")
    return decisions


# --- Sloučení obou zdrojů ---

def merge_decisions(uredni, judikatura):
    """Spojí oba seznamy, deduplikuje podle spisové značky a doplní chybějící údaje."""
    merged = {}
    order = []

    def add(d):
        k = normalize_case(d["case_number"])
        if k in merged:
            existing = merged[k]
            # Doplníme údaje, které úřední deska nemá (PDF, detail, kategorie)
            for field in ("pdf_url", "detail_url", "category", "unid"):
                if not existing.get(field) and d.get(field):
                    existing[field] = d[field]
            existing["source"] = "uredni+judikatura"
        else:
            merged[k] = dict(d)
            order.append(k)

    # Úřední deska první – má autoritativní datum vyhlášení
    for d in uredni:
        add(d)
    for d in judikatura:
        add(d)

    return [merged[k] for k in order]


def resolve_dates(decisions, weeks=2):
    """Doplní pub_dt, zaznamená první výskyt, označí nové a ponechá jen
    rozhodnutí zveřejněná do `weeks` týdnů zpět.

    pub_dt (pro řazení/zobrazení): datum zveřejnění > vyhlášení > první výskyt.
    Okno (jak dlouho položku držíme) se počítá podle data zveřejnění (pub_dt),
    ne podle prvního výskytu u nás – jinak by se po resetu sledování v živém
    seznamu držela i starší rozhodnutí. První výskyt slouží jen k označení
    „nové dnes".
    """
    seen = load_seen()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(weeks=weeks)
    today = now.date()

    kept = []
    for d in decisions:
        ident = d.get("unid") or normalize_case(d["case_number"])
        if ident not in seen:
            seen[ident] = now.isoformat()
        first_seen = datetime.fromisoformat(seen[ident])

        pub_dt = None
        # 1) judikatura: skutečné datum zveřejnění z detailu (ISO)
        if d.get("published"):
            try:
                pub_dt = datetime.fromisoformat(d["published"]).replace(tzinfo=timezone.utc)
            except ValueError:
                pub_dt = None
        # 2) úřední deska: datum vyhlášení (DD.MM.YYYY)
        if pub_dt is None and d.get("date"):
            try:
                pub_dt = datetime.strptime(d["date"], "%d.%m.%Y").replace(tzinfo=timezone.utc)
            except ValueError:
                pub_dt = None
        # 3) fallback: první výskyt u nás
        if pub_dt is None:
            pub_dt = first_seen

        d["pub_dt"] = pub_dt
        d["is_new"] = (first_seen.date() == today)

        if pub_dt >= cutoff:
            kept.append(d)

    save_seen(seen)
    return kept


# --- RSS ---

def build_rss(decisions):
    """Vytvoří RSS 2.0 XML z rozhodnutí."""
    rss = Element("rss", version="2.0", attrib={
        "xmlns:dc": "http://purl.org/dc/elements/1.1/",
        "xmlns:atom": "http://www.w3.org/2005/Atom",
    })
    channel = SubElement(rss, "channel")

    SubElement(channel, "title").text = "NS ČR – senát 23 Cdo – rozhodnutí"
    SubElement(channel, "link").text = URL
    # atom:link rel="self" – adresa feedu samotného (vyžaduje RSS best practice)
    SubElement(channel, "atom:link", attrib={
        "href": "https://rss.davidzavada.cz/feed.xml",
        "rel": "self", "type": "application/rss+xml",
    })
    SubElement(channel, "description").text = (
        "Rozhodnutí senátu 23 Cdo Nejvyššího soudu ČR – úřední deska "
        "i databáze judikatury (včetně usnesení)"
    )
    SubElement(channel, "language").text = "cs"
    SubElement(channel, "lastBuildDate").text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )

    for d in decisions:
        item = SubElement(channel, "item")
        SubElement(item, "title").text = d["case_number"]

        link = d.get("pdf_url") or d.get("detail_url") or ""
        if link:
            SubElement(item, "link").text = link

        # GUID: UNID (stabilní) > odkaz > spisová značka
        guid = d.get("unid") or link or d["case_number"]
        SubElement(item, "guid", isPermaLink="false" if d.get("unid") or not link else "true").text = guid

        prefix = (d.get("typ") or "Rozhodnutí").capitalize()
        meta_parts = [f"{prefix} {d['case_number']}"]
        if d.get("heslo"):
            meta_parts.append(f"Heslo: {d['heslo']}")
        if d.get("decided"):
            meta_parts.append(f"rozhodnuto {d['decided']}")
        if d.get("date"):
            meta_parts.append(f"vyhlášeno {d['date']}")
        if d.get("category"):
            meta_parts.append(f"kategorie {d['category']}")
        meta_line = ", ".join(meta_parts)
        if d.get("summary"):
            desc = f"{d['summary']}\n\n({meta_line})"
        else:
            desc = meta_line
        SubElement(item, "description").text = desc

        # Heslo jako RSS <category> (čte ho index.html do samostatného sloupce)
        if d.get("heslo"):
            SubElement(item, "category").text = d["heslo"]

        # AI shrnutí jako zvláštní element (čte ho index.html)
        if d.get("summary"):
            SubElement(item, "ai-summary").text = d["summary"]

        # AI heslo (právní téma sporu) – samostatný sloupec v index.html
        if d.get("tag"):
            SubElement(item, "ai-tag").text = d["tag"]

        # Příznak „nové dnes" (čte ho index.html → tečka u názvu)
        if d.get("is_new"):
            SubElement(item, "is-new").text = "true"

        pub_dt = d["pub_dt"]
        SubElement(item, "pubDate").text = pub_dt.strftime("%a, %d %b %Y 12:00:00 +0000")
        SubElement(item, "dc:date").text = pub_dt.strftime("%Y-%m-%d")

    return rss


def main():
    print("Stahuji rozhodnutí senátu 23 Cdo...")

    uredni = []
    print("  Zdroj: úřední deska (www.nsoud.cz)")
    try:
        uredni = fetch_uredni_deska()
        print(f"    Nalezeno {len(uredni)} rozhodnutí")
    except Exception as e:
        print(f"    CHYBA: {e}")

    judikatura = []
    print(f"  Zdroj: databáze judikatury (rozhodnuti.nsoud.cz, posledních {JUDIKATURA_DAYS} dní)")
    try:
        judikatura = fetch_judikatura()
        print(f"    Nalezeno {len(judikatura)} rozhodnutí")
    except Exception as e:
        print(f"    CHYBA: {e}")

    decisions = merge_decisions(uredni, judikatura)
    decisions = enrich_metadata(decisions)          # datum zveřejnění + Heslo (cache)
    decisions = resolve_dates(decisions)            # okno 2 týdny + příznak nové
    decisions = enrich_summaries(decisions)         # Gemini jen na ponechané
    decisions.sort(key=lambda d: d["pub_dt"], reverse=True)

    print(f"Celkem {len(decisions)} rozhodnutí senátu {SENAT} po sloučení")
    for d in decisions:
        when = d.get("date") or d["pub_dt"].strftime("%Y-%m-%d")
        print(f"  - {d['case_number']} ({when}) [{d['source']}]")

    rss = build_rss(decisions)

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    indent(rss, space="  ")
    tree = ElementTree(rss)
    tree.write(OUTPUT, encoding="unicode", xml_declaration=True)
    print(f"RSS feed zapsán do {OUTPUT}")


if __name__ == "__main__":
    main()
