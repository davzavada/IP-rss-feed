#!/usr/bin/env python3
"""Scraper for legal journals – generates RSS feed for new journal issues and articles."""

import os
import re
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, ElementTree, indent, parse as ET_parse

import requests
from bs4 import BeautifulSoup

from feed_common import (
    JOURNAL_ARTICLE_PROMPT,
    JOURNAL_ISSUE_PROMPT,
    filter_by_first_seen,
    gemini_enabled,
    gemini_summarize_pdf,
    gemini_summarize_text,
    load_json,
    save_json,
    update_archive,
)

OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "journals_feed.xml")
ARCHIVE_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "journals_archive.xml")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "journals_seen.json")
# Cache AI shrnutí podle guid ({guid: {"summary": ..., "tag": ...}}).
META_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "journals_meta.json")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# --- ÚPV journals (scrape HTML) ---

def scrape_upv():
    """Scrape ÚPV journal page for latest PDF issues."""
    url = "https://upv.gov.cz/informacni-zdroje/publikace/casopis-dusevni-vlastnictvi"
    base_url = "https://upv.gov.cz"

    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    items = []
    current_year = None

    for el in soup.find_all(["h3", "a"]):
        if el.name == "h3":
            year_match = re.match(r"(\d{4})", el.get_text(strip=True))
            if year_match:
                current_year = year_match.group(1)
            continue

        href = el.get("href", "")
        if not href.endswith(".pdf"):
            continue

        title = el.get_text(strip=True)
        if not title:
            continue

        if href.startswith("/"):
            pdf_url = base_url + href
        elif href.startswith("http"):
            pdf_url = href
        else:
            continue

        issue_match = re.search(r"(\d{4})-?(\d+)", href)
        year = current_year or (issue_match.group(1) if issue_match else None)
        issue_num = issue_match.group(2) if issue_match else ""

        if not year:
            continue

        if "evropske_pravo" in href.lower():
            journal_name = "Evropské právo"
        else:
            journal_name = "Duševní vlastnictví"

        quarter_months = {"1": "01", "01": "01", "2": "04", "02": "04",
                          "3": "07", "03": "07", "4": "10", "04": "10"}
        month = quarter_months.get(issue_num, "01")
        pub_date = datetime.strptime(f"{year}-{month}-01", "%Y-%m-%d").replace(tzinfo=timezone.utc)

        items.append({
            "title": f"{journal_name} {issue_num}/{year}",
            "journal_name": journal_name,
            "link": pdf_url,
            "description": f"{journal_name} {issue_num}/{year}\nPDF: {pdf_url}",
            "guid": f"{journal_name}-{issue_num}-{year}",
            "pub_date": pub_date,
            "sort_key": (year, issue_num),
            "ai_source": "issue",  # shrnutí se dělá z celého PDF čísla
        })

    # Keep only the latest issue per journal name
    items.sort(key=lambda x: x["sort_key"], reverse=True)
    seen = set()
    latest = []
    for item in items:
        if item["journal_name"] not in seen:
            seen.add(item["journal_name"])
            latest.append(item)
    return latest


# --- MUNI Revue pro právo a technologie (fetch their RSS) ---

def fetch_muni_rss():
    """Fetch MUNI journal RSS and extract latest articles."""
    url = "https://journals.muni.cz/revue/gateway/plugin/WebFeedGatewayPlugin/rss2"
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()

    import xml.etree.ElementTree as ET
    root = ET.fromstring(resp.text)
    ns = {"dc": "http://purl.org/dc/elements/1.1/"}
    items = []

    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = item.find("description")
        creator_el = item.find("dc:creator", ns)
        pub_date_el = item.find("pubDate")
        guid_el = item.find("guid")

        title = (title_el.text or "").strip() if title_el is not None else ""
        link = (link_el.text or "").strip() if link_el is not None else ""
        desc = (desc_el.text or "").strip() if desc_el is not None else ""
        creator = (creator_el.text or "").strip() if creator_el is not None else ""
        guid = (guid_el.text or "").strip() if guid_el is not None else link

        # Parse date
        pub_date = None
        if pub_date_el is not None and pub_date_el.text:
            try:
                date_str = pub_date_el.text.strip()
                # Handle Czech date format from OJS
                pub_date = datetime.strptime(
                    re.sub(r"^[A-ZÁ-Žá-ž]+,\s*", "", date_str),
                    "%d %b %Y %H:%M:%S %z"
                )
            except ValueError:
                try:
                    pub_date = datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S %z")
                except ValueError:
                    pub_date = datetime.now(timezone.utc)

        # Clean up description (remove HTML). Plnou anotaci si necháme pro AI,
        # do feedu jde zkrácená verze.
        full_desc = BeautifulSoup(desc, "html.parser").get_text().strip()
        clean_desc = full_desc
        if len(clean_desc) > 300:
            clean_desc = clean_desc[:297] + "..."

        full_title = f"[RPT] {title}"
        if creator:
            full_title += f" – {creator}"

        items.append({
            "title": full_title,
            "journal_name": "Revue pro právo a technologie",
            "link": link,
            "description": f"{title}\nAutor: {creator}\n{clean_desc}" if creator else f"{title}\n{clean_desc}",
            "guid": f"RPT-{guid}",
            "pub_date": pub_date or datetime.now(timezone.utc),
            "sort_key": (pub_date.strftime("%Y") if pub_date else "0000", "00"),
            "ai_source": "article",  # shrnutí se dělá z názvu a anotace
            "ai_text": f"{title}\n\n{full_desc}".strip(),
        })

    return items


# --- AI shrnutí (Gemma) ---

def enrich_summaries(items):
    """Doplní AI shrnutí (HESLO + SHRNUTÍ) přes Gemma; cache podle guid.

    Volá se až na ponechané položky (po okně). Články (MUNI RPT) shrnuje
    z názvu a anotace, čísla časopisů (ÚPV) z celého PDF.
    """
    meta = load_json(META_FILE)
    if not gemini_enabled():
        for it in items:
            m = meta.get(it["guid"], {})
            it["summary"] = m.get("summary", "")
            it["tag"] = m.get("tag", "")
        return items

    summarized = 0
    calls = 0
    for it in items:
        g = it["guid"]
        m = meta.get(g, {})
        if not m.get("summary"):
            summary, tag = "", ""
            if it.get("ai_source") == "article" and it.get("ai_text"):
                tlen = len(it["ai_text"])
                print(f"    [diag] {g}: článek, ai_text {tlen} znaků")
                calls += 1
                summary, tag = gemini_summarize_text(it["ai_text"], JOURNAL_ARTICLE_PROMPT)
            elif it.get("ai_source") == "issue" and it.get("link"):
                try:
                    pr = requests.get(it["link"], headers={"User-Agent": USER_AGENT}, timeout=120)
                    pr.raise_for_status()
                    print(f"    [diag] {g}: číslo, PDF {len(pr.content)} B")
                    calls += 1
                    summary, tag = gemini_summarize_pdf(pr.content, JOURNAL_ISSUE_PROMPT)
                except Exception as e:
                    print(f"  CHYBA stahování PDF {it['title']}: {e}")
            else:
                print(f"    [diag] {g}: PŘESKAKUJI (source={it.get('ai_source')}, "
                      f"ai_text={bool(it.get('ai_text'))}, link={bool(it.get('link'))})")
            if summary:
                m = {"summary": summary, "tag": tag}
                meta[g] = m
                summarized += 1
            else:
                print(f"    [diag] {g}: bez shrnutí")
        it["summary"] = m.get("summary", "")
        it["tag"] = m.get("tag", "")

    save_json(META_FILE, meta)
    if calls:
        print(f"  AI: {summarized}/{calls} shrnutí vygenerováno")
    return items


# --- Build combined RSS ---

def build_rss(all_items):
    """Build RSS 2.0 XML from all journal items."""
    rss = Element("rss", version="2.0", attrib={
        "xmlns:dc": "http://purl.org/dc/elements/1.1/"
    })
    channel = SubElement(rss, "channel")

    SubElement(channel, "title").text = "Právní časopisy"
    SubElement(channel, "link").text = "https://rss.davidzavada.cz/journals_feed.xml"
    SubElement(channel, "description").text = "Nová čísla právních časopisů a články"
    SubElement(channel, "language").text = "cs"
    SubElement(channel, "lastBuildDate").text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )

    for item in all_items:
        el = SubElement(channel, "item")
        SubElement(el, "title").text = item["title"]
        SubElement(el, "link").text = item["link"]
        SubElement(el, "guid", isPermaLink="false").text = item["guid"]
        desc = item["description"]
        if item.get("summary"):
            desc = f"{desc}\n{item['summary']}"
        SubElement(el, "description").text = desc
        if item.get("is_new"):
            SubElement(el, "is-new").text = "true"
        # AI shrnutí + heslo (čte je index.html do samostatných sloupců)
        if item.get("tag"):
            SubElement(el, "ai-tag").text = item["tag"]
        if item.get("summary"):
            SubElement(el, "ai-summary").text = item["summary"]
        SubElement(el, "pubDate").text = item["pub_date"].strftime(
            "%a, %d %b %Y 12:00:00 +0000"
        )
        SubElement(el, "dc:date").text = item["pub_date"].strftime("%Y-%m-%d")

    return rss


def main():
    print("Stahuji právní časopisy...")
    all_items = []

    # 1. ÚPV
    print("  Zdroj: Duševní vlastnictví / Evropské právo (ÚPV)")
    try:
        upv = scrape_upv()
        print(f"  Nalezeno {len(upv)} nejnovějších čísel")
        all_items.extend(upv)
    except Exception as e:
        print(f"  CHYBA při stahování ÚPV: {e}")

    # 2. MUNI RPT
    print("  Zdroj: Revue pro právo a technologie (MUNI)")
    try:
        muni = fetch_muni_rss()
        print(f"  Nalezeno {len(muni)} článků")
        all_items.extend(muni)
    except Exception as e:
        print(f"  CHYBA při stahování MUNI RPT: {e}")

    # Ponecháme jen položky s prvním výskytem do 2 týdnů zpět (u všech zdrojů).
    # První výskyt sledujeme sami, aby se staré články s přepsaným datem nevracely.
    all_items = filter_by_first_seen(
        all_items, lambda i: i["guid"], STATE_FILE, weeks=2
    )

    # Sort by date desc
    all_items.sort(key=lambda x: x["pub_date"], reverse=True)

    all_items = enrich_summaries(all_items)  # AI shrnutí jen na ponechané

    rss = build_rss(all_items)

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

    for item in all_items[:6]:
        print(f"  {item['title']}")


if __name__ == "__main__":
    main()
