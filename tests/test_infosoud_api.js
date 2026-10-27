// Testy průběhu řízení z InfoSoudu (api/infosoud.js): node --test tests/
//
// Funkce běží za skutečným http serverem Node (stejné req/res jako na
// Vercelu); InfoSoud nahrazuje podvržený fetch, takže testy nejdou na síť.

"use strict";

const { test, before, after, beforeEach } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");

const handler = require("../api/infosoud.js");
const { rozeberSpz, isoDatum, zjednodus, INFOSOUD_API } = handler;

const skutecnyFetch = globalThis.fetch;
let infosoud;     // (telo) => Response – co „odpoví" InfoSoud
let dotazy = [];  // těla dotazů na InfoSoud

globalThis.fetch = async (url, init) => {
  if (String(url).startsWith("http://127.0.0.1")) return skutecnyFetch(url, init);
  assert.equal(String(url), INFOSOUD_API);
  const telo = JSON.parse(init.body);
  dotazy.push(telo);
  return infosoud(telo);
};

const json = (data, status = 200) => new Response(JSON.stringify(data), { status });

let server, base;
before(async () => {
  server = http.createServer(handler);
  await new Promise(r => server.listen(0, "127.0.0.1", r));
  base = "http://127.0.0.1:" + server.address().port;
});
after(() => server.close());
beforeEach(() => { dotazy = []; });

test("rozebere spisovou značku", () => {
  assert.deepEqual(rozeberSpz("9 C 10/2024"), { cisloSenatu: 9, druhVeci: "C", bcVec: 10, rocnik: 2024 });
  assert.deepEqual(rozeberSpz(" 3 Cmo 73 / 2023 "), { cisloSenatu: 3, druhVeci: "Cmo", bcVec: 73, rocnik: 2023 });
  assert.equal(rozeberSpz("9 C 10"), null);
  assert.equal(rozeberSpz("<script>"), null);
});

test("převede data InfoSoudu na ISO", () => {
  assert.equal(isoDatum("19.05.2026"), "2026-05-19");
  assert.equal(isoDatum("1.5.2026 08:30"), "2026-05-01");
  assert.equal(isoDatum("2026-05-19T00:00:00"), "2026-05-19");
  assert.equal(isoDatum(""), "");
});

test("zjednoduší události a seřadí je od nejstarší", () => {
  const v = zjednodus({
    stav: "Nevyřízená", stavDatum: "11.03.2024",
    udalosti: [
      { udalost: "NAR_JED", datum: "22.10.2026", zruseno: false },
      { udalost: "ZAHAJ_RIZ", datum: "11.03.2024" },
      { udalost: "NOVY_KOD", datum: "01.04.2024", zruseno: true },
    ],
  });
  assert.equal(v.nalezeno, true);
  assert.equal(v.stav_datum, "2024-03-11");
  assert.deepEqual(v.udalosti.map(u => [u.datum, u.nazev, u.zruseno]), [
    ["2024-03-11", "Zahájení řízení", false],
    ["2024-04-01", "NOVY_KOD", true],
    ["2026-10-22", "Nařízení jednání", false],
  ]);
});

test("vrátí průběh řízení a pošle InfoSoudu kód soudu", async () => {
  infosoud = () => json({ stav: "Nevyřízená", udalosti: [{ udalost: "ZAHAJ_RIZ", datum: "11.03.2024" }] });
  const r = await fetch(base + "/api/infosoud?soud=MS&spz=" + encodeURIComponent("9 C 10/2024"));
  assert.equal(r.status, 200);
  assert.match(r.headers.get("cache-control"), /s-maxage=/);
  const data = await r.json();
  assert.equal(data.udalosti[0].nazev, "Zahájení řízení");
  assert.deepEqual(dotazy[0], { druhOrganizace: "MSPHAAB", cisloSenatu: "9", druhVeci: "C", bcVec: "10", rocnik: "2024" });
});

test("zkusí rejstřík velkými i malými písmeny", async () => {
  infosoud = t => t.druhVeci === "cmo"
    ? json({ udalosti: [{ udalost: "ODVOLANI", datum: "02.05.2023" }] })
    : json({}, 400);
  const r = await fetch(base + "/api/infosoud?soud=VS&spz=" + encodeURIComponent("3 Cmo 73/2023"));
  const data = await r.json();
  assert.equal(data.nalezeno, true);
  assert.deepEqual(dotazy.map(t => t.druhVeci), ["Cmo", "CMO", "cmo"]);
  assert.equal(dotazy[0].druhOrganizace, "VSPHAAB");
});

test("neznámé řízení: nalezeno false", async () => {
  infosoud = () => json({}, 400);
  const data = await (await fetch(base + "/api/infosoud?soud=MS&spz=" + encodeURIComponent("1 C 1/2026"))).json();
  assert.equal(data.nalezeno, false);
  assert.deepEqual(data.udalosti, []);
});

test("odmítne jiný soud nebo nesmyslnou značku", async () => {
  assert.equal((await fetch(base + "/api/infosoud?soud=NS&spz=" + encodeURIComponent("9 C 10/2024"))).status, 400);
  assert.equal((await fetch(base + "/api/infosoud?soud=MS&spz=xx")).status, 400);
  assert.equal(dotazy.length, 0);
});

test("výpadek InfoSoudu: 502 bez cache", async () => {
  infosoud = () => json({}, 503);
  const r = await fetch(base + "/api/infosoud?soud=MS&spz=" + encodeURIComponent("9 C 10/2024"));
  assert.equal(r.status, 502);
  assert.equal(r.headers.get("cache-control"), "no-store");
});
