#!/usr/bin/env python3
"""Testy scraperu jednání.

Přehledy jednání jsou obyčejné wordové a pdf dokumenty, které soud může
kdykoli přeformátovat – testy proto hlídají dvě různé věci:

  1. Že se ze skutečných dokumentů (tests/fixtures) vytáhne všechno a
     správně. Fixtury jsou originální přehledy MSPH a VS Praha za období
     16.–31. 8. 2026.
  2. Že se změna formátu pozná. Parsování se testuje i nad poškozenými
     variantami dokumentu – scraper má v takovém případě nahlásit, že
     nasbíral míň řádků, než kolik je v dokumentu dat, ne tiše vrátit
     půlku. To je jediná pojistka proti tomu, aby jednání zmizela.

Agendy, které v aktuálních fixturech nejsou (předběžná opatření a žaloby
proti ÚPV), se testují nad syntetickými dokumenty ve stejném formátu.

Spuštění: python test_hearings.py
"""

import io
import json
import re
import sys
import tempfile
import zipfile

import scraper_hearings as s

FIX_MS = "tests/fixtures/msph_civilni_2026-08-16_31.docx"
FIX_VS = "tests/fixtures/vs_civilni_2026-08-17_31.pdf"

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("  OK   " if cond else "  CHYBA") + f" {name}" + (f" – {detail}" if detail and not cond else ""))


def find(items, spz):
    return next((i for i in items if i["spz"] == spz), None)


# --- Stavba syntetických dokumentů ve formátu obou soudů ---

def build_docx(rows, od="16.08.2026", do="31.08.2026", headers=None):
    """Word dokument se stejnou strukturou jako přehled MSPH: odstavec
    s obdobím a tabulka Datum / Síň / Předseda / Spisová značka / Hodina /
    Účastníci."""
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    headers = headers or ["Datum", "Jednací síň", "Předseda senátu",
                          "Spisová značka", "Hodina", "Jména účastníků"]

    def para(text):
        return f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'

    def cell(value):
        parts = value if isinstance(value, list) else [value]
        return "<w:tc>" + "".join(para(p) for p in parts) + "</w:tc>"

    trs = ["<w:tr>" + "".join(cell(h) for h in headers) + "</w:tr>"]
    for r in rows:
        trs.append("<w:tr>" + "".join(cell(c) for c in r) + "</w:tr>")

    xml = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}"><w:body>'
        + para("Přehled")
        + para(f"zasedání senátů v období od  {od}  do  {do}")
        + "<w:tbl>" + "".join(trs) + "</w:tbl>"
        + "</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def build_pdf_text(rows, od="17.08.2026", do="31.08.2026"):
    """Text v podobě, v jaké ho pypdf vytáhne z přehledu VS."""
    out = [
        "Údaje jsou platné ke dni zpracování přehledu.",
        "Přehled",
        f"zasedání senátů v období od {od} do {do}",
        "Datum Jednací Předseda senátu Spisová značka Hodina Jména účastníků",
        "síň",
    ]
    for datum, sin, predseda, spz, hodina, ucastnici in rows:
        out.append(f"{datum} {sin} {predseda} {spz} {hodina} {ucastnici[0]}")
        out.extend(ucastnici[1:])
    return "\n".join(out)


# =====================================================================
print("\n1) Skutečné dokumenty obou soudů")
# =====================================================================
with open(FIX_MS, "rb") as f:
    ms_bytes = f.read()
with open(FIX_VS, "rb") as f:
    vs_bytes = f.read()

ms_items, ms_period = s.parse_jednani_docx(ms_bytes)
vs_items, vs_period = s.parse_jednani_pdf(vs_bytes)

# Kolik řádků v dokumentu doopravdy je, spočítáno nezávisle na parseru:
# v docx každý řádek tabulky, jehož první buňka začíná datem; v pdf každý
# řádek textu začínající datem (období je v hlavičce uprostřed věty, takže
# se do počtu nepřičte).
ms_rows = sum(
    1 for rows in s.docx_tables(ms_bytes)[0] for r in rows
    if r and r[0] and s.DATE_RE.match(r[0][0].strip())
)
vs_rows = len([ln for ln in s.pdf_text(vs_bytes).splitlines()
               if s.DATE_RE.match(ln.strip())])

check("MSPH: naparsovány všechny řádky tabulky",
      len(ms_items) == ms_rows, f"{len(ms_items)} != {ms_rows}")
check("VS: naparsovány všechny řádky", len(vs_items) == vs_rows,
      f"{len(vs_items)} != {vs_rows}")
check("MSPH: období z hlavičky", ms_period == ("2026-08-16", "2026-08-31"), str(ms_period))
check("VS: období z hlavičky", vs_period == ("2026-08-17", "2026-08-31"), str(vs_period))
check("MSPH: každé jednání má datum, značku i předsedu",
      all(i["datum"] and i["spz"] and i["predseda"] for i in ms_items))
check("VS: každé jednání má datum, značku i předsedu",
      all(i["datum"] and i["spz"] and i["predseda"] for i in vs_items))
check("všechna data spadají do období dokumentu",
      all("2026-08-16" <= i["datum"] <= "2026-08-31" for i in ms_items + vs_items))
check("hodiny mají tvar HH:MM",
      all(re.fullmatch(r"\d{1,2}:\d{2}", i["hodina"])
          for i in ms_items + vs_items if i["hodina"]))

# Konkrétní řádky ověřené proti dokumentu.
osa = find(ms_items, "9 C 10/2026")
check("MSPH: řádek OSA proti BH Drink sedí",
      osa and osa["datum"] == "2026-08-20" and osa["hodina"] == "09:40"
      and osa["sin"] == "265" and osa["predseda"] == "JUDr. Mgr. Petr Košík, Ph.D."
      and osa["ucastnici"] == ["OSA z.s.", "BH Drink s.r.o."], json.dumps(osa, ensure_ascii=False))
bayer = find(vs_items, "1 Cmo 16/2026")
check("VS: řádek Bayer proti Accord sedí",
      bayer and bayer["datum"] == "2026-08-19" and bayer["hodina"] == "09:30"
      and bayer["ucastnici"] == ["Bayer AG", "Bayer Intellectual Property GmbH",
                                 "Accord Healtcare S.L.U.", "Accord Healthcare s.r.o."],
      json.dumps(bayer, ensure_ascii=False))
check("VS: dlouhý název se zkrátí na zkratku a strany se spojí",
      bayer and bayer["nazev"] == "Bayer v. Accord Healtcare a další",
      bayer and bayer["nazev"])
check("MSPH: zalomený titul se nestane samostatnou stranou",
      "Ph.D." not in (find(ms_items, "18 Co 104/2026") or {}).get("ucastnici", []))

# =====================================================================
print("\n2) Rozpoznání změny formátu (kvůli tomu, že jde o dokument)")
# =====================================================================
check("úplný docx projde kontrolou úplnosti",
      len(ms_items) >= s.expected_rows(s.raw_text_of(ms_bytes)) * s.MIN_PARSE_RATIO)

# Přehozené sloupce: značka se ocitne tam, kde parser čeká datum.
prehozene = build_docx([
    ["12C 1/2026", "265", "Mgr. Jana Přibylová", "19.08.2026", "09:00", ["A a.s."]],
    ["12C 2/2026", "265", "Mgr. Jana Přibylová", "20.08.2026", "09:00", ["B a.s."]],
])
items, _ = s.parse_jednani_docx(prehozene)
check("přehozené sloupce: nic se nenaparsuje", len(items) == 0, f"{len(items)}")
check("přehozené sloupce: kontrola úplnosti to pozná",
      len(items) < s.expected_rows(s.raw_text_of(prehozene)) * s.MIN_PARSE_RATIO)

# Polovina řádků má rozbitou spisovou značku.
pulka = build_docx(
    [["19.08.2026", "265", "Mgr. Jana Přibylová", f"12C {i}/2026", "09:00", ["X"]]
     for i in range(1, 6)]
    + [["20.08.2026", "265", "Mgr. Jana Přibylová", "bez značky", "09:00", ["Y"]]
       for _ in range(5)])
items, _ = s.parse_jednani_docx(pulka)
check("polovina rozbitých řádků: naparsuje se jen polovina", len(items) == 5, f"{len(items)}")
check("polovina rozbitých řádků: kontrola úplnosti to pozná",
      len(items) < s.expected_rows(s.raw_text_of(pulka)) * s.MIN_PARSE_RATIO)

# Změna formátu u VS: prohozené pořadí hodiny a značky.
vs_rozbite = s.build_pdf_broken = "\n".join([
    "Přehled",
    "zasedání senátů v období od 17.08.2026 do 31.08.2026",
    "17.08.2026 6 Mgr. Jiří Čurda 09:30 3Cmo 25/2026 FLOWBOX s.r.o.",
    "18.08.2026 6 Mgr. Jiří Čurda 10:30 3Cmo 26/2026 DATEX s.r.o.",
])
items, _ = s.parse_jednani_text(vs_rozbite)
check("VS se změněným pořadím sloupců: nic se nenaparsuje", len(items) == 0, f"{len(items)}")

# Dokument bez hlavičky s obdobím se pořád naparsuje (období je volitelné).
bez_obdobi = build_docx(
    [["19.08.2026", "265", "Mgr. Jana Přibylová", "12C 1/2026", "09:00", ["A"]]],
    od="", do="")
items, period = s.parse_jednani_docx(bez_obdobi)
check("chybějící období nezhatí parsování", len(items) == 1 and period is None)

# =====================================================================
print("\n3) Předběžná opatření (rejstřík Nc)")
# =====================================================================
cfg_ms = json.load(open(s.CONFIG_FILE))["courts"]["MS"]

nc_rows = [
    # (značka, předseda) – obě podoby značky, jakou u Nc soudy používají:
    # číslo rejstříku (1 Nc / 2 Nc) i číslo soudního oddělení (12 Nc).
    ["19.08.2026", "265", "Mgr. Jana Přibylová", "2Nc 15/2026", "09:00", ["Xiaomi", "OSA z.s."]],
    ["19.08.2026", "265", "JUDr. Mgr. Petr Košík, Ph.D.", "1Nc 8/2026", "10:00", ["A", "B"]],
    ["19.08.2026", "265", "Mgr. Jana Přibylová", "12Nc 3/2026", "11:00", ["C", "D"]],
    # Nc jiného soudce – korporátní/insolvenční předběžko, do IP nepatří.
    ["19.08.2026", "108", "JUDr. Ivana Kotrčová", "1Nc 9/2026", "12:00", ["E", "F"]],
]
items, _ = s.parse_jednani_docx(build_docx(nc_rows))
s.mark_ip(items, cfg_ms)
check("Nc: naparsována všechna čtyři předběžka", len(items) == 4, f"{len(items)}")
check("Nc podle rejstříku (2 Nc) u IP soudkyně je IP", find(items, "2 Nc 15/2026")["ip"])
check("Nc podle rejstříku (1 Nc) u IP soudce je IP", find(items, "1 Nc 8/2026")["ip"])
check("Nc podle oddělení (12 Nc) je IP", find(items, "12 Nc 3/2026")["ip"])
check("Nc u soudce mimo IP agendu není IP", not find(items, "1 Nc 9/2026")["ip"])
check("Nc: název sporu se odvodí ze stran",
      find(items, "2 Nc 15/2026")["nazev"] == "Xiaomi v. OSA",
      find(items, "2 Nc 15/2026")["nazev"])

# =====================================================================
print("\n4) Žaloby proti ÚPV (úsek správního soudnictví)")
# =====================================================================
upv_rows = [
    ["19.08.2026", "201", "Mgr. Martin Kříž", "15A 12/2026", "09:00",
     ["Xiaomi Inc.", "Úřad průmyslového vlastnictví"]],
    ["19.08.2026", "201", "Mgr. Martin Lachmann", "18A 5/2026", "10:00",
     ["Někdo a.s.", "Urad prumysloveho vlastnictvi"]],       # bez diakritiky
    ["19.08.2026", "201", "Mgr. Martin Kříž", "15A 20/2026", "11:00",
     ["Někdo", "Ministerstvo dopravy"]],                      # jiná správní věc
    ["19.08.2026", "201", "Mgr. Martin Kříž", "15A 21/2026", "12:00", [""]],
    ["19.08.2026", "201", "Mgr. Andrea Veselá", "8A 3/2026", "13:00",
     ["Někdo", "Úřad průmyslového vlastnictví"]],             # senát mimo seznam
]
items, _ = s.parse_jednani_docx(build_docx(upv_rows))
s.mark_ip(items, cfg_ms)
check("ÚPV: 15 A s ÚPV mezi účastníky je IP", find(items, "15 A 12/2026")["ip"])
check("ÚPV: 18 A s ÚPV bez diakritiky je IP", find(items, "18 A 5/2026")["ip"])
check("ÚPV: 15 A v jiné správní věci není IP", not find(items, "15 A 20/2026")["ip"])
check("ÚPV: 15 A bez uvedených účastníků se raději vezme jako IP",
      find(items, "15 A 21/2026")["ip"])
check("ÚPV: senát mimo seznam se neoznačí ani s ÚPV",
      not find(items, "8 A 3/2026")["ip"])

# =====================================================================
print("\n5) Filtr IP senátů v civilních věcech")
# =====================================================================
civ_rows = [
    ["19.08.2026", "1", "Mgr. Jana Přibylová", "12C 7/2026", "09:00", ["A", "B"]],
    ["19.08.2026", "1", "Mgr. Jana Přibylová", "12EC 55/2026", "09:00", ["A", "B"]],
    ["19.08.2026", "1", "Mgr. Jana Přibylová", "12ECm 4/2026", "09:00", ["A", "B"]],
    ["19.08.2026", "1", "JUDr. Ivana Kotrčová", "17Co 178/2026", "09:00", ["A", "B"]],
    ["19.08.2026", "1", "Mgr. Jana Přibylová", "12Co 9/2026", "09:00", ["A", "B"]],
]
items, _ = s.parse_jednani_docx(build_docx(civ_rows))
s.mark_ip(items, cfg_ms)
check("C v IP oddělení je IP", find(items, "12 C 7/2026")["ip"])
check("EC v IP oddělení je IP (velikost písmen rejstříku)", find(items, "12 EC 55/2026")["ip"])
check("ECm v IP oddělení je IP", find(items, "12 ECm 4/2026")["ip"])
check("odvolací Co není IP", not find(items, "17 Co 178/2026")["ip"])
check("stejné číslo oddělení v jiném rejstříku není IP",
      not find(items, "12 Co 9/2026")["ip"])

# =====================================================================
print("\n6) Slučování běhů a výstupy")
# =====================================================================
out = {}
for _ in range(3):
    it, per = s.parse_jednani_docx(ms_bytes)
    for x in it:
        x["usek"] = "civilni"
    s.mark_ip(it, cfg_ms)
    s.merge_output(out, "MS", it, per, None, cfg_ms)
check("opakovaný běh neduplikuje jednání", len(out["jednani"]) == len(ms_items),
      f"{len(out['jednani'])} != {len(ms_items)}")

# Historie a zrušená jednání: jednání, které zmizí z nově vydaného přehledu
# pokrývajícího jeho den, se nesmaže, jen označí jako zrušené. Termíny mimo
# období nového přehledu (starší historie) zůstávají nedotčené.
hist = {}
prvni = [
    ["19.08.2026", "265", "Mgr. Jana Přibylová", "12C 1/2026", "09:00", ["A", "B"]],
    ["20.08.2026", "265", "Mgr. Jana Přibylová", "12C 2/2026", "10:00", ["C", "D"]],
]
it, per = s.parse_jednani_docx(build_docx(prvni, od="16.08.2026", do="22.08.2026"))
for x in it:
    x["usek"] = "civilni"
s.mark_ip(it, cfg_ms)
s.merge_output(hist, "MS", it, per, None, cfg_ms)

# Novější přehled na týž týden už druhé jednání neuvádí.
druhy = [prvni[0]]
it, per = s.parse_jednani_docx(build_docx(druhy, od="16.08.2026", do="22.08.2026"))
for x in it:
    x["usek"] = "civilni"
s.mark_ip(it, cfg_ms)
s.merge_output(hist, "MS", it, per, None, cfg_ms)

check("zmizelé jednání se nesmaže", len(hist["jednani"]) == 2, str(len(hist["jednani"])))
check("zmizelé jednání se označí jako zrušené",
      find(hist["jednani"], "12 C 2/2026")["zruseno"] is True)
check("jednání, které v přehledu zůstalo, zrušené není",
      find(hist["jednani"], "12 C 1/2026")["zruseno"] is False)

# Starší termín mimo období nového přehledu se zrušit nesmí.
stary = [["01.07.2026", "265", "Mgr. Jana Přibylová", "12C 99/2025", "09:00", ["E", "F"]]]
it, _ = s.parse_jednani_docx(build_docx(stary))
for x in it:
    x["usek"] = "civilni"
s.mark_ip(it, cfg_ms)
s.merge_output(hist, "MS", it, None, None, cfg_ms)
it, per = s.parse_jednani_docx(build_docx(druhy, od="16.08.2026", do="22.08.2026"))
for x in it:
    x["usek"] = "civilni"
s.mark_ip(it, cfg_ms)
s.merge_output(hist, "MS", it, per, None, cfg_ms)
check("historie mimo období nového přehledu zůstává platná",
      find(hist["jednani"], "12 C 99/2025")["zruseno"] is False)
check("proběhlá jednání se nepromazávají podle stáří",
      any(j["datum"] == "2026-07-01" for j in hist["jednani"]))

# Dva úseky téhož soudu se nepřepisují navzájem.
it2, _ = s.parse_jednani_docx(build_docx(upv_rows))
for x in it2:
    x["usek"] = "spravni"
s.mark_ip(it2, cfg_ms)
s.merge_output(out, "MS", it2, ("2026-08-16", "2026-08-31"), None, cfg_ms)
check("správní úsek nepřepíše civilní", len(out["jednani"]) == len(ms_items) + len(it2),
      f"{len(out['jednani'])}")
check("v metadatech jsou oba úseky",
      set(out["courts"]["MS"]["useky"]) == {"civilni", "spravni"})

ics_path = tempfile.mkstemp(suffix=".ics")[1]   # ať test nesahá na ostrý výstup
out["ics"] = s.write_ics(out, ics_path)
try:
    from icalendar import Calendar
    cal = Calendar.from_ical(open(ics_path, "rb").read())
    evs = list(cal.walk("VEVENT"))
    check("ICS je platný a obsahuje jen IP jednání",
          len(evs) == sum(1 for j in out["jednani"] if j["ip"]), f"{len(evs)}")
    check("ICS má časovou zónu", len(list(cal.walk("VTIMEZONE"))) == 1)
    check("ICS: události mají jméno sporu a odkaz na InfoSoud",
          all(str(e.get("SUMMARY")) and "infosoud" in str(e.get("URL")) for e in evs))
except ImportError:
    print("  (přeskočeno: knihovna icalendar není nainstalovaná)")

# =====================================================================
print("\n7) Odkaz na InfoSoud")
# =====================================================================
courts_meta = {"VS": {"infosoud_org": "VSPHAAB"}, "MS": {"infosoud_org": "MSPHAAB"}}
tv_nova = dict(find(vs_items, "3 Co 24/2025"), soud="VS")
check("odkaz na detail řízení sedí s ověřenou podobou",
      s.infosoud_url(tv_nova, courts_meta) ==
      "https://infosoud.gov.cz/InfoSoud/detail-rizeni?typOrganizace=VSECHNY_KRAJE"
      "&druhOrganizace=VSPHAAB&cisloSenatu=3&druhVeci=co&bcVec=24&rocnik=2025",
      s.infosoud_url(tv_nova, courts_meta))
check("rejstřík jde do odkazu malými písmeny i u víceznakových",
      "&druhVeci=ecm&" in s.infosoud_url(
          {"soud": "MS", "cislo_senatu": 12, "rejstrik": "ECm", "bc": 4, "rocnik": 2026},
          courts_meta))

# =====================================================================
failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} testů prošlo")
if failed:
    print("Neprošlo:")
    for n in failed:
        print("  -", n)
sys.exit(1 if failed else 0)
