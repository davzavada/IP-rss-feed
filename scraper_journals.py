#!/usr/bin/env python3
"""Scraper for legal journals – generates RSS feed for new journal issues and articles."""

import os
import re
from datetime import datetime, timedelta, timezone
from xml.etree.ElementTree import Element, SubElement, ElementTree, indent, parse as ET_parse

import requests
from bs4 import BeautifulSoup

OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "journals_feed.xml")
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

        # Clean up description (remove HTML)
        clean_desc = BeautifulSoup(desc, "html.parser").get_text()
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
        })

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
        SubElement(el, "description").text = item["description"]
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

    # Drop items older than 2 weeks
    cutoff = datetime.now(timezone.utc) - timedelta(weeks=2)
    all_items = [i for i in all_items if i["pub_date"] >= cutoff]

    # Sort by date desc
    all_items.sort(key=lambda x: x["pub_date"], reverse=True)

    rss = build_rss(all_items)
    indent(rss, space="  ")

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    tree = ElementTree(rss)
    tree.write(OUTPUT, encoding="unicode", xml_declaration=True)
    print(f"RSS feed zapsán do {OUTPUT}")

    for item in all_items[:6]:
        print(f"  {item['title']}")


if __name__ == "__main__":
    main()
