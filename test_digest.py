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

ok = sum(1 for _, c, _ in results if c)
print(f"\n{ok}/{len(results)} testů prošlo")
sys.exit(0 if ok == len(results) else 1)
