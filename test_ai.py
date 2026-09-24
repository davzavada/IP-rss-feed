#!/usr/bin/env python3
"""Testy AI klienta (feed_common): výběr nejlepšího bezplatného modelu.

Klient nemá model napevno: bere pořadí Flash-Lite → nejnovější Gemma
(nebo vnucené GEMINI_MODELS) a podle odpovědí API rozhoduje, kdy čekat,
kdy přejít na další model a kdy model do konce běhu vyřadit. Testy běží bez sítě – Gemini API i hodiny jsou
nasimulované, takže se dá ověřit i to, kolik se čekalo.

Spuštění: python test_ai.py
"""

import json
import os
import sys

import feed_common as fc

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("  OK   " if cond else "  CHYBA") + f" {name}" + (f" – {detail}" if detail and not cond else ""))


# --- Simulace API a hodin ---

class Odp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


def ok(text, verze=None, finish="STOP", myslenka=None):
    parts = ([{"text": myslenka, "thought": True}] if myslenka else []) + [{"text": text}]
    data = {"candidates": [{"content": {"parts": parts}, "finishReason": finish}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20,
                              "thoughtsTokenCount": 5}}
    if verze:
        data["modelVersion"] = verze
    return Odp(200, data)


def chyba(status, zprava, detaily=()):
    return Odp(status, {"error": {"code": status, "message": zprava, "details": list(detaily)}})


def kvota(quota_id, hodnota, cekani=None):
    detaily = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaId": quota_id, "quotaValue": hodnota}]}]
    if cekani is not None:
        detaily.append({"@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": f"{cekani}s"})
    return chyba(429, "You exceeded your current quota.", detaily)


DENNI = lambda: kvota("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "250")
NULOVA = lambda: kvota("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "0")
MINUTOVA = lambda s=7: kvota("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "10", s)
PRETIZENO = lambda: chyba(503, "The model is overloaded.")


class Sit:
    """Scénář odpovědí po modelech; zapisuje, co klient poslal."""

    def __init__(self, scenar=None, modely=None):
        self.scenar = {m: list(v) for m, v in (scenar or {}).items()}
        self.modely = modely
        self.volani = []
        self.seznamu = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.seznamu += 1
        if self.modely is None:
            return Odp(500, {})
        return Odp(200, {"models": [
            {"name": "models/" + m, "supportedGenerationMethods": ["generateContent"]}
            for m in self.modely]})

    def post(self, url, headers=None, json=None, timeout=None):
        model = url.split("/models/", 1)[1].split(":", 1)[0]
        self.volani.append((model, json))
        fronta = self.scenar.get(model) or []
        if not fronta:
            raise AssertionError(f"nečekané volání {model}")
        odp = fronta.pop(0)
        if isinstance(odp, Exception):
            raise odp
        return odp

    def modely_volani(self):
        return [m for m, _ in self.volani]


class Hodiny:
    def __init__(self):
        self.t = 1000.0
        self.spanky = []

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.spanky.append(round(s, 1))
        self.t += s


VSECHNY = ["gemini-pro-latest", "gemini-flash-latest", "gemini-flash-lite-latest",
           "gemma-3-27b-it", "gemma-4-31b-it", "gemma-4-26b-a4b-it", "gemini-2.5-flash"]
PRVNI, DRUHY = "gemini-flash-lite-latest", "gemma-4-31b-it"
PORADI = [PRVNI, DRUHY]


def priprav(scenar=None, modely=VSECHNY, vnucene=None):
    """Čistý stav klienta, simulovaná síť a hodiny."""
    fc._poradi = None
    fc._modely.clear()
    fc._spotreba.clear()
    fc._klic_zamitnut = False
    fc.GEMINI_API_KEY = "test-klic"
    os.environ.pop("SKIP_GEMINI", None)
    if vnucene is None:
        os.environ.pop("GEMINI_MODELS", None)
    else:
        os.environ["GEMINI_MODELS"] = vnucene
    sit = Sit(scenar, modely)
    hodiny = Hodiny()
    fc.requests = sit
    fc.time = hodiny
    return sit, hodiny


def dotaz(system=None, schema=None):
    return fc.ai_volani([{"text": "Shrň rozhodnutí."}], system=system, schema=schema)


# =====================================================================
print("\n1) Pořadí modelů")
# =====================================================================
sit, _ = priprav()
check("Flash-Lite a pak nejnovější hustá Gemma (Pro a Flash ne)", fc.gemini_modely() == PORADI,
      str(fc.gemini_modely()))
fc.gemini_modely()
check("seznam modelů se stahuje jednou za proces", sit.seznamu == 1, str(sit.seznamu))

priprav(modely=["gemini-pro-latest", "gemma-3-27b-it", "gemma-4-31b-it"])
check("chybějící alias se přeskočí", fc.gemini_modely() == ["gemma-4-31b-it"],
      str(fc.gemini_modely()))

priprav(modely=None)
check("bez seznamu modelů výchozí pořadí a záložní Gemma",
      fc.gemini_modely() == list(fc.GEMINI_VYCHOZI_MODELY) + [fc.GEMMA_ZALOZNI],
      str(fc.gemini_modely()))

sit, _ = priprav(vnucene="gemma-4-31b-it, gemini-flash-lite-latest")
check("GEMINI_MODELS pořadí vnutí a seznam se nestahuje",
      fc.gemini_modely() == ["gemma-4-31b-it", "gemini-flash-lite-latest"] and sit.seznamu == 0,
      str(fc.gemini_modely()))

# =====================================================================
print("\n2) Odpověď a limity")
# =====================================================================
sit, _ = priprav({PRVNI: [ok("HESLO: X\nSHRNUTÍ: Y", "gemini-3.5-flash-lite",
                                           myslenka="interní úvaha")]})
text, model = dotaz()
check("odpoví první model a vrátí skutečnou verzi",
      text == "HESLO: X\nSHRNUTÍ: Y" and model == "gemini-3.5-flash-lite", f"{text!r} {model}")
check("přemýšlení modelu se do odpovědi nedostane", "úvaha" not in text, text)
check("spotřeba se počítá", fc.ai_spotreba()[PRVNI] ==
      {"volani": 1, "vstup": 100, "vystup": 25}, str(fc.ai_spotreba()))

sit, _ = priprav({PRVNI: [DENNI()],
                  DRUHY: [ok("první"), ok("druhá")]})
prvni, druhy = dotaz(), dotaz()
check("vyčerpaný denní limit → další model", prvni[0] == "první", str(prvni))
check("model s vyčerpaným limitem se do konce běhu nezkouší",
      druhy[0] == "druhá" and sit.modely_volani().count(PRVNI) == 1,
      str(sit.modely_volani()))

sit, _ = priprav({PRVNI: [NULOVA()], DRUHY: [ok("flash")]})
check("model mimo free tier (nulová kvóta) se vyřadí",
      dotaz()[0] == "flash" and fc._stav(PRVNI)["vyrazen"] == "na free tieru není",
      fc._stav(PRVNI)["vyrazen"])

sit, hodiny = priprav({PRVNI: [MINUTOVA(7), ok("po čekání")]})
check("limit za minutu → počká podle RetryInfo a zkusí týž model",
      dotaz()[0] == "po čekání" and 7.0 in hodiny.spanky
      and sit.modely_volani() == [PRVNI, PRVNI],
      f"{hodiny.spanky} {sit.modely_volani()}")

sit, hodiny = priprav({PRVNI: [MINUTOVA(9999), ok("ok")]})
dotaz()
check("čekání na minutový limit má strop", max(hodiny.spanky) <= fc.GEMINI_MAX_CEKANI,
      str(hodiny.spanky))

# =====================================================================
print("\n3) Výpadky a chyby")
# =====================================================================
sit, _ = priprav({PRVNI: [PRETIZENO()] * fc.GEMINI_MAX_RETRIES + [ok("pro znovu")],
                  DRUHY: [ok("flash")]})
check("přetížený model: po všech pokusech odpoví další model", dotaz()[0] == "flash")
check("po jednom nezdaru se model zkouší dál", dotaz()[0] == "pro znovu",
      str(sit.modely_volani()))

sit, _ = priprav({PRVNI: [PRETIZENO()] * (3 * fc.GEMINI_MAX_RETRIES),
                  DRUHY: [ok("a"), ok("b"), ok("c"), ok("d")]})
for _ in range(4):
    dotaz()
check("tři položky po sobě bez odpovědi → model do konce běhu pryč",
      sit.modely_volani().count(PRVNI) == 3 * fc.GEMINI_MAX_RETRIES
      and fc._stav(PRVNI)["vyrazen"] == "opakovaně nedostupný",
      str(sit.modely_volani().count(PRVNI)))

sit, _ = priprav({PRVNI: [ConnectionError("reset")] * 2 + [ok("po výpadku")]})
check("síťová chyba se opakuje", dotaz()[0] == "po výpadku", str(sit.modely_volani()))

sit, _ = priprav({PRVNI: [chyba(400, "thinking_level is not supported for this model."),
                                        ok("bez přemýšlení")]})
text, _ = dotaz()
prvni_telo, druhe_telo = sit.volani[0][1], sit.volani[1][1]
check("volba, kterou model nebere, se vypustí a dotaz zopakuje",
      text == "bez přemýšlení" and "thinkingConfig" in prvni_telo["generationConfig"]
      and "thinkingConfig" not in druhe_telo["generationConfig"], str(druhe_telo))

sit, _ = priprav({PRVNI: [chyba(400, "The input token count (1200000) exceeds the maximum number of tokens allowed (1048576).")],
                  DRUHY: [ok("vzal to flash")]})
check("příliš dlouhý vstup → další model, bez vyřazení",
      dotaz()[0] == "vzal to flash" and not fc._stav(PRVNI)["vyrazen"])

sit, _ = priprav({PRVNI: [chyba(404, "models/gemini-flash-lite-latest is not found")],
                  DRUHY: [ok("flash")]})
check("neexistující model se vyřadí", dotaz()[0] == "flash"
      and fc._stav(PRVNI)["vyrazen"] == "neexistuje")

sit, _ = priprav({PRVNI: [chyba(403, "Requests to this API ... are blocked.")]})
prvni = dotaz()
druhy = dotaz()
check("odmítnutý klíč ukončí AI pro celý běh",
      prvni == ("", "") and druhy == ("", "") and len(sit.volani) == 1, str(sit.modely_volani()))

sit, _ = priprav({PRVNI: [ok("useknuté", finish="MAX_TOKENS")],
                  DRUHY: [ok("celé")]})
check("useknutá odpověď → další model", dotaz()[0] == "celé")

sit, _ = priprav({PRVNI: [Odp(200, {"promptFeedback": {"blockReason": "SAFETY"}})],
                  DRUHY: [ok("prošlo")]})
check("zablokovaný dotaz → další model", dotaz()[0] == "prošlo")

sit, _ = priprav({m: [PRETIZENO()] * fc.GEMINI_MAX_RETRIES for m in PORADI})
check("když neodpoví nic, vrátí prázdno", dotaz() == ("", ""))

sit, _ = priprav({PRVNI: [ok("nemá se volat")]})
os.environ["SKIP_GEMINI"] = "1"
check("SKIP_GEMINI → žádné volání", dotaz() == ("", "") and not sit.volani)
os.environ.pop("SKIP_GEMINI", None)

# =====================================================================
print("\n4) Tvar dotazu")
# =====================================================================
schema = {"type": "OBJECT", "properties": {"heslo": {"type": "STRING"}}}
sit, _ = priprav({PRVNI: [ok("{}")]})
dotaz(system="Jsi asistent.", schema=schema)
telo = sit.volani[0][1]
check("Gemini: systémová instrukce, JSON schéma, nízké přemýšlení, bezpečnost",
      telo["systemInstruction"]["parts"][0]["text"] == "Jsi asistent."
      and telo["generationConfig"]["responseMimeType"] == "application/json"
      and telo["generationConfig"]["responseSchema"] == schema
      and telo["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
      and telo["safetySettings"] == fc.GEMINI_BEZPECNOST
      and telo["contents"][0]["role"] == "user", json.dumps(telo)[:300])

sit, _ = priprav({DRUHY: [ok("gemma")]}, vnucene=DRUHY)
dotaz(system="Jsi asistent.", schema=schema)
telo = sit.volani[0][1]
check("Gemma: systémová instrukce předřazená textu, bez schématu a přemýšlení",
      "systemInstruction" not in telo
      and telo["contents"][0]["parts"][0]["text"] == "Jsi asistent."
      and "responseSchema" not in telo["generationConfig"]
      and "thinkingConfig" not in telo["generationConfig"], json.dumps(telo)[:300])

dlouhy = "Odůvodnění. " * 15000   # přes 180 000 znaků
sit, _ = priprav({PRVNI: [ok("HESLO: Test\nSHRNUTÍ: Celé.")]})
shrnuti, heslo = fc.gemini_summarize_text(dlouhy, "Shrň:")
poslano = sit.volani[0][1]["contents"][0]["parts"][0]["text"]
check("text rozhodnutí jde modelu celý, bez ořezu",
      dlouhy.strip() in poslano and heslo == "Test", f"{len(poslano)} znaků")

sit, _ = priprav({PRVNI: [ok("**HESLO:** Ochranná známka\n**SHRNUTÍ:** Soud rozhodl.")]})
check("tučné značky se v odpovědi zahodí",
      fc.gemini_summarize_text("text", "p") == ("Soud rozhodl.", "Ochranná známka"),
      str(fc.parse_ai_response("**HESLO:** Ochranná známka\n**SHRNUTÍ:** Soud rozhodl.")))

# =====================================================================
print("\n5) Rozestup mezi voláními")
# =====================================================================
sit, hodiny = priprav({PRVNI: [ok("a"), ok("b")]}, vnucene=PRVNI)
dotaz()
dotaz()
check("druhé volání téhož modelu počká na rozestup",
      hodiny.spanky == [fc.GEMINI_ROZESTUP["flash-lite"]], str(hodiny.spanky))

# =====================================================================
failed = [n for n, ok_, _ in results if not ok_]
print(f"\n{len(results) - len(failed)}/{len(results)} testů prošlo")
if failed:
    print("Neprošlo:")
    for n in failed:
        print("  -", n)
sys.exit(1 if failed else 0)
