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

# Dva přehledy na jedné stránce (MSPH): každý se vybírá jen podle vlastních
# slov, aby se za chybějící správní dokument nevzal civilní a jeho jednání
# se nezapsala pod cizí úsek. Soud s jediným přehledem si první odkaz vezme
# i bez shody, ať přejmenovaný dokument nezastaví celý běh.
odkazy = [
    ("https://msp.gov.cz/documents/d/ms/civilni-usek-1-15-9-2026", "Občanskoprávní",
     "https://msp.gov.cz/documents/d/ms/civilni-usek-1-15-9-2026 obcanskopravni"),
    ("https://msp.gov.cz/documents/d/ms/spravni-usek-1-15-9-2026", "Správní",
     "https://msp.gov.cz/documents/d/ms/spravni-usek-1-15-9-2026 spravni"),
]
obecna = ("jednani", "prehled")
check("výběr dokumentu: správní přehled se najde podle vlastního slova",
      s.pick_link(odkazy, obecna, ("spravni",), strict=True)[0] == odkazy[1][0])
check("výběr dokumentu: chybějící správní přehled nenahradí civilní",
      s.pick_link(odkazy[:1], obecna, ("spravni",), strict=True) == (None, None))
check("výběr dokumentu: jediný přehled soudu se vezme i bez shody",
      s.pick_link(odkazy[:1], obecna, ("kalendar",))[0] == odkazy[0][0])

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
# Kritérium je žalovaný, ne senát: správní oddělení soudí všechnu správní
# agendu, takže rozhoduje, jestli je mezi účastníky ÚPV. Přehled strany
# nerozlišuje, ale ve správním soudnictví je úřad vždycky žalovaný.
cfg_vs = json.load(open(s.CONFIG_FILE))["courts"]["VS"]
upv_rows = [
    ["19.08.2026", "201", "Mgr. Martin Kříž", "15A 12/2026", "09:00",
     ["Xiaomi Inc.", "Úřad průmyslového vlastnictví"]],
    ["19.08.2026", "201", "Mgr. Martin Lachmann", "18A 5/2026", "10:00",
     ["Někdo a.s.", "Urad prumysloveho vlastnictvi"]],       # bez diakritiky
    ["19.08.2026", "201", "Mgr. Martin Kříž", "15A 20/2026", "11:00",
     ["Někdo", "Ministerstvo dopravy"]],                      # jiná správní věc
    ["19.08.2026", "201", "Mgr. Martin Kříž", "15A 21/2026", "12:00", [""]],
    ["19.08.2026", "201", "Mgr. Andrea Veselá", "8A 3/2026", "13:00",
     ["Někdo", "Úřad průmyslového vlastnictví"]],             # senát mimo rozvrh IP
    ["20.08.2026", "201", "Mgr. Andrea Veselá", "9A 4/2026", "09:00",
     ["J. N.", "ÚPV"]],                                        # jen zkratka
    ["20.08.2026", "201", "Mgr. Andrea Veselá", "9A 6/2026", "10:00",
     ["SUPVOLT s.r.o.", "Energetický regulační úřad"]],       # „upv" uvnitř slova
    ["20.08.2026", "201", "Mgr. Andrea Veselá", "10A 7/2026", "11:00",
     ["Firma s.r.o.", "Úřadu průmyslového vlastnictví ČR"]],  # skloněno, s dovětkem
    ["21.08.2026", "201", "JUDr. Ladislav Hejtmánek", "6A 38/2025", "09:30",
     ["Kverulant.org o.p.s.", "Úřad pro ochranu osobních údajů"]],  # jiný úřad
]
items, _ = s.parse_jednani_docx(build_docx(upv_rows))
s.mark_ip(items, cfg_ms)
check("ÚPV: 15 A s ÚPV mezi účastníky je IP", find(items, "15 A 12/2026")["ip"])
check("ÚPV: 18 A s ÚPV bez diakritiky je IP", find(items, "18 A 5/2026")["ip"])
check("ÚPV: 15 A v jiné správní věci není IP", not find(items, "15 A 20/2026")["ip"])
check("ÚPV: řádek bez účastníků není IP (bez senátu není o co se opřít)",
      not find(items, "15 A 21/2026")["ip"])
check("ÚPV: rozhoduje žalovaný, ne senát – 8 A s ÚPV je IP",
      find(items, "8 A 3/2026")["ip"])
check("ÚPV: stačí zkratka ÚPV", find(items, "9 A 4/2026")["ip"])
check('ÚPV: „upv" uvnitř jiného jména neplatí', not find(items, "9 A 6/2026")["ip"])
check("ÚPV: skloněný název s dovětkem platí", find(items, "10 A 7/2026")["ip"])
check("ÚPV: jiný úřad (ÚOOÚ) není IP", not find(items, "6 A 38/2025")["ip"])
check("ÚPV: v popisku sporu je zkratka úřadu",
      find(items, "15 A 12/2026")["nazev"] == "Xiaomi v. ÚPV"
      and find(items, "10 A 7/2026")["nazev"] == "Firma v. ÚPV",
      find(items, "15 A 12/2026")["nazev"] + " / " + find(items, "10 A 7/2026")["nazev"])
check("ÚPV: config nemá správní senáty v seznamu IP senátů (jde jen podle účastníka)",
      not any(x.split()[-1] == "A" for x in cfg_ms["senaty"])
      and "senaty_ucastnik" not in cfg_ms and cfg_ms.get("ucastnici_ip"))

# Totéž pravidlo platí u VS Praha, kdyby se ÚPV objevil v jeho přehledu –
# senát tam o IP nerozhoduje.
vs_upv = build_pdf_text([
    ("19.08.2026", "6", "JUDr. Jan Novák", "7Co 12/2026", "09:00",
     ["Firma s.r.o.", "Úřad průmyslového vlastnictví"]),
    ("19.08.2026", "6", "JUDr. Jan Novák", "7Co 13/2026", "10:00",
     ["Firma s.r.o.", "Jiná firma a.s."]),
])
items, _ = s.parse_jednani_text(vs_upv)
s.mark_ip(items, cfg_vs)
check("VS: ÚPV mezi účastníky je IP i mimo IP senáty", find(items, "7 Co 12/2026")["ip"])
check("VS: bez ÚPV mimo IP senáty není IP", not find(items, "7 Co 13/2026")["ip"])

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
ms_ip = sum(1 for j in s.mark_ip([dict(x) for x in ms_items], cfg_ms)
            if j["ip"])
check("opakovaný běh neduplikuje jednání", len(out["jednani"]) == ms_ip,
      f"{len(out['jednani'])} != {ms_ip}")
check("do archivu jdou jen IP jednání",
      ms_ip < len(ms_items) and all(j["ip"] for j in out["jednani"]),
      f"{ms_ip} z {len(ms_items)}")

# Historie a odvolaná jednání: jednání, které zmizí z nově vydaného přehledu
# pokrývajícího jeho den, soud odvolal nebo přeložil – smaže se. Termíny mimo
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

check("jednání zmizelé z nového přehledu se smaže",
      find(hist["jednani"], "12 C 2/2026") is None)
check("jednání, které v přehledu zůstalo, zůstává",
      find(hist["jednani"], "12 C 1/2026") is not None)

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
check("historie mimo období nového přehledu zůstává",
      find(hist["jednani"], "12 C 99/2025") is not None)
check("proběhlá jednání se nepromazávají podle stáří",
      any(j["datum"] == "2026-07-01" for j in hist["jednani"]))

# Když oddělení vypadne z rozvrhu ze seznamu IP senátů, jeho jednání z archivu
# zmizí – nesmí se tvářit jako odvolané, protože soud ho pořád nařizuje.
uzsi = dict(cfg_ms, senaty=[x for x in cfg_ms["senaty"] if x != "12 C"])
bez_12c = json.loads(json.dumps(hist))
it, per = s.parse_jednani_docx(build_docx(druhy, od="16.08.2026", do="22.08.2026"))
for x in it:
    x["usek"] = "civilni"
s.mark_ip(it, uzsi)
s.merge_output(bez_12c, "MS", it, per, None, uzsi)
check("senát vyřazený z rozvrhu odejde z archivu",
      find(bez_12c["jednani"], "12 C 1/2026") is None)

# Dva úseky téhož soudu se nepřepisují navzájem.
it2, _ = s.parse_jednani_docx(build_docx(upv_rows))
for x in it2:
    x["usek"] = "spravni"
s.mark_ip(it2, cfg_ms)
s.merge_output(out, "MS", it2, ("2026-08-16", "2026-08-31"), None, cfg_ms)
upv_ip = sum(1 for j in it2 if j["ip"])
check("správní úsek nepřepíše civilní", len(out["jednani"]) == ms_ip + upv_ip,
      f"{len(out['jednani'])} != {ms_ip} + {upv_ip}")
check("v metadatech jsou oba úseky",
      set(out["courts"]["MS"]["useky"]) == {"civilni", "spravni"})
check("v metadatech je jméno úseku pro stránku a .ics",
      out["courts"]["MS"]["useky"]["spravni"].get("nazev") == "Úsek správního soudnictví"
      and out["courts"]["MS"]["useky"]["civilni"].get("nazev") == "Civilní úsek",
      str({u: m.get("nazev") for u, m in out["courts"]["MS"]["useky"].items()}))

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
    check("ICS: u žaloby proti ÚPV je v popisu úsek, u civilní věci ne",
          any("Úsek správního soudnictví" in str(e.get("DESCRIPTION")) for e in evs)
          and not any("Civilní úsek" in str(e.get("DESCRIPTION")) for e in evs))
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
print("\n8) Změny proti minulému přehledu")
# =====================================================================
# InfoSoud je slupka vykreslená javascriptem, stav řízení z něj vytáhnout
# nejde. Co se s jednáním stalo, se proto pozná porovnáním dvou po sobě
# jdoucích přehledů téhož období.
from datetime import date as _date, timedelta as _td
dnes = _date(2026, 8, 31)


def jed(spz, datum, hodina="9:00", sin="101"):
    return {"spz": spz, "datum": datum, "hodina": hodina, "sin": sin}


stare_j = [jed("12 C 1/2026", "2026-09-02"), jed("12 C 2/2026", "2026-09-03"),
           jed("12 C 3/2026", "2026-09-04"), jed("12 C 4/2026", "2026-09-07")]
nove_j = [jed("12 C 1/2026", "2026-09-02"),            # beze změny
          jed("12 C 2/2026", "2026-09-10"),            # přeloženo
          jed("12 C 4/2026", "2026-09-07", hodina="11:30"),   # jiný čas
          jed("12 C 9/2026", "2026-09-11")]            # nové
# 12 C 3/2026 v novém dokumentu není vůbec – soud ho odvolal.
v_prehledu = {(j["spz"], j["datum"]) for j in nove_j}
zmeny = s.porovnej_prehled(stare_j, nove_j, v_prehledu, "MS", "civilni", dnes)
dle_znacky = {z["spz"]: z for z in zmeny}

check("najdou se všechny čtyři změny", len(zmeny) == 4, str(zmeny))
check("nové jednání se pozná", dle_znacky.get("12 C 9/2026", {}).get("typ") == "nove")
check("přeložené jednání se pozná i s oběma daty",
      dle_znacky.get("12 C 2/2026") == {
          "soud": "MS", "usek": "civilni", "spz": "12 C 2/2026", "typ": "presun",
          "datum": "2026-09-10", "z": "2026-09-03", "na": "2026-09-10",
          "kdy": "2026-08-31", "popis": "přeloženo"},
      str(dle_znacky.get("12 C 2/2026")))
check("odvolané jednání se pozná",
      dle_znacky.get("12 C 3/2026", {}).get("typ") == "zruseno")
check("posunutá hodina se pozná",
      dle_znacky.get("12 C 4/2026", {}).get("typ") == "cas"
      and dle_znacky["12 C 4/2026"]["na"] == "11:30",
      str(dle_znacky.get("12 C 4/2026")))
check("beze změny se nic nehlásí", "12 C 1/2026" not in dle_znacky)

# Věc, kterou soud v dokumentu vypsal, jen už není v IP agendě (změna
# rozvrhu práce), se neodvolala – z archivu vypadne, ale změna to není.
zmeny_neip = s.porovnej_prehled(
    [jed("12 C 3/2026", "2026-09-04")], [],
    {("12 C 3/2026", "2026-09-04")}, "MS", "civilni", dnes)
check("jednání vypsané v dokumentu se nehlásí jako odvolané", zmeny_neip == [],
      str(zmeny_neip))

# Stejnou změnu najde každý další běh znovu; ukládá se jen jednou a jen měsíc.
opakovana = dict(dle_znacky["12 C 9/2026"])
starsi = dict(opakovana, kdy=(dnes - _td(days=40)).isoformat(), spz="12 C 8/2026")
orezane = s.orez_zmeny([opakovana, dict(opakovana, kdy="2026-08-25"), starsi])
check("stejná změna se neuloží dvakrát", len(orezane) == 1, str(orezane))
check("uloží se datum prvního výskytu", orezane[0]["kdy"] == "2026-08-25",
      str(orezane[0]["kdy"]))
check("změny starší než měsíc odpadnou",
      not [z for z in orezane if z["spz"] == "12 C 8/2026"])

# =====================================================================
print("\n9) Zkrácení fyzických osob mezi účastníky na iniciály")
# =====================================================================
check("iniciály jména a příjmení", s.initials("Ing. Tomáš Seidl") == "T. S.",
      s.initials("Ing. Tomáš Seidl"))
check("akademický titul za jménem se do iniciál nepočítá",
      s.initials("Ing. Jan Babák CSc.") == "J. B.", s.initials("Ing. Jan Babák CSc."))
check("složený titul (Ing. arch.) se ořízne celý",
      s.initials("Ing. arch. Martin Pálka") == "M. P.",
      s.initials("Ing. arch. Martin Pálka"))
check("hotové iniciály se dalším během nezmění", s.initials("T. S.") == "T. S.",
      s.initials("T. S."))


def s_ai(odpoved):
    """Spustí blok s nasimulovanou odpovědí AI (None = AI vůbec neběží).

    `odpoved` je buď hotová odpověď na každé volání, nebo funkce, která ji
    spočítá z textu dotazu (na dávku po dávce různě). Texty dotazů se
    ukládají do `dotazy`, ať se dá zkontrolovat, na co se scraper ptal.
    """
    class Sim:
        def odpovez(self, prompt, text, **kw):
            self.dotazy.append(text)
            return odpoved(text) if callable(odpoved) else odpoved

        def __enter__(self):
            self.dotazy = []
            self.puvodni = (s.gemini_enabled, s.gemini_generate_raw)
            s.gemini_enabled = lambda: odpoved is not None
            s.gemini_generate_raw = self.odpovez
            return self

        def __exit__(self, *e):
            s.gemini_enabled, s.gemini_generate_raw = self.puvodni

    return Sim()


def jmena_dotazu(text):
    """Jména, na která se jedna dávka ptala."""
    return re.findall(r"^- (.+)$", text, re.M)


# Kdo je fyzická osoba, rozhoduje jedině AI – z tvaru jména to uhádnout
# nejde, „Karolína Janáčková" a „Yunnan Tobacco" vypadají stejně. Jméno,
# které AI vrátí, ale v seznamu nebylo (halucinace), se zahodí.
strany = ["Ing. Tomáš Seidl", "Karolína Janáčková", "Yunnan Tobacco",
          "FLOWBOX s.r.o."]
with s_ai('["Ing. Tomáš Seidl", "Karolína Janáčková", "Vymyšlené Jméno"]'):
    polozky = [{"ucastnici": list(strany)}]
    s.redact_osoby(polozky)
check("AI zkrátí osobu s titulem i bez něj, firmy nechá",
      polozky[0]["ucastnici"] == ["T. S.", "K. J.", "Yunnan Tobacco",
                                  "FLOWBOX s.r.o."],
      str(polozky[0]["ucastnici"]))
check("nazev se dopočítá ze zkrácených jmen",
      polozky[0]["nazev"] == "T. S. v. K. J. a další", polozky[0]["nazev"])

# Strany se párují z původních jmen, ne z iniciál: „K. J." a „K. Š." mají
# shodné první slovo, takže by z dvou lidí vyšla jedna strana a popisek by
# ukázal cizí protistranu. Firmy se přitom spojovat musí dál.
with s_ai('["Karolína Janáčková", "Karla Šolcová", "Stanislav Janáček"]'):
    dva_ka = [{"ucastnici": ["Karolína Janáčková", "Karla Šolcová",
                             "Stanislav Janáček"]}]
    s.redact_osoby(dva_ka)
check("dvě osoby se stejným iniciálem zůstanou dvě strany",
      dva_ka[0]["nazev"] == "K. J. v. K. Š. a další", dva_ka[0]["nazev"])
check("jedna strana vypsaná víc jmény se pořád spojí",
      s.party_label(["Bayer AG", "Bayer Intellectual Property GmbH",
                     "Accord Healtcare S.L.U."]) == "Bayer v. Accord Healtcare",
      s.party_label(["Bayer AG", "Bayer Intellectual Property GmbH",
                     "Accord Healtcare S.L.U."]))

# Se stejným voláním se dočistí i archiv – jednání uložená dřív (tenkrát
# třeba celým jménem) projdou stejnou klasifikací.
with s_ai('["Karolína Janáčková"]'):
    nova = [{"ucastnici": ["FLOWBOX s.r.o."]}]
    stara = [{"ucastnici": ["Karolína Janáčková"], "nazev": "Karolína Janáčková"}]
    s.redact_osoby(nova, stara)
check("archiv se dočistí zároveň s novým přehledem",
      stara[0]["ucastnici"] == ["K. J."] and stara[0]["nazev"] == "K. J.",
      str(stara[0]))

# Bez AI (vypnutá, bez klíče, nedovolala se) se celé jméno nezveřejní:
# nová jednání se uloží bez účastníků a příští běh je načte z přehledu
# znovu. Archiv se nemaže – ten už se z ničeho nedoplní.
with s_ai(None):
    nova = [{"ucastnici": ["Karolína Janáčková"], "nazev": "Karolína Janáčková"}]
    stara = [{"ucastnici": ["T. S."], "nazev": "T. S."}]
    s.redact_osoby(nova, stara)
check("bez AI se nové jednání uloží bez účastníků",
      nova[0]["ucastnici"] == [] and nova[0]["nazev"] == "", str(nova[0]))
check("bez AI zůstane archiv nedotčený",
      stara[0]["ucastnici"] == ["T. S."], str(stara[0]))

# Totéž, když se AI dovolá, ale odpoví nesmysl místo JSON pole.
with s_ai("Promiňte, nerozumím zadání."):
    nova = [{"ucastnici": ["Karolína Janáčková"], "nazev": "Karolína Janáčková"}]
    s.redact_osoby(nova)
check("rozbitá odpověď AI se bere jako nerozhodnuto",
      nova[0]["ucastnici"] == [], str(nova[0]))

# Na jména se AI ptá po dávkách. Do stropu odpovědi se musí vejít i to, co
# si model promyslí, a nad celým archivem najednou se nevejde – odpověď
# skončí na MAX_TOKENS a nerozhodnuto je všechno, včetně jmen z čerstvého
# přehledu. S archivem tak roste počet volání, ne délka jedné odpovědi.
vsichni = json.dumps  # odpověď „všichni ze seznamu jsou lidé"
mnoho = [{"ucastnici": [f"Jana Novakova{i:02d}"]}
         for i in range(s.OSOBY_DAVKA + 5)]
with s_ai(lambda text: vsichni(jmena_dotazu(text))) as sim:
    s.redact_osoby(mnoho)
check("na víc jmen, než je dávka, se AI zeptá víc voláními",
      len(sim.dotazy) == 2, f"volání: {len(sim.dotazy)}")
check("jedna dávka nepřesáhne strop",
      all(len(jmena_dotazu(d)) <= s.OSOBY_DAVKA for d in sim.dotazy),
      str([len(jmena_dotazu(d)) for d in sim.dotazy]))
check("zkrátí se jména ze všech dávek",
      all(it["ucastnici"] == ["J. N."] for it in mnoho),
      str({it["ucastnici"][0] for it in mnoho}))

# Nepovedená dávka zůstane jen ve svých jménech – ostatní jednání se
# klasifikují dál, místo aby přehled skončil bez účastníků celý.
zdenek = {"ucastnici": ["Zdeněk Rozbitý"], "nazev": "Zdeněk Rozbitý"}
ostatni = [{"ucastnici": [f"Osoba Novakova{i:02d}"]}
           for i in range(s.OSOBY_DAVKA)]


def rozbita_davka(text):
    jmena = jmena_dotazu(text)
    # Jména jdou do dávek seřazená, takže „Zdeněk" je sám ve druhé dávce.
    return "Promiňte, nerozumím zadání." if "Zdeněk Rozbitý" in jmena else vsichni(jmena)


with s_ai(rozbita_davka):
    s.redact_osoby(ostatni + [zdenek])
check("jméno z nepovedené dávky se nezveřejní",
      zdenek["ucastnici"] == [] and zdenek["nazev"] == "", str(zdenek))
check("jednání z povedené dávky se zkrátí i tak",
      all(it["ucastnici"] == ["O. N."] for it in ostatni),
      str({it["ucastnici"][0] for it in ostatni}))

# Přehled soudu pokrývá pořád stejné období, takže každý běh čte tatáž
# jednání znovu celými jmény a klasifikuje je od nuly. Když AI zrovna
# vypadne, nesmí tím jednání přijít o jméno, které už jednou dostalo –
# jinak se prázdná jednání mezi běhy jen střídají, místo aby ubývala.
znovu = [{"spz": "1 Cmo 5/2026", "datum": "2026-09-30",
          "ucastnici": ["Karolína Janáčková"], "nazev": "Karolína Janáčková"}]
archiv = [{"spz": "1 Cmo 5/2026", "datum": "2026-09-30",
           "ucastnici": ["K. J."], "nazev": "K. J."}]
with s_ai(None):
    s.redact_osoby(znovu, archiv)
check("bez AI se celé jméno neuloží ani napodruhé",
      znovu[0]["ucastnici"] == [], str(znovu[0]))
s.prevzit_z_archivu(znovu, archiv)
check("jednání si nechá jméno z minulého běhu",
      znovu[0]["nazev"] == "K. J." and znovu[0]["ucastnici"] == ["K. J."],
      str(znovu[0]))

# Přebírá se podle značky a dne dohromady – přeložené jednání je pro archiv
# jiný záznam a cizí účastníky si přitáhnout nesmí.
jinyden = [{"spz": "1 Cmo 5/2026", "datum": "2026-10-07",
            "ucastnici": [], "nazev": ""}]
s.prevzit_z_archivu(jinyden, archiv)
check("jednání z jiného dne si účastníky nepřitáhne",
      jinyden[0]["ucastnici"] == [] and jinyden[0]["nazev"] == "",
      str(jinyden[0]))

# Co archiv nezná (úplně nové jednání), zůstane prázdné – radši holá
# spisovka než celé jméno, o kterém AI nerozhodla.
nove = [{"spz": "9 Cmo 1/2026", "datum": "2026-09-30",
         "ucastnici": [], "nazev": ""}]
s.prevzit_z_archivu(nove, archiv)
check("nové jednání bez předlohy zůstane prázdné",
      nove[0]["ucastnici"] == [], str(nove[0]))

# Úspěšně zkrácené jednání se z archivu nepřepisuje.
cerstve = [{"spz": "1 Cmo 5/2026", "datum": "2026-09-30",
            "ucastnici": ["XTV s.r.o.", "T. M."], "nazev": "XTV v. T. M."}]
s.prevzit_z_archivu(cerstve, archiv)
check("čerstvě zkrácené jednání se nepřepíše starším",
      cerstve[0]["nazev"] == "XTV v. T. M.", str(cerstve[0]))

# Jméno uložené už jako iniciály se dál zkracovat nedá, tak se na ně AI
# neptáme – jinak by dotaz s každým dalším přehledem nafukoval archiv.
with s_ai("[]") as sim:
    s.redact_osoby([{"ucastnici": ["FLOWBOX s.r.o."]}], [{"ucastnici": ["T. S."]}])
check("na hotové iniciály se AI už neptá",
      "T. S." not in " ".join(sim.dotazy), " ".join(sim.dotazy))

# =====================================================================
print("\n10) Sestavy senátů z rozvrhu práce")
# =====================================================================
# Kalendář pod mřížkou ukazuje, kdo senátům předsedá a kdo v nich sedí.
# Dvojice senát → soudci vrací AI z rozvrhu; dřív se zahazovaly a zbyly jen
# ploché seznamy.


def mini_pdf(text):
    """Jednostránkové PDF s jedním řádkem textu (ASCII) – stačí na to, aby
    ho pypdf přečetl a aby prošlo filtrem IP klíčových slov."""
    obsah = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objekty = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(obsah) + obsah + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, odkazy = b"%PDF-1.4\n", []
    for i, o in enumerate(objekty, 1):
        odkazy.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objekty) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in odkazy)
    return out + (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
                  % (len(objekty) + 1, xref))


odpoved_rozvrh = json.dumps({
    "senaty": [
        {"senat": "1 Cmo", "predseda": "JUDr. Roman Horáček, MBA",
         "clenove": ["JUDr. Filip Havrda", "Mgr. Michal Výtisk"],
         "agenda": "duševní vlastnictví"},
        {"senat": "2 Co", "predseda": "JUDr. Roman Horáček, MBA",
         "clenove": ["JUDr. Filip Havrda", "Mgr. Michal Výtisk"],
         "agenda": "duševní vlastnictví"},
        {"senat": "3 Cmo", "predseda": "JUDr. Jiří Čurda", "clenove": [],
         "agenda": "nekalá soutěž"},
    ],
    "soudci": ["Jiří Čurda", "Roman Horáček"],
}, ensure_ascii=False)

senaty_r, soudci_r, sestavy_r = s.rozvrh_z_odpovedi(json.loads(odpoved_rozvrh))
check("ploché senáty zůstávají jako dřív", senaty_r == ["1 Cmo", "2 Co", "3 Cmo"],
      str(senaty_r))
check("soudci bez titulů", soudci_r == ["Jiří Čurda", "Roman Horáček"], str(soudci_r))
check("senáty se stejnou sestavou se sloučí",
      [x["senaty"] for x in sestavy_r] == [["1 Cmo", "2 Co"], ["3 Cmo"]],
      str(sestavy_r))
check("sestava nese předsedu, členy a agendu bez titulů",
      sestavy_r[0] == {"senaty": ["1 Cmo", "2 Co"], "predseda": "Roman Horáček",
                       "clenove": ["Filip Havrda", "Michal Výtisk"],
                       "agenda": "duševní vlastnictví"}, str(sestavy_r[0]))
check("rozbitá odpověď AI nic nevrátí", s.rozvrh_z_odpovedi("nesmysl") == ([], [], []))

pdf_rozvrh = mini_pdf("Senat 1 Cmo: spory z prava autorskeho a prumyslove vlastnictvi")
cfg_vs = {"courts": {"VS": {"nazev": "Vrchní soud v Praze", "senaty": ["1 Cmo"],
                            "soudci": ["Roman Horáček"]}}}
with s_ai(odpoved_rozvrh) as sim:
    zmena = s.update_rozvrh(cfg_vs, "VS", pdf_rozvrh, "https://example.test/rozvrh.pdf")
check("rozvrh se sestavami se uloží", zmena and
      cfg_vs["courts"]["VS"]["sestavy"] == sestavy_r, str(cfg_vs["courts"]["VS"]))

# Stejný rozvrh podruhé: sestavy už jsou, AI se znovu neptá.
with s_ai(odpoved_rozvrh) as sim:
    zmena = s.update_rozvrh(cfg_vs, "VS", pdf_rozvrh, "https://example.test/rozvrh.pdf")
check("nezměněný rozvrh se sestavami se znovu neposílá AI",
      not zmena and not sim.dotazy, str(sim.dotazy))

# Konfigurace z doby před sestavami (hash sedí, sestavy chybí): AI se jednou
# zeptá i beze změny rozvrhu, jinak by sestavy čekaly na další změnu rozvrhu.
cfg_stary = {"courts": {"VS": {"nazev": "Vrchní soud v Praze", "senaty": ["1 Cmo"],
                               "rozvrh_zdroj": dict(cfg_vs["courts"]["VS"]["rozvrh_zdroj"])}}}
with s_ai(odpoved_rozvrh) as sim:
    zmena = s.update_rozvrh(cfg_stary, "VS", pdf_rozvrh, "https://example.test/rozvrh.pdf")
check("chybějící sestavy se doplní i z nezměněného rozvrhu",
      zmena and cfg_stary["courts"]["VS"].get("sestavy") == sestavy_r,
      str(cfg_stary["courts"]["VS"]))

# =====================================================================
failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} testů prošlo")
if failed:
    print("Neprošlo:")
    for n in failed:
        print("  -", n)
sys.exit(1 if failed else 0)
