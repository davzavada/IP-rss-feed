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
akce, cesta, _ = s.nacti_poradatele("CAK", CAK, DNES, soubor("cak.html", JSONLD))
check("JSON-LD bez AI", cesta == "jsonld" and len(akce) == 2, f"{cesta} {akce}")
akce, cesta, _ = s.nacti_poradatele("UPV", CFG["poradatele"]["UPV"], DNES, soubor("upv.ics", ICS))
check("iCal soubor", cesta == "ical" and len(akce) == 3)
akce, chyba, _ = s.nacti_poradatele("CAK", CAK, DNES, soubor("prazdna.html", "<p>Akce</p>"))
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
akce, cesta, otisk = s.nacti_poradatele("CAK", CAK, DNES, soubor("cak_text.html", "<h1>Akce</h1><p>6. 10.</p>"))
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
                dict(ak("Vícedenní", "2026-10-05"), datum_do="2026-10-06"),
                dict(ak("Semestrální kurz", "2026-10-05"), datum_do="2026-12-14", zacatek="16:00", konec="17:30")]}
p = os.path.join(tmp, "akce.ics")
s.write_ics(out, p)
ics = open(p, encoding="utf-8", newline="").read()
check("ics: čas", "DTSTART;TZID=Europe/Prague:20261001T090000" in ics and "T123000" in ics)
check("ics: konec nepřeteče přes půlnoc", "DTEND;TZID=Europe/Prague:20261002T235900" in ics)
check("ics: vícedenní celodenní", "DTSTART;VALUE=DATE:20261005" in ics and "DTEND;VALUE=DATE:20261007" in ics)
check("ics: dlouhý kurz jako jedna událost s časem",
      "DTSTART;TZID=Europe/Prague:20261005T160000" in ics and "Do 14. 12. 2026" in ics)
check("ics: escapování a pořadatel", "SUMMARY:ČAK: Seminář\\, s čárkou" in ics)
check("ics: stálé UID", f"UID:akce-{out['akce'][0]['id']}@{s.ICS_UID_HOST}" in ics)
check("ics: řádky nejvýš 75 bajtů", all(len(r.encode()) <= 75 for r in ics.split("\r\n")))
check("ics: zpětně čitelné", [e["nazev"] for e in s.z_ics(ics)][0] == "ČAK: Seminář, s čárkou")

# --- Vlastní parsery nad uloženými výpisy ------------------------------------
print("Parsery")
with open("tests/fixtures/akce/cak_vypis_2026-09-25.html", encoding="utf-8") as f:
    cak = s.parser_cak(f.read(), CAK["stranky"][0])
check("ČAK: všech 39 řádků výpisu", len(cak) == 39, str(len(cak)))
check("ČAK: datum, čas, odkaz",
      (cak[0]["datum"], cak[0]["zacatek"], cak[0]["konec"], cak[0]["url"])
      == ("30. 09. 2026", "10:00", "14:00", "https://www.cak.cz/akce/1191"), str(cak[0]))
kurz = next(x for x in cak if x["datum_do"])
check("ČAK: kurz od–do", kurz["datum_do"] == "14. 12. 2026" and kurz["zacatek"] == "16:00", str(kurz))
n = s.normalizuj(cak[0], "CAK", CAK, CAK["stranky"][0])
check("předpona formy z názvu pryč",
      n["nazev"].startswith("Advokátní tarif") and n["forma"] == "hybridne", str(n))
check("předpona bez zbytku názvu zůstane",
      s.normalizuj({"nazev": "Online:", "datum": "2026-10-01"}, "CAK", CAK, "")["nazev"] == "Online:")
norm_cak = [s.normalizuj(x, "CAK", CAK, CAK["stranky"][0]) for x in cak]
check("ČAK: všechny normalizované kromě zrušené",
      sum(1 for x in norm_cak if x) == 38
      and all(s.je_zrusena(x["nazev"]) for x, n in zip(cak, norm_cak) if not n))

check("pozvánka ze stránky akce",
      s.odkaz_pozvanky('<a href="/kalendar/soubor/1215">Pozvánka 30.9.2026 - PREZENČNÍ FORMA.pdf</a>'
                       '<a href="/jinde">Kontakty</a>', "https://www.cak.cz/akce/1191", ["cak.cz"])
      == "https://www.cak.cz/kalendar/soubor/1215")
check("pozvánka jen z domény pořadatele",
      s.odkaz_pozvanky('<a href="https://jinde.cz/x.pdf">Pozvánka</a>', "https://www.cak.cz/akce/1", ["cak.cz"]) is None)

# --- Stránky akcí -------------------------------------------------------------
print("Stránky akcí")
puvodni = (s.text_detailu, s.z_ai_detailu)
stazeno = []
s.text_detailu = lambda url, hosty=(): (stazeno.append(url) or ("", "text", True))
s.z_ai_detailu = lambda text, org, cfg: ({"lektori": ["JUDr. X"], "cena": "990 Kč", "anotace": "O čem to je."}, False)
cfgd = {"poradatele": {"CAK": CAK}}
akce_d = [ak(f"D{i}", f"2026-10-0{i + 1}", url=f"https://www.cak.cz/akce/{i}") for i in range(4)]
zbytek = s.dopln_detaily(akce_d, cfgd, 3)
check("detail: jen v rozpočtu, od nejbližších", zbytek == 0 and len(stazeno) == 3
      and akce_d[0]["lektori"] == ["JUDr. X"] and not akce_d[3]["lektori"], str(stazeno))
s.dopln_detaily(akce_d, cfgd, 3)
check("detail: hotové se nestahují znovu, zbylá další noc",
      stazeno[3:] == ["https://www.cak.cz/akce/3"] and akce_d[3]["cena"] == "990 Kč", str(stazeno))
s.z_ai_detailu = lambda text, org, cfg: (None, False)
prazdna = [ak("Bez údajů", "2026-10-01", url="https://www.cak.cz/akce/9")]
for _ in range(4):
    s.dopln_detaily(prazdna, cfgd, 5)
check("detail: nejvýš dva pokusy", prazdna[0]["detail_pokusy"] == s.DETAIL_POKUSU)
s.text_detailu, s.z_ai_detailu = puvodni

# --- Výpadek AI při čtení výpisu ------------------------------------------------
print("Výpadek AI u výpisu")
puvodni_pretizena = s.ai_pretizena
s.gemini_enabled = lambda: True
stranka = soubor("vypis.html", "<h1>Akce</h1><p>Seminář 6. 10. 2026</p>")
for popis, odpoved in (("prázdná odpověď", ""), ("text místo JSON", "Omlouvám se, nevím."),
                       ("JSON objekt místo pole", '{"chyba": 1}')):
    s.gemini_generate_raw = lambda *a, _o=odpoved, **k: _o
    akce, chyba, otisk = s.nacti_poradatele("UPV", CFG["poradatele"]["UPV"], DNES, stranka)
    check(f"AI výpis: {popis} = chyba, ne prázdný výpis", akce is None and "AI" in chyba and otisk is None,
          f"{akce} {chyba}")
s.gemini_generate_raw = lambda *a, **k: "ok"
akce, chyba, _ = s.nacti_poradatele("UPV", CFG["poradatele"]["UPV"], DNES,
                                    soubor("jen_skript.html", "<script>render()</script>"))
check("AI výpis: stránka bez textu = chyba", akce is None and chyba, str(chyba))
s.gemini_generate_raw = lambda *a, **k: "[]"
akce, cesta, otisk = s.nacti_poradatele("UPV", CFG["poradatele"]["UPV"], DNES, stranka)
check("AI výpis: „[]“ = přečteno, bez akcí", akce == [] and cesta == "ai" and otisk, f"{akce} {cesta}")


def upv(nazev, datum, **kw):
    return dict({"id": s.akce_id("UPV", datum, nazev), "poradatel": "UPV", "datum": datum,
                 "nazev": nazev, "lektori": [], "cena": ""}, **kw)


stare_upv = [upv("Espacenet", "2026-10-01"), upv("Rešerše", "2026-10-07"), upv("Proběhlá", "2026-09-01")]
for c in ("ai", "parser", "ical", None):
    vysl = {a["nazev"] for a in s.sloucit(stare_upv, "UPV", [], c, DNES)}
    check(f"prázdný výpis ({c}) budoucí akce nesmaže", {"Espacenet", "Rešerše", "Proběhlá"} <= vysl, str(vysl))
check("podezřele málo: nula u parseru ano, pokles u parseru ne",
      s.podezrele_malo(stare_upv, "UPV", [], "parser", DNES)
      and not s.podezrele_malo(stare_upv, "UPV", [upv("Espacenet", "2026-10-01")], "parser", DNES))

# --- Otisk výpisu: nezměněný výpis AI nečte ---------------------------------------
print("Otisk výpisu")
volani_vypis = []


def ai_vypis(prompt, text, **k):
    volani_vypis.append(text)
    return json.dumps([{"nazev": "Espacenet", "datum": "2026-10-01", "anotace": "Nově napsaná anotace."}])


s.gemini_generate_raw = ai_vypis
akce1, cesta1, otisk1 = s.nacti_poradatele("UPV", CFG["poradatele"]["UPV"], DNES, stranka)
minule = {"otisk": otisk1, "akce": [dict(akce1[0], anotace="Anotace ze stránky akce.", detail_pokusy=2),
                                    upv("Proběhlá", "2026-09-01")]}
akce2, cesta2, otisk2 = s.nacti_poradatele("UPV", CFG["poradatele"]["UPV"], DNES, stranka, minule)
check("otisk: stejný text = AI se neptá, minulé akce", len(volani_vypis) == 1 and cesta2 == "ai"
      and otisk2 == otisk1 and [a["anotace"] for a in akce2] == ["Anotace ze stránky akce."], str(akce2))
s.gemini_enabled = lambda: False
akce2, cesta2, _ = s.nacti_poradatele("UPV", CFG["poradatele"]["UPV"], DNES, stranka, minule)
check("otisk: stejný text se čte i bez AI", akce2 and cesta2 == "ai", f"{akce2} {cesta2}")
s.gemini_enabled = lambda: True
akce3, _, otisk3 = s.nacti_poradatele("UPV", CFG["poradatele"]["UPV"], DNES,
                                      soubor("vypis2.html", "<h1>Akce</h1><p>Nový text</p>"), minule)
check("otisk: změněný text = AI čte znovu", len(volani_vypis) == 2 and otisk3 != otisk1)
vysl = s.sloucit(minule["akce"], "UPV", akce3, "ai", DNES)
check("AI výpis nepřepíše dosavadní anotaci",
      next(a for a in vysl if a["nazev"] == "Espacenet")["anotace"] == "Anotace ze stránky akce.")
vysl = s.sloucit([dict(ak("Trvá", "2026-10-02"), anotace="Stará")], "CAK",
                 [ak("Trvá", "2026-10-02", anotace="Nová z JSON-LD")], "jsonld", DNES)
check("strojová data anotaci aktualizují", vysl[0]["anotace"] == "Nová z JSON-LD")

# --- Zrušené akce ------------------------------------------------------------------
print("Zrušené akce")
for nazev in ("POZOR: ONLINE SEMINÁŘ: Stavební zákon SE NEBUDE KONAT a bude přesunut na leden 2027.",
              "ZRUŠENO: Advokátní tarif", "Seminář je zrušen – GDPR v praxi", "Zrušeno: Nekalá soutěž"):
    check(f"zrušená: {nazev[:30]}", s.normalizuj({"nazev": nazev, "datum": "2026-11-04"}, "CAK", CAK, "") is None)
for nazev in ("Zrušení a likvidace obchodní korporace", "Zrušení SJM a vypořádání",
              "Odložení věci v trestním řízení", "Přesunutí sídla do zahraničí",
              "ZRUŠENÍ SPOLEČNOSTI S LIKVIDACÍ"):
    check(f"není zrušená: {nazev[:30]}",
          s.normalizuj({"nazev": nazev, "datum": "2026-11-04"}, "CAK", CAK, "") is not None)
check("prompt AI vynechává zrušené akce", "zrušené akce" in s.VYPIS_AI_PROMPT)

# --- Přejmenovaná akce drží id ------------------------------------------------------
print("Přejmenování")
VYPIS_PF = ["https://www.prf.cuni.cz/events"]


def pf_akce(nazev, datum, **kw):
    return dict({"id": s.akce_id("PFUK", datum, nazev), "poradatel": "PFUK", "datum": datum,
                 "nazev": nazev, "lektori": [], "cena": ""}, **kw)


U = "https://www.prf.cuni.cz/akce/danove-pravo-2026"
stara = pf_akce("Konference: Daňové právo 2026", "2026-10-02", url=U, anotace="Z detailu.",
                detail_pokusy=2, oblasti=["dane"], oblasti_klic="k")
dalsi = [pf_akce(f"P{i}", f"2026-10-1{i}", url=f"https://www.prf.cuni.cz/akce/{i}") for i in range(3)]
nova = pf_akce("Daňové právo 2026", "2026-10-02", url=U, anotace="")
vysl = s.sloucit([stara] + dalsi, "PFUK", [dict(nova)] + [dict(x) for x in dalsi], "ai", DNES, VYPIS_PF)
v = [a for a in vysl if a["datum"] == "2026-10-02"]
check("přejmenování: stejné id a převzaté údaje",
      len(v) == 1 and v[0]["id"] == stara["id"] and v[0]["nazev"] == "Daňové právo 2026"
      and v[0]["detail_pokusy"] == 2 and v[0]["oblasti"] == ["dane"], str(v))
vysl = s.sloucit([stara] + dalsi, "PFUK", [dict(nova)], "ai", DNES, VYPIS_PF)
check("přejmenování v režimu „podezřele málo“ bez duplicity",
      len([a for a in vysl if a["datum"] == "2026-10-02"]) == 1 and len(vysl) == 4, str(vysl))
spolecna = [dict(nova, url=VYPIS_PF[0]), pf_akce("Jiná", "2026-10-02", url=VYPIS_PF[0])]
vysl = s.sloucit([dict(stara, url=VYPIS_PF[0])] + dalsi, "PFUK", spolecna + [dict(x) for x in dalsi],
                 "ai", DNES, VYPIS_PF)
check("odkaz na výpis nepáruje", stara["id"] not in {a["id"] for a in vysl})
vysl = s.sloucit([stara] + dalsi, "PFUK", [dict(nova, datum="2026-10-09", id="jine")] + [dict(x) for x in dalsi],
                 "ai", DNES, VYPIS_PF)
check("jiné datum nepáruje", "jine" in {a["id"] for a in vysl} and stara["id"] not in {a["id"] for a in vysl})

# --- Konec vícedenní akce --------------------------------------------------------------
print("Konec akce")
dvoudenni = ak("Konference", "2026-10-05", datum_do="2026-10-06")
vysl = s.sloucit([dvoudenni], "CAK", [ak("Konference", "2026-10-05")], "parser", DNES)
check("parser: zkrácená akce ztratí datum_do", "datum_do" not in vysl[0], str(vysl[0]))
vysl = s.sloucit([dvoudenni], "CAK", [ak("Konference", "2026-10-05")], "ai", DNES)
check("AI: vynechané datum_do zůstane", vysl[0].get("datum_do") == "2026-10-06")

# --- Neočekávané typy hodnot ----------------------------------------------------------
print("Typy hodnot")
n = s.normalizuj({"nazev": ["Seminář", "GDPR"], "datum": "2026-10-01", "forma": ["online", "prezencne"],
                  "url": ["https://www.cak.cz/akce/5", "https://www.cak.cz/x"], "misto": {"a": 1},
                  "cena": 1200, "lektori": [{"name": "JUDr. A"}, "Mgr. B", None]}, "CAK", CAK, "")
check("normalizuj: seznamy a čísla", n and n["forma"] == "hybridne" and n["url"] == "https://www.cak.cz/akce/5"
      and n["nazev"] == "Seminář GDPR" and n["misto"] == "" and n["cena"] == "1200"
      and n["lektori"] == ["JUDr. A", "Mgr. B"], str(n))
check("normalizuj: forma a url jako slovník",
      s.normalizuj({"nazev": "X", "datum": "2026-10-01", "forma": {}, "url": {"u": 1}}, "CAK", CAK, "") is not None)
check("normalizuj: datum jako seznam",
      s.normalizuj({"nazev": "X", "datum": ["2026-10-01"]}, "CAK", CAK, "")["datum"] == "2026-10-01")

# --- Stránky akcí: počítání pokusů, forma ------------------------------------------------
print("Pokusy o stránku akce")
s.text_detailu = lambda url, hosty=(): ("<html></html>", "text stránky", True)
s.gemini_enabled = lambda: False
bez_ai = [ak(f"B{i}", f"2026-10-0{i + 1}", url=f"https://www.cak.cz/akce/b{i}") for i in range(3)]
s.dopln_detaily(bez_ai, cfgd, 5)
check("bez AI se pokus nepočítá a další stránky se nestahují",
      all(not a.get("detail_pokusy") for a in bez_ai), str([a.get("detail_pokusy") for a in bez_ai]))
s.gemini_enabled = lambda: True
s.gemini_generate_raw = lambda *a, **k: ""
s.ai_pretizena = lambda: True
s.dopln_detaily(bez_ai, cfgd, 5)
check("přetížená AI: pokus se nepočítá", all(not a.get("detail_pokusy") for a in bez_ai))
s.ai_pretizena = lambda: False
s.dopln_detaily(bez_ai[:1], cfgd, 5)
check("AI odmítla stránku (ne přetížení): pokus se počítá", bez_ai[0].get("detail_pokusy") == 1)
s.gemini_generate_raw = lambda *a, **k: "nejde přečíst"
s.dopln_detaily(bez_ai[1:2], cfgd, 5)
check("nečitelná odpověď AI: pokus se počítá", bez_ai[1].get("detail_pokusy") == 1)
s.gemini_generate_raw = lambda *a, **k: json.dumps({"anotace": "Jen anotace, lektoři ani cena nejsou."})
s.dopln_detaily(bez_ai[2:], cfgd, 5)
check("přečtená stránka je hotová, i když údaj chybí",
      bez_ai[2]["detail_pokusy"] == s.DETAIL_POKUSU and not s.potrebuje_detail(bez_ai[2]))
s.text_detailu = lambda url, hosty=(): ("<html></html>", "text bez pozvánky", False)
bez_pozvanky = [ak("P", "2026-10-01", url="https://www.cak.cz/akce/p")]
s.dopln_detaily(bez_pozvanky, cfgd, 5)
check("nestažená pozvánka: stránka se zkusí znovu", bez_pozvanky[0]["detail_pokusy"] == 1)


def spadne(url, hosty=()):
    raise OSError("síť")


s.text_detailu = spadne
sit = [ak("S", "2026-10-01", url="https://www.cak.cz/akce/s")]
s.dopln_detaily(sit, cfgd, 5)
check("chyba stažení se počítá", sit[0]["detail_pokusy"] == 1)

print("Forma ze stránky akce")
s.text_detailu = lambda url, hosty=(): ("", "text", True)
s.z_ai_detailu = lambda text, org, cfg: ({"misto": "Velká geologická posluchárna, Albertov 6", "forma": None,
                                          "anotace": "A", "lektori": ["X"], "cena": "zdarma"}, False)
PFC = {"poradatele": {"PFUK": {"nazev": "PF UK", "hosty": ["cuni.cz"]}, "CAK": CAK}}
chybna = pf_akce("Dezinformace", "2026-10-01", url="https://www.prf.cuni.cz/a/1", forma="online", misto="")
s.dopln_detaily([chybna], PFC, 5, "ai")
check("AI výpis: stránka akce opraví formu a místo",
      chybna["forma"] == "prezencne" and chybna["misto"].startswith("Velká")
      and set(chybna["z_detailu"]) == {"misto", "forma"}, str(chybna))
znovu = {k: v for k, v in chybna.items() if k != "z_detailu"}
znovu.update(forma="online", misto="", anotace="")
vysl = s.sloucit([chybna], "PFUK", [znovu], "ai", DNES)
check("oprava ze stránky akce přežije další čtení výpisu",
      vysl[0]["forma"] == "prezencne" and vysl[0]["misto"].startswith("Velká"), str(vysl[0]))
z_parseru = ak("Online seminář", "2026-10-01", url="https://www.cak.cz/akce/o", forma="online")
s.dopln_detaily([z_parseru], cfgd, 5, "parser")
check("parser: forma z výpisu zůstane", z_parseru["forma"] == "online" and not z_parseru.get("z_detailu"))
s.z_ai_detailu = lambda text, org, cfg: ({"forma": ["online", "prezencne"], "url": ["x"], "anotace": "B"}, False)
typy = ak("T", "2026-10-01", url="https://www.cak.cz/akce/t")
s.dopln_detaily([typy], cfgd, 5)
check("detail: forma jako seznam nespadne", typy["forma"] == "hybridne", str(typy))
s.text_detailu, s.z_ai_detailu = puvodni
s.ai_pretizena = puvodni_pretizena

# --- Rozpočet stránek akcí ----------------------------------------------------------------
print("Rozpočet")
check("rozpočet: kdo potřebuje méně, dostane potřebu, zbytek ČAK",
      s.rozdel_rozpocet({"CAK": 28, "PFUK": 4, "JCP": 0, "UPV": 6}, 80) == {"CAK": 28, "PFUK": 4, "JCP": 0, "UPV": 6})
r = s.rozdel_rozpocet({"A": 50, "B": 5, "C": 50}, 40)
check("rozpočet: nedostatek se dělí férově", r["B"] == 5 and sum(r.values()) == 40 and abs(r["A"] - r["C"]) <= 1, str(r))
check("rozpočet: nic nepotřebuje", s.rozdel_rozpocet({"A": 0}, 80) == {"A": 0})

# --- Tatáž akce u dvou pořadatelů ---------------------------------------------------------
print("Duplicity")
DCFG = {"poradatele": {"PFUK": {}, "EPRAVO": {"prodejce": True}, "CAK": {}}}
pf = {"id": "pf", "poradatel": "PFUK", "datum": "2026-10-02", "nazev": "Konference: Daňové právo 2026",
      "misto": "", "zacatek": "", "lektori": [], "cena": "", "url": "https://www.prf.cuni.cz/a"}
ep = {"id": "ep", "poradatel": "EPRAVO", "datum": "2026-10-02", "nazev": "Daňové právo 2026",
      "misto": "PF UK, místnost 120", "zacatek": "09:00", "konec": "17:00", "lektori": ["A", "B"],
      "cena": "3 025 Kč", "anotace": "x", "url": "https://www.epravo.cz/e/1"}
zobr, dup = s.rozdel_duplicity([dict(ep), dict(pf)], DCFG)
hl = zobr[0] if zobr else {}
check("duplicita: ukáže se pořadatel, prodejce skrytý",
      [a["id"] for a in zobr] == ["pf"] and [a["id"] for a in dup] == ["ep"] and dup[0]["stejna_jako"] == "pf",
      f"{zobr} {dup}")
check("duplicita: doplněné údaje a odkaz „také u“",
      hl.get("misto") == "PF UK, místnost 120" and hl.get("zacatek") == "09:00" and hl.get("cena") == "3 025 Kč"
      and hl.get("take_u") == [{"poradatel": "EPRAVO", "url": "https://www.epravo.cz/e/1"}], str(hl))
brno = {"id": "c1", "poradatel": "CAK", "datum": "2026-10-02", "nazev": "Seminář: Daňové právo 2026",
        "misto": "Brno", "lektori": [], "cena": ""}
zobr, dup = s.rozdel_duplicity([dict(ep), dict(brno)], DCFG)
check("duplicita: jiné místo = jiná akce", len(zobr) == 2 and not dup)
zobr, dup = s.rozdel_duplicity([dict(pf), dict(pf, id="pf2")], DCFG)
check("duplicita: v rámci pořadatele se nespojuje", len(zobr) == 2 and not dup)
zobr, dup = s.rozdel_duplicity([dict(pf, datum="2026-10-03"), dict(ep)], DCFG)
check("duplicita: jiný den = jiná akce", len(zobr) == 2 and not dup)
zobr, dup = s.rozdel_duplicity([dict(pf, stejna_jako="x", take_u=[1])], DCFG)
check("duplicita: značky z minula se přepočítají", "stejna_jako" not in zobr[0] and "take_u" not in zobr[0])

# --- Celý běh (main) nad lokálními soubory ------------------------------------------------
print("Celý běh")
beh = tempfile.mkdtemp()
cfg_beh = {"poradatele": {
    "PFUK": {"nazev": "PF UK", "zkratka": "PF UK", "barva": "#6d28d9",
             "stranky": ["https://www.prf.cuni.cz/events"], "hosty": ["cuni.cz"]},
    "EPRAVO": {"nazev": "epravo.cz", "zkratka": "epravo", "barva": "#15803d", "prodejce": True,
               "stranky": ["https://www.epravo.cz/v"], "hosty": ["epravo.cz"]},
    "UPV": dict(CFG["poradatele"]["UPV"]),
}}
stare_beh = [upv("Espacenet", "2026-10-01", oblasti=[]), dict(upv("Vyřazený pořadatel", "2026-10-03"), poradatel="BECK"),
             dict(pf, oblasti=[]), dict(ep, stejna_jako="pf", oblasti=[])]
for x in stare_beh:
    x["oblasti_klic"] = s.obsah_klic(x)
with open(os.path.join(beh, "config.json"), "w", encoding="utf-8") as f:
    json.dump(cfg_beh, f)
with open(os.path.join(beh, "akce.json"), "w", encoding="utf-8") as f:
    json.dump({"poradatele": {}, "akce": stare_beh[:3], "duplikaty": stare_beh[3:]}, f)


class PevnyCas(s.datetime):
    @classmethod
    def now(cls, tz=None):
        return s.datetime(2026, 9, 25, 12, 0, tzinfo=tz)


puvodni_beh = (s.CONFIG_FILE, s.OUTPUT_FILE, s.ICS_FILE, sys.argv, s.datetime)
s.CONFIG_FILE, s.OUTPUT_FILE, s.ICS_FILE = (os.path.join(beh, "config.json"), os.path.join(beh, "akce.json"),
                                            os.path.join(beh, "akce.ics"))
s.datetime = PevnyCas
s.gemini_enabled = lambda: True
s.gemini_generate_raw = lambda *a, **k: ""   # AI nedostupná
sys.argv = ["scraper_akce.py", "--local", f"UPV={stranka}"]
try:
    s.main()
finally:
    s.CONFIG_FILE, s.OUTPUT_FILE, s.ICS_FILE, sys.argv, s.datetime = puvodni_beh
vystup = s.load_json(os.path.join(beh, "akce.json"))
with open(os.path.join(beh, "akce.ics"), encoding="utf-8", newline="") as f:
    ics_beh = f.read().replace("\r\n ", "")
check("běh: výpadek AI nechá akce pořadatele a zapíše chybu",
      any(a["nazev"] == "Espacenet" for a in vystup["akce"]) and "AI" in (vystup["poradatele"]["UPV"]["chyba"] or ""),
      str(vystup["poradatele"]["UPV"]))
check("běh: akce vyřazeného pořadatele pryč", all(a["poradatel"] in cfg_beh["poradatele"] for a in vystup["akce"]))
check("běh: duplikát zůstává ve stavu, ne ve výpisu ani v ics",
      [a["id"] for a in vystup["duplikaty"]] == ["ep"] and all(a["id"] != "ep" for a in vystup["akce"])
      and "SUMMARY:epravo" not in ics_beh and "Také u epravo: https://www.epravo.cz/e/1" in ics_beh,
      str(vystup["duplikaty"]))
check("běh: počet bez duplikátu", vystup["poradatele"]["EPRAVO"]["pocet"] == 0
      and vystup["poradatele"]["PFUK"]["pocet"] == 1, str(vystup["poradatele"]))
s.gemini_enabled, s.gemini_generate_raw = puvodni_enabled, puvodni_raw

# --- Skutečný config ----------------------------------------------------------
print("Config")
real = s.load_json(s.CONFIG_FILE)
check("šest pořadatelů v pořadí štítků",
      list(real["poradatele"]) == ["CAK", "PFUK", "JCP", "EPRAVO", "ALAI", "UPV"])
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
