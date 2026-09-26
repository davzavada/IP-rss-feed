#!/usr/bin/env python3
"""Testy dvoutýdenního přehledu (digest.py) – bez sítě a bez AI.

Okna judikatury a časopisy se podstrčí do dočasného adresáře místo docs/;
ověřuje se, co jde do přehledu (jen IP/IT) a jak se čte odpověď modelu.

Spuštění: python test_digest.py
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import digest
import feed_common as fc

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("  OK   " if cond else "  CHYBA") + f" {name}"
          + (f" – {detail}" if detail and not cond else ""))


ted = datetime.now(timezone.utc)


def iso(hodin):
    return (ted - timedelta(hours=hodin)).strftime("%Y-%m-%dT%H:%M:%SZ")


def rozhodnuti(id_, oblasti, hodin=5, senat=None, **k):
    return dict({"id": id_, "spz": id_.split(":", 1)[1], "senat": senat, "oblasti": oblasti,
                 "first_seen": iso(hodin), "zverejneno": ted.date().isoformat(),
                 "url": "https://example.org/" + id_, "heslo": "Heslo", "shrnuti": "Shrnutí " + id_}, **k)


# =====================================================================
print("1) Co jde do přehledu: jen IP/IT")
# =====================================================================
koren = tempfile.mkdtemp()
docs = os.path.join(koren, "docs")
os.makedirs(os.path.join(docs, "data", "judikatura"))
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "data", "oblasti.json"),
          encoding="utf-8") as f:
    oblasti = f.read()
with open(os.path.join(docs, "data", "oblasti.json"), "w", encoding="utf-8") as f:
    f.write(oblasti)
okna = {
    "ns": [rozhodnuti("ns:23 Cdo 1/2026", ["zavazky"], senat=23),              # senát 23, ale ne IP
           rozhodnuti("ns:23 Cdo 2/2026", ["prumyslova_prava", "zavazky"], senat=23),
           rozhodnuti("ns:30 Cdo 3/2026", ["gdpr"], senat=30),
           rozhodnuti("ns:23 Cdo 4/2026", ["autorske"], senat=23, hodin=24 * 20)],  # mimo dva týdny
    "nss": [rozhodnuti("nss:9 As 1/2026", ["prumyslova_prava"]), rozhodnuti("nss:1 Afs 2/2026", ["dane"])],
    "us": [rozhodnuti("us:I. ÚS 1/26", ["ustavni", "osobnost_media"], nazev="Kauza")],
    "sdeu": [rozhodnuti("sdeu:62024TJ0001", ["prumyslova_prava"]), rozhodnuti("sdeu:62024CJ0002", ["soutez"])],
}
for soud, polozky in okna.items():
    with open(os.path.join(docs, "data", "judikatura", f"{soud}.json"), "w", encoding="utf-8") as f:
        json.dump({"soud": soud, "generated": iso(1), "polozky": polozky}, f)
with open(os.path.join(docs, "data", "casopisy.json"), "w", encoding="utf-8") as f:
    json.dump({"casopisy": [{"id": "jiplp", "zkratka": "JIPLP"}],
               "polozky": [{"id": "a1", "casopis": "jiplp", "nazev": "Článek", "url": "https://example.org/a1",
                            "first_seen": iso(3), "shrnuti": "O známkách."}]}, f)

puvodni = digest.DOCS_DIR, digest.CASOPISY_JSON
digest.DOCS_DIR = docs
digest.CASOPISY_JSON = os.path.join(docs, "data", "casopisy.json")
try:
    polozky = digest.collect_items()
finally:
    digest.DOCS_DIR, digest.CASOPISY_JSON = puvodni
guid = [p["guid"] for p in polozky]
check("oblasti IP/IT jsou výchozí oblasti ze seznamu",
      digest.oblasti_ip_it() == {"autorske", "prumyslova_prava", "nekala_soutez", "it", "gdpr"},
      str(digest.oblasti_ip_it()))
check("z judikatury jen rozhodnutí z oblastí IP/IT, u všech soudů",
      sorted(g for g in guid if ":" in g) == ["ns:23 Cdo 2/2026", "ns:30 Cdo 3/2026", "nss:9 As 1/2026",
                                               "sdeu:62024TJ0001"], str(guid))
check("senát 23 se nebere celý (obchodní věc bez IP chybí)", "ns:23 Cdo 1/2026" not in guid)
check("starší než dva týdny chybí", "ns:23 Cdo 4/2026" not in guid)
check("časopisy jdou do přehledu", "a1" in guid and next(p for p in polozky if p["guid"] == "a1")["tag"] == "JIPLP")

# =====================================================================
print("\n2) Prompt a čtení odpovědi")
# =====================================================================
check("prompt je jen o IP a IT", "a jen v nich" in fc.DIGEST_PROMPT and "pár dalších" not in fc.DIGEST_PROMPT)
vstup = digest.build_prompt_input(polozky)
check("seznam pro model je očíslovaný a nese heslo", vstup.startswith("1. [") and "heslo: Heslo" in vstup, vstup[:80])
raw = ("**PŘEHLED:** Dva týdny ve znamení známek.\n\nTÉMA: Známky\nTEXT: NSS i Tribunál řešily známky.\n"
       "ZDROJE: 1, 2, 99\n\nTÉMA: Bez textu\n")
intro, bloky = digest.parse_digest(raw, polozky)
check("odpověď: úvod, jeden blok, čísla mimo rozsah zahodí",
      intro == "Dva týdny ve znamení známek." and len(bloky) == 1 and len(bloky[0]["sources"]) == 2,
      f"{intro!r} {bloky}")
check("otisk vstupu nese verzi tvaru", digest.FORMAT_VERSION == "4"
      and digest.input_hash(polozky) != digest.input_hash(polozky[1:]))

# =====================================================================
print("\n3) Rozhodnutí, která ještě čekají na AI rozbor")
# =====================================================================
# Bez rozboru nemají oblasti. Věc proti EUIPO je IP i bez nich; ostatní
# v přehledu chybí, ale počet je vidět.
okna["sdeu"] += [
    rozhodnuti("sdeu:T-890/25", [], nazev="Fitmart v. EUIPO (ULTRAPURE)", stav_shrnuti="pripravuje", shrnuti=""),
    rozhodnuti("sdeu:T-1/26", [], nazev="Alfa v. Rada", stav_shrnuti="pripravuje", shrnuti=""),
    rozhodnuti("sdeu:T-2/26", [], nazev="Beta v. EUIPO", stav_shrnuti="pripravuje", hodin=24 * 20),
]
with open(os.path.join(docs, "data", "judikatura", "sdeu.json"), "w", encoding="utf-8") as f:
    json.dump({"soud": "sdeu", "generated": iso(1), "polozky": okna["sdeu"]}, f)
digest.DOCS_DIR = docs
digest.CASOPISY_JSON = os.path.join(docs, "data", "casopisy.json")
stats = {}
try:
    polozky = digest.collect_items(stats)
finally:
    digest.DOCS_DIR, digest.CASOPISY_JSON = puvodni
guid = [p["guid"] for p in polozky]
check("čekající věc proti EUIPO do přehledu jde", "sdeu:T-890/25" in guid, str(guid))
check("jiná čekající věc chybí, ale spočítá se (jen v okně)",
      "sdeu:T-1/26" not in guid and stats == {"cekajici": 1}, str(stats))

# =====================================================================
print("\n4) Když AI selže")
# =====================================================================
vystup = os.path.join(koren, "digest.json")
predchozi = {"generated": (ted - timedelta(days=7)).isoformat(), "blocks": [{"title": "Starý"}],
             "input_hash": "x", "intro": "Starý přehled."}
with open(vystup, "w", encoding="utf-8") as f:
    json.dump(predchozi, f)
volani, spanky = [], []
puvodni_main = (digest.OUTPUT, digest.gemini_generate_raw, digest.gemini_enabled, digest.time.sleep,
                digest.DOCS_DIR, digest.CASOPISY_JSON)
digest.OUTPUT = vystup
digest.DOCS_DIR, digest.CASOPISY_JSON = docs, os.path.join(docs, "data", "casopisy.json")
digest.gemini_enabled = lambda: True
digest.gemini_generate_raw = lambda *a, **k: volani.append(1) or ""
digest.time.sleep = spanky.append
os.environ["DIGEST_FORCE"] = "1"
try:
    digest.main()
    kod = 0
except SystemExit as e:
    kod = e.code
with open(vystup, encoding="utf-8") as f:
    po = json.load(f)
check("AI selže i napodruhé → kód 1, starý přehled zůstane a nese `selhalo`",
      kod == 1 and len(volani) == 2 and spanky == [digest.DIGEST_OPAKOVANI_S]
      and po["blocks"] == predchozi["blocks"] and po.get("selhalo"), f"{kod} {volani} {po}")

volani.clear()
odpovedi = ["", "PŘEHLED: Úvod.\nTÉMA: Známky\nTEXT: Text.\nZDROJE: 1"]
digest.gemini_generate_raw = lambda *a, **k: volani.append(1) or odpovedi.pop(0)
digest.main()
with open(vystup, encoding="utf-8") as f:
    po = json.load(f)
check("napodruhé vyjde → uloží se bez `selhalo`, s počtem čekajících",
      len(volani) == 2 and po["blocks"][0]["title"] == "Známky" and "selhalo" not in po
      and po["cekajici"] == 1, str(po))

digest.gemini_enabled = lambda: False
os.environ.pop("DIGEST_FORCE")
os.environ["DIGEST_AUTO"] = "1"
with open(vystup, "w", encoding="utf-8") as f:
    json.dump(dict(predchozi, selhalo="x"), f)
try:
    digest.main()
    kod = 0
except SystemExit as e:
    kod = e.code
check("s vypnutou AI se nekončí chybou", kod == 0, str(kod))
os.environ.pop("DIGEST_AUTO")
(digest.OUTPUT, digest.gemini_generate_raw, digest.gemini_enabled, digest.time.sleep,
 digest.DOCS_DIR, digest.CASOPISY_JSON) = puvodni_main

# DIGEST_AUTO: kdy se přehled píše znovu.
pondeli = datetime(2026, 9, 28, 0, 30, tzinfo=timezone.utc)   # 2:30 v Praze
check("týden začíná v pondělí 0:00 pražského času",
      digest.zacatek_tydne(pondeli) == datetime(2026, 9, 27, 22, 0, tzinfo=timezone.utc)
      and digest.zacatek_tydne(pondeli - timedelta(hours=3)) == datetime(2026, 9, 20, 22, 0, tzinfo=timezone.utc))
tento = {"generated": (pondeli + timedelta(hours=1)).isoformat(), "blocks": [1], "input_hash": "h"}
check("tento týden hotový a úplný → nic",
      digest.proc_generovat(tento, "jiny", pondeli + timedelta(days=2)) is None)
check("pondělní běh o pár minut dřív než před týdnem → přehled je z minulého týdne",
      digest.proc_generovat(dict(tento, generated=(pondeli + timedelta(minutes=5) - timedelta(days=7)).isoformat()),
                            "h", pondeli) is not None)
check("minulý pokus selhal → znovu",
      digest.proc_generovat(dict(tento, selhalo="x"), "h", pondeli + timedelta(days=1)) is not None)
check("chyběla čekající rozhodnutí a vstup se změnil → znovu; beze změny ne",
      digest.proc_generovat(dict(tento, cekajici=3), "jiny", pondeli + timedelta(days=1)) is not None
      and digest.proc_generovat(dict(tento, cekajici=3), "h", pondeli + timedelta(days=1)) is None)

ok = sum(1 for _, c, _ in results if c)
print(f"\n{ok}/{len(results)} testů prošlo")
sys.exit(0 if ok == len(results) else 1)
