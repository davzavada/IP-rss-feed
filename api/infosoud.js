// Průběh řízení z InfoSoudu pro sledovaná řízení v Kalendáři jednání
// (Vercel Function).
//
// Web se ptá adresou /api/infosoud?soud=MS&spz=9 C 10/2024. Funkce pošle
// dotaz na veřejné API InfoSoudu (to samé, ze kterého čte jeho web) a vrátí
// zjednodušený seznam událostí: zahájení, nařízená jednání, rozhodnutí,
// odvolání… Prohlížeč se na InfoSoud zeptat nemůže sám – API neposílá
// hlavičky CORS.
//
// Bere jen soudy z Kalendáře (MS a VS Praha) a spisovou značku ve tvaru
// „senát rejstřík číslo/ročník“, adresu na InfoSoud si skládá sama.
// Odpověď si CDN Vercelu nechá na pár hodin, ať InfoSoud nezatěžují
// opakovaná otevření kalendáře.

"use strict";

const INFOSOUD_API = "https://infosoud.gov.cz/api/v1/rizeni/vyhledej";
const TIMEOUT_MS = 8000;
// Stejný prohlížeč, jako se hlásí scrapery (feed_common.USER_AGENT).
const USER_AGENT =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

// Kódy organizací v InfoSoudu (hearings_config.json, infosoud_org).
const SOUDY = { MS: "MSPHAAB", VS: "VSPHAAB" };

// Názvy událostí podle číselníku InfoSoudu.
const UDALOSTI = {
  ZAHAJ_RIZ: "Zahájení řízení",
  NAR_JED: "Nařízení jednání",
  ZRUS_JED: "Zrušení jednání",
  VYD_ROZH: "Vydání rozhodnutí",
  ODES_SPIS: "Odeslání spisu",
  VRAC_SPIS: "Vrácení spisu",
  POD_OP_PR: "Podání opravného prostředku",
  VYR_OP_PR: "Vyřízení opravného prostředku",
  ODVOLANI: "Řízení o odvolání",
  DOVOL_RIZ: "Řízení o dovolání",
  NAD_RIZENI: "Řízení u nadřízeného soudu",
  ST_VEC_VYR: "Vyřízení věci",
  ST_VEC_ODS: "Skončení věci",
  ST_VEC_OBZ: "Obživnutí věci",
  ST_VEC_PRE: "Přerušení řízení",
  ST_VEC_UPR: "Ukončení přerušení",
  SPIS_K_SC: "Spis odeslán soudci",
  SPIS_OD_SC: "Soudce předal spis",
  PREVD_SPIS: "Převedeno pod jinou sp. zn.",
  PR_VEC_NS: "Přerušení věci",
  VR_SP_NS: "Vrácení spisu odvolacímu soudu",
};

const SPZ_RE = /^\s*(\d{1,4})\s+([A-Za-z]{1,6})\s+(\d{1,6})\s*\/\s*(\d{4})\s*$/;

function parametry(req) {
  const url = new URL(req.url || "/", "http://owl");
  const p = Object.fromEntries(url.searchParams);
  for (const [k, v] of Object.entries(req.query || {})) {
    if (p[k] === undefined) p[k] = String(Array.isArray(v) ? v[0] : v);
  }
  return p;
}

// „9 C 10/2024“ -> části značky; null, když to značka není.
function rozeberSpz(spz) {
  const m = SPZ_RE.exec(String(spz || ""));
  if (!m) return null;
  return { cisloSenatu: Number(m[1]), druhVeci: m[2], bcVec: Number(m[3]), rocnik: Number(m[4]) };
}

// Datum z InfoSoudu („19.05.2026“, „19.05.2026 08:30“, „2026-05-19“
// i s časem) -> „2026-05-19“; jinak "".
function isoDatum(s) {
  const t = String(s || "").trim();
  let m = /^(\d{4})-(\d{2})-(\d{2})/.exec(t);
  if (m) return m[1] + "-" + m[2] + "-" + m[3];
  m = /^(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})/.exec(t);
  if (m) return m[3] + "-" + m[2].padStart(2, "0") + "-" + m[1].padStart(2, "0");
  return "";
}

// Odpověď InfoSoudu -> to, co potřebuje web: stav a události od nejstarší.
function zjednodus(data) {
  const udalosti = (Array.isArray(data && data.udalosti) ? data.udalosti : [])
    .map(u => {
      const kod = String(u.udalost || u.druhUdalosti || "");
      return {
        datum: isoDatum(u.datum),
        kod,
        nazev: UDALOSTI[kod] || u.nazev || u.popis || kod,
        zruseno: Boolean(u.zruseno),
      };
    })
    .filter(u => u.datum && u.kod);
  udalosti.sort((a, b) => a.datum.localeCompare(b.datum));
  return {
    nalezeno: udalosti.length > 0 || Boolean(data && data.stav),
    stav: (data && data.stav) || "",
    stav_datum: isoDatum(data && data.stavDatum),
    udalosti,
  };
}

async function dotaz(telo) {
  const r = await fetch(INFOSOUD_API, {
    method: "POST",
    headers: { "User-Agent": USER_AGENT, Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(telo),
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  // Neznámou značku InfoSoud odmítá jako chybný dotaz.
  if (r.status === 400 || r.status === 404) return null;
  if (r.status !== 200) throw new Error("HTTP " + r.status);
  return r.json();
}

// Rejstřík píše InfoSoud na webu malými písmeny, API podle klientů velkými;
// zkusí se tvar ze značky a pak oba další, dokud řízení nenajde.
async function najdiRizeni(org, z) {
  const tvary = [z.druhVeci, z.druhVeci.toUpperCase(), z.druhVeci.toLowerCase()]
    .filter((x, i, a) => a.indexOf(x) === i);
  for (const druhVeci of tvary) {
    const data = await dotaz({
      druhOrganizace: org,
      cisloSenatu: String(z.cisloSenatu),
      druhVeci,
      bcVec: String(z.bcVec),
      rocnik: String(z.rocnik),
    });
    const vysledek = data && zjednodus(data);
    if (vysledek && vysledek.nalezeno) return vysledek;
  }
  return { nalezeno: false, stav: "", stav_datum: "", udalosti: [] };
}

function odpovez(res, status, hlavicky, telo) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  for (const [k, v] of Object.entries(hlavicky)) res.setHeader(k, v);
  res.end(JSON.stringify(telo));
}

async function handler(req, res) {
  if (req.method !== "GET") {
    return odpovez(res, 405, { Allow: "GET", "Cache-Control": "no-store" }, { chyba: "Jen GET." });
  }
  const p = parametry(req);
  const org = SOUDY[p.soud];
  const z = rozeberSpz(p.spz);
  if (!org || !z) {
    return odpovez(res, 400, { "Cache-Control": "no-store" }, { chyba: "Neznámý soud nebo spisová značka." });
  }
  let vysledek;
  try {
    vysledek = await najdiRizeni(org, z);
  } catch (e) {
    console.warn(`infosoud: ${p.soud} ${p.spz}: ${e && e.message}`);
    return odpovez(res, 502, { "Cache-Control": "no-store" }, { chyba: "InfoSoud neodpověděl." });
  }
  // Průběh řízení se mění nanejvýš párkrát za den: prohlížeč hodinu,
  // CDN šest hodin.
  odpovez(res, 200, { "Cache-Control": "public, max-age=3600, s-maxage=21600" }, vysledek);
}

module.exports = handler;
// Kvůli testům (tests/test_infosoud_api.js).
module.exports.rozeberSpz = rozeberSpz;
module.exports.isoDatum = isoDatum;
module.exports.zjednodus = zjednodus;
module.exports.INFOSOUD_API = INFOSOUD_API;
