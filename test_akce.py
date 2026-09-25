#!/usr/bin/env python3
"""Testy scraperu akcí (scraper_akce.py).

Weby pořadatelů nejsou z vývojového prostředí vidět, takže testy běží nad
syntetickými stránkami ve formátech, které scraper umí číst: schema.org
Event v JSON-LD, iCalendar a obyčejná stránka pro AI (AI je tu podvržená).
Až sonda uloží skutečné výpisy (tests/fixtures/akce/), přibudou testy
vlastních parserů nad nimi.

Spuštění: python test_akce.py
"""

import json
import os
import sys
import tempfile
from datetime import date

import scraper_akce as s
from judikatura.taxonomie import Taxonomie

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("  OK   " if cond else "  CHYBA") + f" {name}" + (f" – {detail}" if detail and not cond else ""))


DNES = date(2026, 9, 25)
CFG = {"poradatele": {
    "CAK": {"nazev": "Česká advokátní komora", "zkratka": "ČAK", "barva": "#b42318",
            "stranky": ["https://www.cak.cz/vzdelavaci-akce-cak-5"], "hosty": ["cak.cz"]},
    "UPV": {"nazev": "Úřad průmyslového vlastnictví", "zkratka": "ÚPV", "barva": "#0e7490",
            "stranky": ["https://upv.gov.cz/vzdelavani/kurzy-a-seminare"], "hosty": ["upv.gov.cz"]},
}}
CAK = CFG["poradatele"]["CAK"]


# --- Normalizace ---------------------------------------------------------
print("Normalizace")
check("datum ISO", s.norm_datum("2026-10-01T09:00:00+02:00") == "2026-10-01")
check("datum české", s.norm_datum("čt 1. 10. 2026") == "2026-10-01")
check("neplatné datum", s.norm_datum("31. 2. 2026") is None)
check("čas", s.norm_cas("9.00") == "09:00" and s.norm_cas("2026-10-01T16:30") == "16:30")
check("čas bez času", s.norm_cas("celý den") == "")
check("UTC na Prahu", s.iso_na_prahu("2026-10-01T07:00:00Z") == ("2026-10-01", "09:00"))
check("zimní čas", s.iso_na_prahu("2026-11-02T08:00:00Z") == ("2026-11-02", "09:00"))
check("forma online", s.norm_forma("Webinář") == "online")
check("forma hybridní", s.norm_forma("prezenčně i online") == "hybridne")
check("forma z místa", s.norm_forma("", "Praha, Národní 16") == "prezencne")
check("forma neznámá", s.norm_forma("", "") == "")
check("odkaz na doménu pořadatele", s.povoleny_odkaz("https://www.cak.cz/akce/1", ["cak.cz"]))
check("cizí odkaz ne", not s.povoleny_odkaz("https://evil.example/cak.cz", ["cak.cz"]))
check("javascript: ne", not s.povoleny_odkaz("javascript:alert(1)", ["cak.cz"]))
check("id nezávisí na času a diakritice",
      s.akce_id("CAK", "2026-10-01", "Ochranné známky") == s.akce_id("CAK", "2026-10-01", "ochranne  znamky"))

a = s.normalizuj({"nazev": "  Seminář  ", "datum": "1. 10. 2026", "zacatek": "9:00",
                  "url": "/akce/42", "misto": "online", "lektori": "JUDr. A; Mgr. B"},
                 "CAK", CAK, CAK["stranky"][0])
check("normalizuj: relativní odkaz", a["url"] == "https://www.cak.cz/akce/42", a["url"])
check("normalizuj: forma a lektoři", a["forma"] == "online" and a["lektori"] == ["JUDr. A", "Mgr. B"], str(a))
check("normalizuj: cizí odkaz zahozen",
      s.normalizuj({"nazev": "X", "datum": "2026-10-01", "url": "https://jinde.cz/x"},
                   "CAK", CAK, "")["url"] == "")
check("normalizuj: bez data nic", s.normalizuj({"nazev": "X"}, "CAK", CAK, "") is None)

# --- JSON-LD -------------------------------------------------------------
print("JSON-LD")
JSONLD = """<html><head><script type="application/ld+json">
{"@context": "https://schema.org", "@graph": [
  {"@type": "WebPage", "name": "Akce"},
  {"@type": "EducationEvent", "name": "Ochranné známky: řízení před ÚPV a EUIPO",
   "startDate": "2026-09-30T09:00:00+02:00", "endDate": "2026-09-30T13:00:00+02:00",
   "url": "https://www.cak.cz/akce/ochranne-znamky",
   "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
   "location": {"@type": "Place", "name": "Kleinův palác",
                "address": {"@type": "PostalAddress", "addressLocality": "Brno"}},
   "performer": [{"@type": "Person", "name": "Ing. Mgr. Jana Dvořáková"}],
   "offers": {"@type": "Offer", "price": "1600", "priceCurrency": "CZK"},
   "description": "<p>Praktický seminář o námitkách.</p>"},
  {"@type": "Event", "name": "Webinář DSA", "startDate": "2026-10-06T13:00",
   "location": {"@type": "VirtualLocation", "url": "https://zoom.us/x"},
   "offers": [{"price": 0}]}
]}
</script></head><body></body></html>"""
ev = s.z_jsonld(JSONLD)
check("JSON-LD: dvě akce z @graph", len(ev) == 2, str(ev))
e0 = ev[0]
check("JSON-LD: čas a konec", (e0["datum"], e0["zacatek"], e0["konec"]) == ("2026-09-30", "09:00", "13:00"))
check("JSON-LD: místo", e0["misto"] == "Kleinův palác, Brno", e0["misto"])
check("JSON-LD: forma, lektor, cena",
      e0["forma"] == "prezencne" and e0["lektori"] == ["Ing. Mgr. Jana Dvořáková"] and e0["cena"] == "1600 Kč", str(e0))
check("JSON-LD: anotace bez HTML", e0["anotace"].strip() == "Praktický seminář o námitkách.")
check("JSON-LD: virtuální = online, 0 = zdarma", ev[1]["forma"] == "online" and ev[1]["cena"] == "zdarma", str(ev[1]))
check("JSON-LD: rozbitý skript nevadí",
      s.z_jsonld('<script type="application/ld+json">{nejde</script>') == [])

# --- iCal ----------------------------------------------------------------
print("iCal")
ICS = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
       "BEGIN:VEVENT\r\nUID:1\r\nDTSTART;TZID=Europe/Prague:20260923T090000\r\n"
       "DTEND;TZID=Europe/Prague:20260923T150000\r\n"
       "SUMMARY:Základy průmyslově právní ochrany – modul\r\n  ochranné známky\r\n"
       "LOCATION:Praha\\, ÚPV\r\nURL:https://upv.gov.cz/kurz/1\r\n"
       "DESCRIPTION:Kurz pro začátečníky.\\nZdarma.\r\nEND:VEVENT\r\n"
       "BEGIN:VEVENT\r\nUID:2\r\nDTSTART:20261007T070000Z\r\nDTEND:20261007T100000Z\r\n"
       "SUMMARY:Rešerše v patentových databázích\r\nLOCATION:online\r\nEND:VEVENT\r\n"
       "BEGIN:VEVENT\r\nUID:3\r\nDTSTART;VALUE=DATE:20261012\r\nDTEND;VALUE=DATE:20261014\r\n"
       "SUMMARY:Dvoudenní kurz\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
iv = s.z_ics(ICS)
check("iCal: tři akce", len(iv) == 3, str(iv))
check("iCal: pokračovací řádek a escapování",
      iv[0]["nazev"] == "Základy průmyslově právní ochrany – modul ochranné známky"
      and iv[0]["misto"] == "Praha, ÚPV", str(iv[0]))
check("iCal: TZID", (iv[0]["datum"], iv[0]["zacatek"], iv[0]["konec"]) == ("2026-09-23", "09:00", "15:00"))
check("iCal: UTC na Prahu", (iv[1]["zacatek"], iv[1]["konec"], iv[1]["forma"]) == ("09:00", "12:00", "online"), str(iv[1]))
check("iCal: vícedenní celodenní", iv[2]["datum"] == "2026-10-12" and iv[2]["datum_do"] == "2026-10-13", str(iv[2]))
check("iCal: odkazy na stránce",
      s.ics_odkazy('<a href="webcal://upv.gov.cz/akce.ics">x</a><a href="/k?ical=1">y</a>',
                   "https://upv.gov.cz/v") == ["https://upv.gov.cz/akce.ics", "https://upv.gov.cz/k?ical=1"])

# --- Text pro AI -----------------------------------------------------------
print("Text stránky")
t = s.text_stranky('<nav>Menu</nav><h2>Seminář</h2><p>1. 10. 2026 <a href="/a/1">Přihláška</a></p>'
                   '<script>x()</script>', "https://www.cak.cz/v")
check("text: bez menu a skriptů", "Menu" not in t and "x()" not in t, t)
check("text: odkaz zůstane", "Přihláška <https://www.cak.cz/a/1>" in t, t)

# --- Načtení pořadatele (lokální soubory, podvržená AI) ---------------------
print("Načtení pořadatele")
tmp = tempfile.mkdtemp()


def soubor(jmeno, obsah):
    p = os.path.join(tmp, jmeno)
    with open(p, "w", encoding="utf-8") as f:
        f.write(obsah)
    return p


puvodni_enabled, puvodni_raw = s.gemini_enabled, s.gemini_generate_raw
s.gemini_enabled = lambda: False
akce, cesta = s.nacti_poradatele("CAK", CAK, DNES, soubor("cak.html", JSONLD))
check("JSON-LD bez AI", cesta == "jsonld" and len(akce) == 2, f"{cesta} {akce}")
akce, cesta = s.nacti_poradatele("UPV", CFG["poradatele"]["UPV"], DNES, soubor("upv.ics", ICS))
check("iCal soubor", cesta == "ical" and len(akce) == 3)
akce, chyba = s.nacti_poradatele("CAK", CAK, DNES, soubor("prazdna.html", "<p>Akce</p>"))
check("bez dat a bez AI = chyba, ne prázdný výpis", akce is None and "AI" in chyba, str(chyba))

volani = []


def fake_ai(prompt, text, max_tokens=8192, timeout=300):
    volani.append(prompt)
    if "přehledem vzdělávacích akcí" in prompt:
        return "```json\n" + json.dumps([
            {"nazev": "Nekalá soutěž v online reklamě", "datum": "2026-10-06", "zacatek": "13:00",
             "konec": "16:00", "url": "https://www.cak.cz/akce/nekala", "misto": "online",
             "forma": "online", "lektori": ["Mgr. Eva Pokorná"], "cena": "1 200 Kč",
             "anotace": "Influencer marketing a klamavá reklama."},
            {"nazev": "Stará akce", "datum": "2025-01-01"},
            {"nazev": "Bez data"},
        ]) + "\n```"
    if "zařaď" in prompt.lower():
        ids = [ln.split("id ", 1)[1].split(":", 1)[0] for ln in text.splitlines() if ln.startswith("- id ")]
        return json.dumps({i: ["nekala_soutez", "it", "neexistuje", "autorske", "gdpr"] for i in ids})
    return ""


s.gemini_enabled = lambda: True
s.gemini_generate_raw = fake_ai
akce, cesta = s.nacti_poradatele("CAK", CAK, DNES, soubor("cak_text.html", "<h1>Akce</h1><p>6. 10.</p>"))
check("AI z textu stránky", cesta == "ai" and [a["nazev"] for a in akce] == ["Nekalá soutěž v online reklamě"],
      f"{cesta} {akce}")

# --- Oblasti -----------------------------------------------------------------
print("Oblasti")
tax = Taxonomie()
volani.clear()
s.zarad_oblasti(akce, tax)
check("oblasti: jen známé, nejvýš tři", akce[0]["oblasti"] == ["nekala_soutez", "it", "autorske"], str(akce[0]))
volani.clear()
s.zarad_oblasti(akce, tax)
check("oblasti: podruhé se AI neptá", not volani)
akce[0]["anotace"] = "Jiná anotace"
s.zarad_oblasti(akce, tax)
check("oblasti: změna obsahu = nové zařazení", len(volani) == 1)
s.gemini_generate_raw = lambda *a, **k: ""
b = [dict(akce[0], id="x", nazev="Nová")]
s.zarad_oblasti(b, tax)
check("oblasti: bez odpovědi AI se nic nezapíše", b[0].get("oblasti_klic") != s.obsah_klic(b[0]))
s.gemini_enabled, s.gemini_generate_raw = puvodni_enabled, puvodni_raw

# --- Sloučení -------------------------------------------------------------
print("Sloučení")


def ak(nazev, datum, **kw):
    return dict({"id": s.akce_id("CAK", datum, nazev), "poradatel": "CAK", "datum": datum,
                 "nazev": nazev, "lektori": [], "cena": ""}, **kw)


stare = [ak("Proběhlá", "2026-09-20"), ak("Zrušená", "2026-10-01"),
         ak("Trvá", "2026-10-02", cena="990 Kč", oblasti=["it"], oblasti_klic="k"),
         dict(ak("Jiný pořadatel", "2026-10-03"), poradatel="UPV")]
nove = [ak("Trvá", "2026-10-02"), ak("Nová", "2026-10-05")]
vysl = {a["nazev"]: a for a in s.sloucit(stare, "CAK", [dict(x) for x in nove], "jsonld", DNES)}
check("proběhlá zůstává", "Proběhlá" in vysl)
check("zmizelá budoucí vypadne", "Zrušená" not in vysl)
check("nová přibude", "Nová" in vysl)
check("údaje ze staré verze", vysl["Trvá"]["cena"] == "990 Kč" and vysl["Trvá"]["oblasti"] == ["it"], str(vysl["Trvá"]))
check("jiný pořadatel se nesahá", "Jiný pořadatel" not in vysl)
vysl = {a["nazev"] for a in s.sloucit(stare * 1 + [ak(f"A{i}", "2026-10-1" + str(i)) for i in range(5)],
                                       "CAK", [ak("Nová", "2026-10-05")], "ai", DNES)}
check("AI vrátila podezřele málo = staré zůstanou", {"Zrušená", "A3", "Nová"} <= vysl, str(vysl))
check("prořezání starých",
      [a["nazev"] for a in s.prorezat([ak("Dávno", "2026-07-01"), ak("Nedávno", "2026-09-01"),
                                        ak("Dlouhá", "2026-07-01", datum_do="2026-09-10")], DNES)]
      == ["Nedávno", "Dlouhá"])

# --- iCal výstup ------------------------------------------------------------
print("iCal výstup")
out = {"poradatele": {"CAK": {"nazev": "Česká advokátní komora", "zkratka": "ČAK", "stranky": []}},
       "akce": [dict(ak("Seminář, s čárkou", "2026-10-01"), zacatek="09:00", konec="12:30",
                     forma="online", lektori=["JUDr. A"], url="https://www.cak.cz/a"),
                dict(ak("Bez konce", "2026-10-02"), zacatek="23:30"),
                dict(ak("Vícedenní", "2026-10-05"), datum_do="2026-10-06")]}
p = os.path.join(tmp, "akce.ics")
s.write_ics(out, p)
ics = open(p, encoding="utf-8", newline="").read()
check("ics: čas", "DTSTART;TZID=Europe/Prague:20261001T090000" in ics and "T123000" in ics)
check("ics: konec nepřeteče přes půlnoc", "DTEND;TZID=Europe/Prague:20261002T235900" in ics)
check("ics: vícedenní celodenní", "DTSTART;VALUE=DATE:20261005" in ics and "DTEND;VALUE=DATE:20261007" in ics)
check("ics: escapování a pořadatel", "SUMMARY:ČAK: Seminář\\, s čárkou" in ics)
check("ics: stálé UID", f"UID:akce-{out['akce'][0]['id']}@{s.ICS_UID_HOST}" in ics)
check("ics: řádky nejvýš 75 bajtů", all(len(r.encode()) <= 75 for r in ics.split("\r\n")))
check("ics: zpětně čitelné", [e["nazev"] for e in s.z_ics(ics)][0] == "ČAK: Seminář, s čárkou")

# --- Skutečný config ----------------------------------------------------------
print("Config")
real = s.load_json(s.CONFIG_FILE)
check("sedm pořadatelů v pořadí štítků",
      list(real["poradatele"]) == ["CAK", "PFUK", "JCP", "BECK", "EPRAVO", "ALAI", "UPV"])
for org, cfg in real["poradatele"].items():
    check(f"{org}: úplný záznam",
          all(cfg.get(k) for k in ("nazev", "zkratka", "barva", "stranky", "hosty"))
          and all(s.povoleny_odkaz(u, cfg["hosty"]) for u in cfg["stranky"])
          and (not cfg.get("parser") or cfg["parser"] in s.PARSERY))

# =====================================================================
failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} testů prošlo")
if failed:
    print("Neprošlo:")
    for n in failed:
        print("  -", n)
sys.exit(1 if failed else 0)
