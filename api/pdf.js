// Náhled PDF rozhodnutí NSS v prohlížeči (Vercel Function).
//
// Vyhledávač NSS posílá PDF s hlavičkou „Content-Disposition: attachment",
// takže se po kliknutí rovnou stáhne. Funkce PDF stáhne a pošle dál jako
// „inline" – prohlížeč ho pak otevře ve svém prohlížeči PDF. Web na ni
// odkazuje adresou /pdf/nss/{id}/{spisová značka}.pdf (přepis ve
// vercel.json); jméno souboru na konci je jen kvůli titulku karty a názvu
// při uložení. PDF Nejvyššího soudu přes funkci nejdou, ta soud posílá
// inline sám.
//
// Bere jen číselné id dokumentu a adresu na NSS si skládá sama, takže přes
// ni nejde stáhnout nic jiného. Když se PDF získat nepodaří (NSS neodpoví,
// vrátí něco jiného než PDF, soubor je větší, než smí funkce vrátit),
// přesměruje na původní adresu – PDF se pak stáhne jako dřív.

"use strict";

const NSS_PDF = "https://vyhledavac.nssoud.cz/DokumentOriginal/Index/";
// Odpověď Vercel Function má strop 4,5 MB.
const MAX_BAJTU = 4 * 1024 * 1024;
const TIMEOUT_MS = 8000;
// Stejný prohlížeč, jako se hlásí scrapery (feed_common.USER_AGENT).
const USER_AGENT =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

// Parametry z dotazu. Po přepisu z /pdf/{soud}/{id}/{soubor} je Vercel
// předá v dotazu (a v req.query); kdyby req.url nesl ještě původní cestu,
// vezmou se z ní.
function parametry(req) {
  const url = new URL(req.url || "/", "http://owl");
  const p = Object.fromEntries(url.searchParams);
  for (const [k, v] of Object.entries(req.query || {})) {
    if (p[k] === undefined) p[k] = String(Array.isArray(v) ? v[0] : v);
  }
  const m = /^\/pdf\/([a-z]+)\/(\d+)\/([^/]+)$/.exec(url.pathname);
  if (m) {
    p.soud = p.soud || m[1];
    p.id = p.id || m[2];
    if (!p.soubor) {
      try { p.soubor = decodeURIComponent(m[3]); } catch { p.soubor = ""; }
    }
  }
  return p;
}

// Spisová značka -> jméno souboru: lomítka na pomlčky, znaky, které
// v názvu souboru nemají co dělat, pryč, vždy s příponou .pdf.
function jmenoSouboru(s) {
  const jmeno = String(s || "").normalize("NFC")
    .replace(/\.pdf$/i, "")
    .replace(/[/\\]+/g, "-")
    .replace(/[\u0000-\u001f\u007f"*:<>?|]+/g, "")
    .replace(/\s+/g, " ").trim().slice(0, 120);
  return (jmeno || "rozhodnuti") + ".pdf";
}

// inline + jméno (ASCII pro staré prohlížeče, UTF-8 podle RFC 6266).
function dispozice(jmeno) {
  const ascii = jmeno.normalize("NFKD").replace(/[^\x20-\x7e]/g, "")
    .replace(/["\\;]/g, "") || "rozhodnuti.pdf";
  const utf8 = encodeURIComponent(jmeno)
    .replace(/['()*]/g, (c) => "%" + c.charCodeAt(0).toString(16).toUpperCase());
  return `inline; filename="${ascii}"; filename*=UTF-8''${utf8}`;
}

async function precti(telo, max) {
  const casti = [];
  let celkem = 0;
  for await (const kus of telo) {
    celkem += kus.length;
    if (celkem > max) throw new Error("PDF je větší než " + max + " B");
    casti.push(kus);
  }
  return Buffer.concat(casti);
}

// PDF začíná „%PDF-" (norma snese před hlavičkou až 1024 bajtů smetí).
function jePdf(data) {
  return data.subarray(0, 1024).indexOf("%PDF-") !== -1;
}

async function stahniPdf(url) {
  const r = await fetch(url, {
    headers: { "User-Agent": USER_AGENT, Accept: "application/pdf,*/*;q=0.8" },
    redirect: "follow",
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  if (new URL(r.url || url).origin !== new URL(url).origin) {
    throw new Error("přesměrování mimo NSS: " + r.url);
  }
  if (r.status !== 200) throw new Error("HTTP " + r.status);
  if (Number(r.headers.get("content-length") || 0) > MAX_BAJTU) {
    throw new Error("PDF je větší než " + MAX_BAJTU + " B");
  }
  const data = await precti(r.body, MAX_BAJTU);
  if (!jePdf(data)) throw new Error("odpověď není PDF");
  return data;
}

function odpovez(res, status, hlavicky, telo) {
  res.statusCode = status;
  for (const [k, v] of Object.entries(hlavicky)) res.setHeader(k, v);
  res.end(telo);
}

async function handler(req, res) {
  if (req.method !== "GET" && req.method !== "HEAD") {
    return odpovez(res, 405, { Allow: "GET, HEAD", "Cache-Control": "no-store" });
  }
  const p = parametry(req);
  if (p.soud !== "nss" || !/^\d{1,12}$/.test(p.id || "")) {
    return odpovez(res, 400, {
      "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store",
    }, "Neznámý dokument.\n");
  }
  const puvodni = NSS_PDF + p.id;
  let data;
  try {
    data = await stahniPdf(puvodni);
  } catch (e) {
    console.warn(`pdf: ${puvodni}: ${e && e.message}`);
    // Přesměrování se necachuje – příště to může vyjít.
    return odpovez(res, 302, { Location: puvodni, "Cache-Control": "no-store" });
  }
  // Rozhodnutí se po zveřejnění nemění: prohlížeč si PDF nechá den,
  // CDN Vercelu týden (další otevření už NSS nezatíží).
  odpovez(res, 200, {
    "Content-Type": "application/pdf",
    "Content-Length": String(data.length),
    "Content-Disposition": dispozice(jmenoSouboru(p.soubor)),
    "Cache-Control": "public, max-age=86400, s-maxage=604800",
  }, data);
}

module.exports = handler;
// Kvůli testům (tests/test_pdf_api.js).
module.exports.parametry = parametry;
module.exports.jmenoSouboru = jmenoSouboru;
module.exports.dispozice = dispozice;
module.exports.NSS_PDF = NSS_PDF;
module.exports.MAX_BAJTU = MAX_BAJTU;
