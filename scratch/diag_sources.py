#!/usr/bin/env python3
"""Throwaway diagnostic: compare candidate CJEU text sources (ipcuria, EUR-Lex, CURIA)."""
import re
import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HDR = {"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"}


def dump(label, url):
    print("=" * 72)
    print(label, url)
    try:
        r = requests.get(url, headers=HDR, timeout=60)
        print("  status", r.status_code, "raw_len", len(r.text), "final", r.url)
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "noscript"]):
            t.decompose()
        txt = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
        print("  TEXT_LEN", len(txt))
        print("  TEXT[:700]:", txt[:700])
        docish = sorted({(a.get("href", "") or "") for a in soup.find_all("a")
                         if "document" in (a.get("href", "") or "").lower()})
        print("  docish_links:", docish[:8])
    except Exception as e:
        print("  ERR", repr(e))


cases = [
    ("C-414/24", "CJ", "2024", "0414"),  # ruling
    ("C-312/24", "CJ", "2024", "0312"),  # ruling
    ("C-660/26", "CN", "2026", "0660"),  # referral
    ("C-585/26", "CN", "2026", "0585"),  # referral
]
for ref, typ, yr, num in cases:
    dump(f"IPCURIA {ref}", f"https://ipcuria.eu/case?reference={ref}")
    celex = f"6{yr}{typ}{num}"
    dump(f"EURLEX {ref} {celex}",
         f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{celex}")
