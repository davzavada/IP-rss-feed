#!/usr/bin/env python3
"""Scraper IPcuria – generuje RSS feed ze 2 kategorií (rulings, referrals)."""

import os
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
from xml.etree.ElementTree import Element, SubElement, ElementTree, indent

import requests
from bs4 import BeautifulSoup

from feed_common import (
    CJEU_PROMPT,
    filter_by_first_seen,
    gemini_enabled,
    gemini_summarize_text,
    load_json,
    save_json,
    update_archive,
)

CURIA_BASE = "https://curia.europa.eu/juris/liste.do?num="
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "ipcuria_feed.xml")
ARCHIVE_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "ipcuria_archive.xml")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ipcuria_seen.json")
# Cache AI shrnutí podle guid ({guid: {"summary": ..., "tag": ...}}).
META_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ipcuria_meta.json")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SOURCES = [
    ("https://ipcuria.eu/all_preliminary_rulings.php", "Ruling"),
    ("https://ipcuria.eu/all_referrals.php", "Referral"),
]


def _guid(d):
    """Stabilní identifikátor položky (shodný s oknem prvního výskytu i RSS guid)."""
    return f"{d['category']}-{d['case_ref']}"


def fetch_all():
    """Stáhne obě stránky a vrátí rozhodnutí z posledních 31 dnů."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=31)
    decisions = []

    for url, category in SOURCES:
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"  CHYBA při stahování {category} ({url}): {e}")
            continue
        body_html = resp.text

        # Stránky mají bloky oddělené <hr>
        blocks = re.split(r"<hr\s*/?>", body_html)

        for block in blocks:
            block_soup = BeautifulSoup(block, "html.parser")
            text = block_soup.get_text()

            # Číslo případu z odkazu
            link_tag = block_soup.find("a", href=re.compile(r"case\?reference="))
            if not link_tag:
                continue
            case_ref = link_tag.get_text(strip=True)

            # Název případu z <i>
            name_tag = block_soup.find("i")
            case_name = name_tag.get_text(strip=True) if name_tag else ""

            # Datum – různé formáty:
            # "Judgement of 19 Mar 2026" / "Order of ..." / "lodged on 3 Feb 2026"
            date_match = re.search(r"(\d{1,2}\s+\w{3}\s+\d{4})", text)
            if not date_match:
                continue

            date_str = date_match.group(1)
            try:
                dt = datetime.strptime(date_str, "%d %b %Y").replace(tzinfo=timezone.utc)
            except ValueError:
                continue

            if dt < cutoff:
                continue

            # Typ rozhodnutí (pro Ruling/Appeal stránky)
            type_match = re.search(r"(Judgement|Judgment|Order)", text)
            detail_type = ""
            if type_match:
                detail_type = type_match.group(1)
                if detail_type == "Judgement":
                    detail_type = "Judgment"

            # Kategorie z breadcrumbs (pokud existují)
            categories = []
            for span in block_soup.select("span.breadcrumbs"):
                cats = [a.get_text(strip=True) for a in span.find_all("a")]
                if cats:
                    categories.append(" > ".join(cats))

            decisions.append({
                "case_ref": case_ref,
                "case_name": case_name,
                "date": dt,
                "date_str": date_str,
                "category": category,
                "detail_type": detail_type,
                "categories": categories,
                "ipcuria_url": f"https://ipcuria.eu/case?reference={case_ref}",
                "curia_url": f"{CURIA_BASE}{case_ref}",
            })

    decisions.sort(key=lambda d: d["date"], reverse=True)
    return decisions


def fetch_curia_text(curia_url):
    """Z CURIA (liste.do) najde odkaz na plný dokument a vrátí jeho text.

    Když je k dispozici odkaz na samotný dokument (document.jsf), stáhne ho
    a vytáhne text rozsudku/žádosti; jinak použije text výpisové stránky.
    Vrací '' při neúspěchu – pak shrnutí radši nevytváříme.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    }
    # Sdílená session – CURIA vyžaduje cookie z výpisové stránky, než pustí dokument.
    session = requests.Session()
    try:
        r = session.get(curia_url, headers=headers, timeout=60)
        r.raise_for_status()
    except Exception as e:
        print(f"    CHYBA stahování CURIA {curia_url}: {e}")
        return ""

    soup = BeautifulSoup(r.text, "html.parser")
    doc_link = soup.find("a", href=re.compile(r"document/document\.jsf"))
    if doc_link and doc_link.get("href"):
        doc_url = urljoin(curia_url, doc_link["href"])
        try:
            dr = session.get(doc_url, headers=headers, timeout=60)
            dr.raise_for_status()
            soup = BeautifulSoup(dr.text, "html.parser")
        except Exception as e:
            print(f"    CHYBA stahování dokumentu CURIA {doc_url}: {e}")
    else:
        print(f"    [diag] CURIA bez odkazu na dokument (jen výpis): {curia_url}")

    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def enrich_summaries(decisions):
    """Doplní AI shrnutí (HESLO + SHRNUTÍ) přes Gemma; cache podle guid.

    Volá se až na ponechané položky (po okně), aby se neshrnovalo zbytečně.
    Text bere z CURIA; když ho je příliš málo, shrnutí raději nevytváří
    (ať si Gemma nic nevymýšlí).
    """
    meta = load_json(META_FILE)
    if not gemini_enabled():
        for d in decisions:
            m = meta.get(_guid(d), {})
            d["summary"] = m.get("summary", "")
            d["tag"] = m.get("tag", "")
        return decisions

    summarized = 0
    calls = 0
    for d in decisions:
        g = _guid(d)
        m = meta.get(g, {})
        if not m.get("summary"):
            text = fetch_curia_text(d["curia_url"])
            # Doplníme kontext, který už máme z výpisu (témata z breadcrumbs).
            if d.get("categories"):
                text = "Témata: " + "; ".join(d["categories"]) + "\n\n" + text
            tlen = len(text.strip())
            print(f"    [diag] {g}: {tlen} znaků textu z CURIA")
            if tlen >= 400:
                calls += 1
                summary, tag = gemini_summarize_text(text, CJEU_PROMPT)
                if summary:
                    m = {"summary": summary, "tag": tag}
                    meta[g] = m
                    summarized += 1
                else:
                    print(f"    [diag] {g}: Gemma nevrátila shrnutí")
            else:
                print(f"    [diag] {g}: málo textu (<400), shrnutí přeskočeno")
        d["summary"] = m.get("summary", "")
        d["tag"] = m.get("tag", "")

    save_json(META_FILE, meta)
    if calls:
        print(f"  AI: {summarized}/{calls} shrnutí vygenerováno")
    return decisions


def build_rss(decisions):
    """Vytvoří RSS 2.0 XML z rozhodnutí."""
    rss = Element("rss", version="2.0", attrib={
        "xmlns:dc": "http://purl.org/dc/elements/1.1/"
    })
    channel = SubElement(rss, "channel")

    SubElement(channel, "title").text = "CJEU IP case law"
    SubElement(channel, "link").text = "https://curia.europa.eu/"
    SubElement(channel, "description").text = (
        "Latest CJEU IP case law: preliminary rulings, referrals, appeals"
    )
    SubElement(channel, "language").text = "en"
    SubElement(channel, "lastBuildDate").text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )

    for d in decisions:
        item = SubElement(channel, "item")

        title = f"[{d['category']}] {d['case_ref']}"
        if d["case_name"]:
            title += f" ({d['case_name']})"
        SubElement(item, "title").text = title

        SubElement(item, "link").text = d["curia_url"]
        SubElement(item, "guid", isPermaLink="false").text = f"{d['category']}-{d['case_ref']}"

        if d.get("is_new"):
            SubElement(item, "is-new").text = "true"

        # AI shrnutí + heslo (čte je index.html do samostatných sloupců)
        if d.get("tag"):
            SubElement(item, "ai-tag").text = d["tag"]
        if d.get("summary"):
            SubElement(item, "ai-summary").text = d["summary"]

        desc_parts = [
            f"[{d['category']}] {d['date_str']}, {d['case_ref']}",
        ]
        if d["case_name"]:
            desc_parts[0] += f" ({d['case_name']})"
        if d.get("summary"):
            desc_parts.append(d["summary"])
        if d["detail_type"]:
            desc_parts.append(f"Type: {d['detail_type']}")
        for cat in d["categories"]:
            desc_parts.append(f"- {cat}")
        desc_parts.append(f"CURIA: {d['curia_url']}")

        SubElement(item, "description").text = "\n".join(desc_parts)

        SubElement(item, "pubDate").text = d["date"].strftime(
            "%a, %d %b %Y 12:00:00 +0000"
        )
        SubElement(item, "dc:date").text = d["date"].strftime("%Y-%m-%d")

    return rss


def main():
    print("Stahuji IPcuria – 2 kategorie...")
    decisions = fetch_all()
    print(f"Nalezeno {len(decisions)} položek z posledního měsíce")

    # Okno 2 týdny od prvního výskytu + příznak „nové dnes"
    decisions = filter_by_first_seen(decisions, _guid, STATE_FILE, weeks=2)
    decisions.sort(key=lambda d: d["date"], reverse=True)
    print(f"Po okně 2 týdnů: {len(decisions)} položek")

    decisions = enrich_summaries(decisions)  # AI shrnutí jen na ponechané

    for d in decisions:
        print(f"  [{d['category']}] {d['case_ref']} {d['case_name']} ({d['date_str']})")

    rss = build_rss(decisions)

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    archived = update_archive(ARCHIVE_OUTPUT, rss)  # archiv (nemaže staré položky)
    if archived < 0:
        print(f"Archiv: ponechán beze změny (chyba čtení) → {ARCHIVE_OUTPUT}")
    else:
        print(f"Archiv: {archived} položek → {ARCHIVE_OUTPUT}")

    indent(rss, space="  ")
    tree = ElementTree(rss)
    tree.write(OUTPUT, encoding="unicode", xml_declaration=True)
    print(f"RSS feed zapsán do {OUTPUT}")


if __name__ == "__main__":
    main()
