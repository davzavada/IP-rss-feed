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
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "feed.xml")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed_seen.json")


def normalize_case(case_number):
    """Sjednotí mezery ve spisové značce pro porovnání/deduplikaci."""
    return re.sub(r"\s+", " ", case_number).strip()


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

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
        "Referer": "https://rozhodnuti.nsoud.cz/",
    }
    # Domino může vyžadovat session cookie – nejprve navštívíme úvodní stránku.
    session = requests.Session()
    try:
        session.get("https://rozhodnuti.nsoud.cz/", headers=headers, timeout=30)
    except Exception:
        pass

    resp = session.get(url, headers=headers, timeout=30)
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
        detail_url = link.get("href", "")

        pdf_url = ""
        rtf_url = ""
        for a in row.select("td.icons a[href]"):
            href = a["href"]
            low = href.lower()
            if ".pdf?openelement" in low or low.endswith(".pdf"):
                pdf_url = href
            elif ".rtf?openelement" in low or low.endswith(".rtf"):
                rtf_url = href

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


def resolve_dates(decisions):
    """Doplní každému rozhodnutí pub_dt (datetime). Judikatura bez data → první výskyt."""
    seen = load_seen()
    now = datetime.now(timezone.utc)

    for d in decisions:
        pub_dt = None
        if d.get("date"):
            try:
                pub_dt = datetime.strptime(d["date"], "%d.%m.%Y").replace(tzinfo=timezone.utc)
            except ValueError:
                pub_dt = None

        if pub_dt is None:
            ident = d.get("unid") or normalize_case(d["case_number"])
            if ident not in seen:
                seen[ident] = now.isoformat()
            pub_dt = datetime.fromisoformat(seen[ident])

        d["pub_dt"] = pub_dt

    save_seen(seen)
    return decisions


# --- RSS ---

def build_rss(decisions):
    """Vytvoří RSS 2.0 XML z rozhodnutí."""
    rss = Element("rss", version="2.0", attrib={
        "xmlns:dc": "http://purl.org/dc/elements/1.1/"
    })
    channel = SubElement(rss, "channel")

    SubElement(channel, "title").text = "NS ČR – senát 23 Cdo – rozhodnutí"
    SubElement(channel, "link").text = URL
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

        desc_parts = [f"Rozhodnutí {d['case_number']}"]
        if d.get("date"):
            desc_parts.append(f"vyhlášeno {d['date']}")
        if d.get("category"):
            desc_parts.append(f"kategorie {d['category']}")
        SubElement(item, "description").text = ", ".join(desc_parts)

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
    decisions = resolve_dates(decisions)
    decisions.sort(key=lambda d: d["pub_dt"], reverse=True)

    print(f"Celkem {len(decisions)} rozhodnutí senátu {SENAT} po sloučení")
    for d in decisions:
        when = d.get("date") or d["pub_dt"].strftime("%Y-%m-%d")
        print(f"  - {d['case_number']} ({when}) [{d['source']}]")

    rss = build_rss(decisions)
    indent(rss, space="  ")

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    tree = ElementTree(rss)
    tree.write(OUTPUT, encoding="unicode", xml_declaration=True)
    print(f"RSS feed zapsán do {OUTPUT}")


if __name__ == "__main__":
    main()
