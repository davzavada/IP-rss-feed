// Testy náhledu PDF (api/pdf.js): node --test tests/
//
// Funkce běží za skutečným http serverem Node (stejné req/res jako na
// Vercelu); NSS nahrazuje podvržený fetch, takže testy nejdou na síť.

"use strict";

const { test, before, after, beforeEach } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");

const handler = require("../api/pdf.js");
const { parametry, jmenoSouboru, dispozice, NSS_PDF, MAX_BAJTU } = handler;

const PDF = Buffer.from("%PDF-1.7\n1 0 obj << >> endobj\n%%EOF\n");
const skutecnyFetch = globalThis.fetch;
let nss;          // (url, init) => Response – co „odpoví" NSS
let dotazy = [];  // adresy, na které se funkce ptala

globalThis.fetch = async (url, init) => {
  if (String(url).startsWith("http://127.0.0.1")) return skutecnyFetch(url, init);
  dotazy.push(String(url));
  return nss(String(url), init);
};

function odpoved(telo, { status = 200, hlavicky = {}, url } = {}) {
  const r = new Response(telo, { status, headers: hlavicky });
  if (url) Object.defineProperty(r, "url", { value: url });
  return r;
}

let server, zaklad;
before(async () => {
  server = http.createServer((req, res) => handler(req, res));
  await new Promise((ok) => server.listen(0, "127.0.0.1", ok));
  zaklad = `http://127.0.0.1:${server.address().port}`;
});
after(() => server.close());
beforeEach(() => {
  dotazy = [];
  nss = () => odpoved(PDF, { hlavicky: { "Content-Type": "application/pdf" } });
});

const dotaz = (cesta, init = {}) => skutecnyFetch(zaklad + cesta, { redirect: "manual", ...init });

test("PDF z NSS přijde inline se jménem podle spisové značky", async () => {
  const r = await dotaz("/api/pdf?soud=nss&id=785776&soubor=" +
                        encodeURIComponent("2 Nao 141-2026.pdf"));
  assert.equal(r.status, 200);
  assert.equal(r.headers.get("content-type"), "application/pdf");
  assert.equal(r.headers.get("content-disposition"),
               `inline; filename="2 Nao 141-2026.pdf"; filename*=UTF-8''2%20Nao%20141-2026.pdf`);
  assert.match(r.headers.get("cache-control"), /s-maxage=/);
  assert.deepEqual(Buffer.from(await r.arrayBuffer()), PDF);
  assert.deepEqual(dotazy, [NSS_PDF + "785776"]);
});

test("adresu na NSS skládá funkce sama – jen z číselného id", async () => {
  for (const cesta of [
    "/api/pdf?soud=nss&id=abc",
    "/api/pdf?soud=nss&id=1/../../x",
    "/api/pdf?soud=nss&id=" + encodeURIComponent("https://example.com/a.pdf"),
    "/api/pdf?soud=ns&id=123",
    "/api/pdf?id=123",
    "/api/pdf",
  ]) {
    const r = await dotaz(cesta);
    assert.equal(r.status, 400, cesta);
  }
  assert.deepEqual(dotazy, []);
});

test("jen GET a HEAD; HEAD bez těla", async () => {
  const post = await dotaz("/api/pdf?soud=nss&id=1", { method: "POST" });
  assert.equal(post.status, 405);
  assert.equal(post.headers.get("allow"), "GET, HEAD");
  const head = await dotaz("/api/pdf?soud=nss&id=1", { method: "HEAD" });
  assert.equal(head.status, 200);
  assert.equal(head.headers.get("content-type"), "application/pdf");
  assert.equal((await head.arrayBuffer()).byteLength, 0);
});

test("když PDF získat nejde, přesměruje na NSS (a necachuje to)", async () => {
  const pripady = {
    "NSS neodpoví": () => { throw new TypeError("fetch failed"); },
    "HTTP 500": () => odpoved("chyba", { status: 500 }),
    "HTML místo PDF": () => odpoved("<html>Chyba</html>", { hlavicky: { "Content-Type": "text/html" } }),
    "hlášená velikost nad limit": () =>
      odpoved(PDF, { hlavicky: { "Content-Length": String(MAX_BAJTU + 1) } }),
    "skutečná velikost nad limit": () =>
      odpoved(Buffer.concat([PDF, Buffer.alloc(MAX_BAJTU)])),
    "přesměrování mimo NSS": () => odpoved(PDF, { url: "https://example.com/x.pdf" }),
  };
  for (const [popis, fn] of Object.entries(pripady)) {
    nss = fn;
    const r = await dotaz("/api/pdf?soud=nss&id=42");
    assert.equal(r.status, 302, popis);
    assert.equal(r.headers.get("location"), NSS_PDF + "42", popis);
    assert.equal(r.headers.get("cache-control"), "no-store", popis);
  }
});

test("PDF smí mít před hlavičkou smetí (do 1024 B)", async () => {
  nss = () => odpoved(Buffer.concat([Buffer.from("\r\n\r\n"), PDF]));
  const r = await dotaz("/api/pdf?soud=nss&id=7");
  assert.equal(r.status, 200);
});

test("parametry z dotazu (po přepisu) i z původní cesty", () => {
  assert.deepEqual(
    parametry({ url: "/api/pdf?soud=nss&id=5&soubor=A%20B.pdf" }),
    { soud: "nss", id: "5", soubor: "A B.pdf" });
  assert.deepEqual(
    parametry({ url: "/pdf/nss/5/10%20Afs%2012-2025.pdf" }),
    { soud: "nss", id: "5", soubor: "10 Afs 12-2025.pdf" });
  assert.deepEqual(
    parametry({ url: "/api/pdf", query: { soud: "nss", id: ["6", "7"] } }),
    { soud: "nss", id: "6" });
});

test("jméno souboru a Content-Disposition", () => {
  assert.equal(jmenoSouboru("10 Afs 123/2025 - 45"), "10 Afs 123-2025 - 45.pdf");
  assert.equal(jmenoSouboru("3 As 7/2026.pdf"), "3 As 7-2026.pdf");
  assert.equal(jmenoSouboru('a"b\u0000c:d?.pdf'), "abcd.pdf");
  assert.equal(jmenoSouboru(""), "rozhodnuti.pdf");
  assert.equal(jmenoSouboru(undefined), "rozhodnuti.pdf");
  assert.equal(dispozice("Čj. 1 (x).pdf"),
               `inline; filename="Cj. 1 (x).pdf"; filename*=UTF-8''%C4%8Cj.%201%20%28x%29.pdf`);
});
