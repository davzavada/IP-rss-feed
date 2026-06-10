#!/usr/bin/env python3
"""TEMP debug: dump structure of PrF CUNI kolegium dekana dokumenty page."""
import re
import requests
from bs4 import BeautifulSoup

URL = "https://www.prf.cuni.cz/kolegium-dekana/dokumenty"
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

r = requests.get(URL, headers=H, timeout=30)
print("STATUS", r.status_code, "LEN", len(r.text))
soup = BeautifulSoup(r.text, "html.parser")

print("\n===== TABLES =====", len(soup.find_all("table")))
print("===== UL/OL =====", len(soup.find_all(["ul", "ol"])))

print("\n===== LINKS containing 'zapis'/'kolegium'/'.pdf' (text or href) =====")
for a in soup.find_all("a", href=True):
    txt = re.sub(r"\s+", " ", a.get_text(strip=True))
    href = a["href"]
    low = (txt + " " + href).lower()
    if "zápis" in low or "zapis" in low or "kolegium" in low or href.lower().endswith(".pdf"):
        print(repr(txt), "->", href)

print("\n===== MAIN CONTENT CONTAINER (first 6000 chars of likely content) =====")
main = soup.find("main") or soup.find(id="content") or soup.find(class_=re.compile("content")) or soup.body
print(main.prettify()[:6000] if main else "NO MAIN")
