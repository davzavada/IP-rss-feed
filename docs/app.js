/* Owl – skript stránky. Čte hotové soubory vedle sebe (data/judikatura/*.json,
   data/casopisy.json, data/oblasti.json, digest.json,
   hearings.json, hearings.ics) a vykresluje je; nic nepočítá, co si už
   spočítaly scrapery. Přihlášení (Clerk) slouží jen k vlastnímu výběru –
   bez něj web ukazuje výchozí výběr. */

/* ========== Pomocné ========== */

/* Hodnoty z feedů escapujeme, aby zvláštní znaky (&, <, >, uvozovky)
   nerozbily stránku a nedal se přes ně vložit cizí HTML kód. */
function esc(s) {
  return String(s).replace(/[&<>"']/g, ch => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

// Do href pustíme jen http(s) odkazy (žádné javascript: apod.).
function safeHref(url) {
  return /^https?:\/\//i.test(url) ? esc(url) : "";
}

/* Položky ze všech zdrojů (JSON) převádíme na jednotné objekty, ať buňky
   tabulek nemusí rozlišovat zdroje:
     title, link, doc (PDF), heslo, shrnuti, poznamka, datum, nove, autori
   u judikatury navíc oblasti, senat, druh, procesni, u časopisů casopis a tag. */

// Novinky na úvodní stránce = poprvé viděné za posledních 7 dní.
const NOVE_DNI = 7;
const NOVE_MS = NOVE_DNI * 24 * 60 * 60 * 1000;

function zJson(r) {
  const prvni = Date.parse(r.first_seen || "");
  return {
    title: r.spz || r.nazev || "",
    // Populární název (u ÚS) jde pod značku; bez značky je sám titulkem.
    vec: r.spz && r.nazev ? r.nazev : "",
    link: r.url || "",
    doc: r.pdf && r.pdf !== r.url ? r.pdf : "",
    heslo: r.heslo || "",
    shrnuti: r.shrnuti || "",
    poznamka: r.poznamka || "",
    // Raná předběžná otázka z ipcuria ještě zveřejněná není – datum podání.
    datum: r.zverejneno || r.datum || r.first_seen || "",
    rozhodnuto: r.datum || "",
    zverejneno: r.zverejneno || "",
    nove: !isNaN(prvni) && Date.now() - prvni < NOVE_MS,
    prvni: isNaN(prvni) ? 0 : prvni,
    autori: "",
    oblasti: r.oblasti || [],
    sdeu: /^sdeu:/.test(r.id || ""),
    stav: r.stav_shrnuti || "",
    senat: r.senat,
    druh: r.druh || "",
    procesni: !!r.procesni,
    vysledek: r.vysledek || "",
    vysledekPopis: r.vysledek_popis || ""
  };
}

// Časopisy (data/casopisy.json): štítek je zkratka časopisu z registru.
let CASOPISY = [];                // [{id, zkratka, nazev, vydavatel}]

function zkratkaCasopisu(id) {
  const c = CASOPISY.find(x => x.id === id);
  return c ? c.zkratka : "";
}

function zCasopisu(r) {
  const prvni = Date.parse(r.first_seen || "");
  return {
    title: r.nazev || "",
    link: r.url || "",
    doc: "",
    heslo: r.heslo || "",
    shrnuti: r.shrnuti || "",
    poznamka: r.poznamka || "",
    datum: r.datum || r.first_seen || "",
    nove: !isNaN(prvni) && Date.now() - prvni < NOVE_MS,
    prvni: isNaN(prvni) ? 0 : prvni,
    autori: r.autori || "",
    casopis: r.casopis || "",
    tag: zkratkaCasopisu(r.casopis)
  };
}

// „1. 9. 2026" – z ISO data (bez posunu přes UTC, který by ukrojil den)
// i z čehokoli, co přečte Date (pubDate z RSS, časová značka z JSONu).
function czDate(value) {
  const s = String(value || "");
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (m) return +m[3] + ". " + +m[2] + ". " + m[1];
  const d = new Date(s);
  return isNaN(d) ? "" : d.toLocaleDateString("cs-CZ");
}

function fetchJson(url) {
  return fetch(url).then(r => {
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  });
}

function failed(containerId) {
  document.getElementById(containerId).innerHTML = '<p class="feed-empty">Nepodařilo se načíst feed.</p>';
}

/* ========== Štítky ========== */

function tagSlug(label) {
  return String(label).toLowerCase().replace(/[^a-z0-9]+/g, "-");
}

// Štítek časopisu / typu řízení.
function tagBadge(label) {
  if (!label) return "";
  return '<span class="tag tag-' + tagSlug(label) + '">' + esc(label) + "</span>";
}

// „[IIC] Název" -> „IIC"
function tagOf(title) {
  const m = title.match(/^\[([^\]]+)\]/);
  return m ? m[1] : "";
}

/* ========== Šířky sloupců (tažení za záhlaví, uložení do cookie) ========== */
// Výchozí šířky v procentech podle druhu sloupce; sloupec, který tu není
// (Shrnutí), si rozebere zbytek řádku – a má dostat nejvíc, je to hlavní
// obsah. Zdroj a druh jsou štítky, název značka s odkazem na PDF, heslo
// štítek, který se případně zalomí. Každá tabulka má jinou skladbu
// sloupců, takže se výchozí hodnoty počítají pro každou zvlášť.
const COL_DEFAULTS = { type: 10, name: 15, oblasti: 14, heslo: 13, src: 8, date: 8, author: 12 };
const MIN_COL_PCT = 4;          // pod tuhle šířku sloupec nepustíme
const KEY_STEP_PCT = 2;         // krok při ovládání šipkami
// Ve 3. verzi bez sloupce Datum – šířky uložené pro starou skladbu sloupců
// se zahodí (jiný název cookie).
const COL_COOKIE = "colw3";
const COL_COOKIE_MAX_AGE = 365 * 24 * 60 * 60;

// Skladba sloupců podle klíče tabulky – potřeba při obnovení výchozích šířek.
const tableColumns = {};

function readCookie(name) {
  const m = document.cookie.match("(?:^|; )" + name + "=([^;]*)");
  try {
    return m ? decodeURIComponent(m[1]) : "";
  } catch (e) {
    return "";
  }
}

function writeCookie(name, value) {
  try {
    document.cookie = name + "=" + encodeURIComponent(value) +
      ";path=/;max-age=" + COL_COOKIE_MAX_AGE + ";samesite=lax";
  } catch (e) { /* zakázané cookies – šířky prostě nepřežijí načtení */ }
}

// Cookie drží { klíč tabulky: [procenta sloupců] }. Poškozený obsah
// (ruční úprava, starší verze stránky) zahodíme a jedeme na výchozích.
function loadWidths() {
  try {
    const data = JSON.parse(readCookie(COL_COOKIE) || "{}");
    return data && typeof data === "object" && !Array.isArray(data) ? data : {};
  } catch (e) {
    return {};
  }
}

const storedWidths = loadWidths();

function colKey(c) {
  return String(c.cls || "").replace(/^col-/, "");
}

function defaultWidths(columns) {
  // Výchozí šířka je podle typu sloupce; tabulka si ji může přebít (`width`),
  // když má sloupců míň a je kam růst.
  const fixed = columns.map(c => c.width || COL_DEFAULTS[colKey(c)] || 0);
  const rest = 100 - fixed.reduce((a, b) => a + b, 0);
  const flexible = fixed.filter(w => !w).length;
  return fixed.map(w => w || Math.max(MIN_COL_PCT, rest / (flexible || 1)));
}

// Uložené šířky bereme jen tehdy, když sedí na aktuální skladbu sloupců;
// součet dorovnáme na 100 %, ať se nesejde tabulka širší nebo užší než řádek.
function ulozeneSirky(key, columns) {
  const saved = storedWidths[key];
  const usable = Array.isArray(saved) && saved.length === columns.length &&
    saved.every(w => typeof w === "number" && isFinite(w) && w >= 1);
  return usable ? saved : null;
}

function widthsFor(key, columns) {
  const saved = ulozeneSirky(key, columns);
  if (!saved) return defaultWidths(columns);
  const sum = saved.reduce((a, b) => a + b, 0);
  return saved.map(w => (w / sum) * 100);
}

function saveWidths(key, widths) {
  if (!key) return;
  storedWidths[key] = widths.map(w => Math.round(w * 100) / 100);
  writeCookie(COL_COOKIE, JSON.stringify(storedWidths));
}

function currentWidths(table) {
  return Array.from(table.querySelectorAll("col")).map(c => parseFloat(c.style.width) || 0);
}

function applyWidths(table, widths) {
  const cols = table.querySelectorAll("col");
  widths.forEach((w, i) => { if (cols[i]) cols[i].style.width = w.toFixed(2) + "%"; });
}

// Posun hranice mezi sloupci i a i+1: co jeden získá, druhý ztratí, takže
// zbytek tabulky zůstane, kde byl. Vrací nové šířky (v procentech).
function moveBoundary(widths, i, deltaPct) {
  const pair = widths[i] + widths[i + 1];
  const next = widths.slice();
  next[i] = Math.max(MIN_COL_PCT, Math.min(widths[i] + deltaPct, pair - MIN_COL_PCT));
  next[i + 1] = pair - next[i];
  return next;
}

function boundaryOf(handle) {
  const th = handle.parentElement;
  const table = th.closest("table");
  return { table, index: Array.prototype.indexOf.call(th.parentElement.children, th) };
}

function resetWidths(table) {
  const key = table.dataset.cols;
  const columns = tableColumns[key];
  if (!columns) return;
  applyWidths(table, defaultWidths(columns));
  delete storedWidths[key];
  writeCookie(COL_COOKIE, JSON.stringify(storedWidths));
  prizpusobSirky(table);
}

// Výchozí šířky podle obsahu: sloupce kolem shrnutí (zdroj, značka, heslo,
// datum…) jsou tak široké, jak potřebuje jejich nejdelší položka, s malou
// rezervou; shrnutí dostane zbytek. Delší obsah (název článku, populární
// název, dlouhé heslo) má strop v em a zalomí se. Změřit jde jen viditelnou
// tabulku – na skryté stránce se to dopočítá při jejím zobrazení, a znovu
// při změně šířky okna. Kdo si sloupce potáhl, má uložené vlastní šířky
// a ty platí dál.
const FIT_MAX_EM = { name: 18, heslo: 16, author: 14, type: 12, oblasti: 14 };
// Štítky zdroje stojí pod sebou – sloupci stačí nejširší z nich.
const FIT_MIN_CONTENT = { src: true };
const FIT_REZERVA_PX = 6;
const MIN_SHRNUTI_PCT = 38;

// Přirozená šířka obsahu sloupce v px (včetně odsazení buňky); null
// u shrnutí a u sloupců s pevnou šířkou z definice.
function zmerObsah(table, columns) {
  // Řádky dní (Novinky) mají jednu buňku přes celou šířku – neměří se.
  const telo = table.querySelector("tbody tr:not(.den)");
  if (!telo) return columns.map(() => null);
  const pismo = parseFloat(getComputedStyle(table).fontSize) || 14;
  const meric = document.createElement("div");
  // Měrka stojí vedle tabulky, písmo tabulky (menší než okolí) jí dáme ručně.
  meric.style.fontSize = pismo + "px";
  table.parentNode.appendChild(meric);
  const sirky = columns.map((c, j) => {
    const k = colKey(c);
    if (c.width || k === "summary") return null;
    meric.className = "meric " + (c.cls || "");
    meric.style.width = FIT_MIN_CONTENT[k] ? "min-content" : "max-content";
    meric.classList.add("meric-th");
    meric.innerHTML = esc(c.label);
    const zahlavi = meric.getBoundingClientRect().width;
    meric.classList.remove("meric-th");
    const hodnoty = [];
    Array.from(telo.parentNode.rows).forEach(tr => {
      // Prázdné buňky (článek bez autora) šířku neurčují, řádky dní taky ne.
      if (tr.classList.contains("den") || !tr.cells[j] || !tr.cells[j].textContent.trim()) return;
      meric.innerHTML = tr.cells[j].innerHTML;
      hodnoty.push(meric.getBoundingClientRect().width);
    });
    // Sloupec se řídí zhruba devadesátým percentilem, ne nejdelší položkou –
    // pár výjimek (dlouhé heslo) se radši zalomí, než aby kvůli nim byl
    // široký celý sloupec. U krátkých tabulek to vyjde na nejdelší.
    hodnoty.sort((a, b) => a - b);
    const typicka = hodnoty.length ? hodnoty[Math.ceil(0.9 * (hodnoty.length - 1))] : 0;
    const strop = FIT_MAX_EM[k] ? FIT_MAX_EM[k] * pismo : Infinity;
    const cs = getComputedStyle(telo.cells[j]);
    return Math.max(zahlavi, Math.min(typicka, strop)) + parseFloat(cs.paddingLeft) +
      parseFloat(cs.paddingRight) + FIT_REZERVA_PX;
  });
  meric.remove();
  return sirky;
}

function prizpusobSirky(table) {
  const key = table && table.dataset.cols;
  const columns = key && tableColumns[key];
  if (!columns || ulozeneSirky(key, columns) || getComputedStyle(table).display === "block") return;
  const celkem = table.getBoundingClientRect().width;
  if (!celkem) return;
  if (!table._obsah) table._obsah = zmerObsah(table, columns);
  const px = table._obsah;
  const pevne = columns.reduce((s, c) => s + (c.width || 0), 0);
  let obsah = px.map(w => (w == null ? 0 : (w / celkem) * 100));
  const soucet = obsah.reduce((a, b) => a + b, 0);
  const smi = Math.max(0, 100 - MIN_SHRNUTI_PCT - pevne);
  if (soucet > smi) obsah = obsah.map(p => (p * smi) / soucet);
  const pruzne = columns.filter((c, j) => !c.width && px[j] == null).length || 1;
  const widths = columns.map((c, j) => c.width || (px[j] == null ? 0 : Math.max(MIN_COL_PCT, obsah[j])));
  const zbytek = 100 - widths.reduce((a, b) => a + b, 0);
  applyWidths(table, widths.map((w, j) => (!columns[j].width && px[j] == null ? zbytek / pruzne : w)));
}

function prizpusobViditelne(koren) {
  (koren || document).querySelectorAll("table[data-cols]").forEach(prizpusobSirky);
}

let sirkyRaf = 0;
window.addEventListener("resize", () => {
  cancelAnimationFrame(sirkyRaf);
  sirkyRaf = requestAnimationFrame(() => prizpusobViditelne());
});

function initColumnResize() {
  let drag = null;

  document.addEventListener("pointerdown", e => {
    const handle = e.target.closest(".col-resizer");
    if (!handle || (e.pointerType === "mouse" && e.button !== 0)) return;
    const { table, index } = boundaryOf(handle);
    // Šířky měříme v pixelech ze skutečně vykreslené tabulky – tažení pak
    // sedí na pohyb myši i tam, kde tabulka přetéká (min-width: 780px).
    const cells = handle.parentElement.parentElement.children;
    drag = {
      table, index, handle,
      startX: e.clientX,
      total: table.getBoundingClientRect().width,
      a: cells[index].getBoundingClientRect().width,
      b: cells[index + 1].getBoundingClientRect().width
    };
    handle.classList.add("is-active");
    document.body.classList.add("is-resizing");
    handle.setPointerCapture(e.pointerId);
    e.preventDefault();
  });

  document.addEventListener("pointermove", e => {
    if (!drag || !drag.total) return;
    const pct = w => (w / drag.total) * 100;
    const widths = currentWidths(drag.table);
    widths[drag.index] = pct(drag.a);
    widths[drag.index + 1] = pct(drag.b);
    applyWidths(drag.table, moveBoundary(widths, drag.index, pct(e.clientX - drag.startX)));
  });

  function endDrag() {
    if (!drag) return;
    drag.handle.classList.remove("is-active");
    document.body.classList.remove("is-resizing");
    saveWidths(drag.table.dataset.cols, currentWidths(drag.table));
    drag = null;
  }

  document.addEventListener("pointerup", endDrag);
  document.addEventListener("pointercancel", endDrag);

  // Dvojklik na hranici = zpátky na výchozí šířky celé tabulky.
  document.addEventListener("dblclick", e => {
    const handle = e.target.closest(".col-resizer");
    if (!handle) return;
    e.preventDefault();
    resetWidths(boundaryOf(handle).table);
  });

  // Bez myši: táhlo je ve fokusu a šipky s ním hýbou.
  document.addEventListener("keydown", e => {
    const handle = e.target.closest && e.target.closest(".col-resizer");
    if (!handle) return;
    const step = e.key === "ArrowLeft" ? -KEY_STEP_PCT
      : e.key === "ArrowRight" ? KEY_STEP_PCT : 0;
    const { table, index } = boundaryOf(handle);
    if (step) {
      e.preventDefault();
      const widths = moveBoundary(currentWidths(table), index, step);
      applyWidths(table, widths);
      saveWidths(table.dataset.cols, widths);
    } else if (e.key === "Home") {
      e.preventDefault();
      resetWidths(table);
    }
  });
}

/* ========== Seznamy položek ========== */

// Vyhledávač NSS posílá PDF ke stažení; na webu na Vercelu ho proto
// odkaz otevře přes náhled (api/pdf.js), který ho prohlížeči předá
// k zobrazení. Jméno souboru na konci adresy je spisová značka – ukáže se
// v titulku karty. Lokálně (bez funkcí Vercelu) vede odkaz rovnou na NSS;
// PDF Nejvyššího soudu prohlížeč ukáže sám.
const NAHLED_PDF = /(^|\.)davidzavada\.cz$|\.vercel\.app$/.test(location.hostname);
const NSS_PDF_RE = /^https:\/\/vyhledavac\.nssoud\.cz\/DokumentOriginal\/Index\/(\d+)$/;

function pdfHref(item) {
  const nss = NAHLED_PDF && NSS_PDF_RE.exec(item.doc || "");
  if (!nss) return safeHref(item.doc);
  const jmeno = item.title.replace(/[/\\]+/g, "-").replace(/[^\p{L}\p{N} ._-]+/gu, "")
    .replace(/\s+/g, " ").trim() || "rozhodnuti";
  return "/pdf/nss/" + nss[1] + "/" + encodeURIComponent(jmeno + ".pdf");
}

function nameCell(item) {
  const title = esc(item.title.replace(/^\[[^\]]+\]\s*/, ""));
  const href = safeHref(item.link);
  let html = href ? '<a href="' + href + '">' + title + "</a>" : title;
  // Odkaz ještě na samotný dokument (PDF rozhodnutí, znění předběžné otázky)
  // – shrnutí je jen shrnutí. Otevře se v nové kartě, ať čtenář nepřijde
  // o místo v tabulce.
  const doc = pdfHref(item);
  if (doc) html += ' <a class="doc-link" href="' + doc + '" target="_blank" rel="noopener">PDF</a>';
  if (item.vec) html += '<span class="vec">' + esc(item.vec) + "</span>";
  return html + datumRadek(item);
}

// Drobný řádek s datem pod značkou – u rozhodnutí datum vydání („ze dne",
// jak se cituje), zveřejnění v bublině; u článku datum vydání čísla.
function datumRadek(item) {
  if (item.casopis !== undefined) {
    return item.datum ? '<span class="datum-radek">' + czDate(item.datum) + "</span>" : "";
  }
  const d = item.rozhodnuto || item.datum;
  if (!d) return "";
  const pred = item.druh === "předběžná otázka" ? "podáno " : "ze dne ";
  const zv = item.zverejneno && item.zverejneno !== item.rozhodnuto ? item.zverejneno : "";
  return '<span class="datum-radek"' + (zv ? ' title="Zveřejněno ' + esc(czDate(zv)) + '"' : "") + ">" +
    pred + czDate(d) + "</span>";
}

function authorCell(item) {
  return item.autori ? '<span class="author">' + esc(item.autori) + "</span>" : "";
}

// V přehledu přes všechny zdroje jdou autoři pod název (má je jen část položek).
function nameAuthorCell(item) {
  if (item.casopis === undefined) return nameCell(item) + authorCell(item);
  // Článek: název, autoři, pak datum.
  const bezData = Object.assign({}, item, { datum: "" });
  return nameCell(bezData) + authorCell(item) + datumRadek(item);
}

function hesloCell(item) {
  const tag = item.heslo;
  return tag ? '<span class="heslo" title="' + esc(tag) + '">' + esc(tag) + "</span>" : "";
}

// Výsledek rozhodnutí před shrnutím – tlumeně, kapitálkami. U NSS a ÚS
// z úředního výroku (jeho znění je v bublině), u NS a SDEU ho určila AI
// z textu rozhodnutí (judikatura/vysledky.py).
const VYSLEDKY = {
  odmitnuto: "Odmítnuto", zamitnuto: "Zamítnuto", zruseno_vraceno: "Zrušeno a vráceno",
  zruseno: "Zrušeno", zmeneno: "Změněno", vyhoveno: "Vyhověno", castecne: "Zčásti vyhověno",
  zastaveno: "Zastaveno"
};

function vysledekHtml(item) {
  const nazev = VYSLEDKY[item.vysledek];
  if (!nazev) return "";
  const title = item.vysledekPopis ? "Výrok: " + item.vysledekPopis : "Výsledek určila AI z textu rozhodnutí";
  return '<span class="vysledek" title="' + esc(title) + '">' + nazev + "</span>";
}

function summaryCell(item) {
  const vysledek = vysledekHtml(item);
  const pred = vysledek ? vysledek + '<span class="vysledek-odd"> · </span>' : "";
  if (item.shrnuti) return '<span class="summary">' + pred + esc(item.shrnuti) + "</span>";
  // Bez shrnutí ještě může být poznámka, proč žádné není – třeba že u žádosti
  // o předběžnou otázku zatím nejsou zveřejněné otázky.
  if (item.poznamka) {
    return '<span class="summary">' + pred + '<span class="note">' + esc(item.poznamka) + "</span></span>";
  }
  return vysledek ? '<span class="summary">' + vysledek + "</span>" : "";
}


// U SDEU druh rozhodnutí (rozsudek, stanovisko GA, předběžná otázka…),
// u časopisů zkratka časopisu z titulku.
const DRUHY_SDEU = { "rozsudek": "tag-ruling", "předběžná otázka": "tag-referral",
                     "stanovisko GA": "tag-opinion" };

function typeCell(item) {
  if (item.sdeu && item.druh) {
    return '<span class="tag ' + (DRUHY_SDEU[item.druh] || "") + '">' + esc(item.druh) + "</span>";
  }
  return tagBadge(item.tag || tagOf(item.title));
}

/* ========== Oblasti a výběr (výchozí, nebo uložený u účtu) ========== */
// Seznam oblastí a senátů NS čte stránka z data/ (stejné soubory jako AI).
// Nepřihlášený vidí výchozí výběr (IP a IT); přihlášený svůj, uložený
// v Clerku jako user.unsafeMetadata.owl:
//   {"v":1,"ns":{"oblasti":[…],"senaty":[23]},"nss":{"oblasti":[…]},
//    "us":{…},"sdeu":{…},"skryte_casopisy":[]}
// (Dřívější „skryt_procesni" se ignoruje – procesní rozhodnutí jsou vidět vždy.)
let OBLASTI = {};                 // id -> název
let OBLASTI_SEZNAM = [];          // oblasti v pořadí seznamu (se skupinou)
let ALIAS_OBLASTI = {};           // přejmenované id -> nové
let VYCHOZI_OBLASTI = [];
let VYCHOZI_SENATY_NS = [23];
let SENATY_NS = [];               // [{senat, kolegium, popis}]

// Soudy, u kterých se filtruje podle oblastí. Výběr je uložený u každého
// soudu zvlášť, ale stránka Můj výběr ho zatím nastavuje všem stejně.
// `nazev2` je 2. pád do vět.
const SOUDY_VYBERU = [
  { soud: "ns", zkratka: "NS", nazev: "Nejvyšší soud", nazev2: "Nejvyššího soudu" },
  { soud: "nss", zkratka: "NSS", nazev: "Nejvyšší správní soud", nazev2: "Nejvyššího správního soudu" },
  { soud: "us", zkratka: "ÚS", nazev: "Ústavní soud", nazev2: "Ústavního soudu" },
  { soud: "sdeu", zkratka: "SDEU", nazev: "Soudní dvůr EU", nazev2: "Soudního dvora EU" }
];

let vyber = null;                 // platný výběr (výchozí nebo z účtu)

function nastavVyber(oblasti, senaty) {
  if (oblasti && Array.isArray(oblasti.oblasti)) {
    OBLASTI = {};
    OBLASTI_SEZNAM = oblasti.oblasti;
    oblasti.oblasti.forEach(o => { OBLASTI[o.id] = o.nazev; });
    ALIAS_OBLASTI = oblasti.alias || {};
    VYCHOZI_OBLASTI = oblasti.oblasti.filter(o => o.vychozi).map(o => o.id);
  }
  if (senaty) {
    if (Array.isArray(senaty.vychozi)) VYCHOZI_SENATY_NS = senaty.vychozi;
    if (Array.isArray(senaty.senaty)) SENATY_NS = senaty.senaty;
  }
  vyber = vychoziVyber();
}

function vychoziVyber() {
  const v = { v: 1, skryte_casopisy: [] };
  SOUDY_VYBERU.forEach(s => { v[s.soud] = { oblasti: VYCHOZI_OBLASTI.slice() }; });
  v.ns.senaty = VYCHOZI_SENATY_NS.slice();
  return v;
}

function bezDuplicit(seznam) {
  return seznam.filter((x, i) => seznam.indexOf(x) === i);
}

// Uložený výběr projde přes známá id (přejmenované oblasti přes alias);
// co v něm chybí, doplní výchozí – účet tak přežije i nové soudy a oblasti.
function normalizujVyber(ulozeny) {
  const v = vychoziVyber();
  if (!ulozeny || typeof ulozeny !== "object") return v;
  SOUDY_VYBERU.forEach(s => {
    const u = ulozeny[s.soud];
    if (!u || !Array.isArray(u.oblasti)) return;
    v[s.soud].oblasti = bezDuplicit(u.oblasti.map(o => ALIAS_OBLASTI[o] || o).filter(o => OBLASTI[o]));
  });
  if (ulozeny.ns && Array.isArray(ulozeny.ns.senaty)) {
    v.ns.senaty = bezDuplicit(ulozeny.ns.senaty.map(Number).filter(n => Number.isInteger(n) && n > 0));
  }
  if (Array.isArray(ulozeny.skryte_casopisy)) {
    // Starší výběry ukládaly zkratku („GRUR Int"), teď se ukládá id z registru.
    const idCasopisu = c => (CASOPISY.find(x => x.zkratka === c) || {}).id || c;
    v.skryte_casopisy = bezDuplicit(ulozeny.skryte_casopisy.filter(c => typeof c === "string")
      .map(idCasopisu));
  }
  return seradVyber(v);
}

function jeVychozi(v) {
  return JSON.stringify(v) === JSON.stringify(vychoziVyber());
}

// Rozhodnutí NS je vidět, když je z vybraného senátu, nebo když spadá do
// některé z vybraných oblastí.
function vidiNS(item) {
  const v = vyber || vychoziVyber();
  if (v.ns.senaty.indexOf(item.senat) >= 0) return true;
  return (item.oblasti || []).some(o => v.ns.oblasti.indexOf(o) >= 0);
}

// NSS a ÚS: rozhodnutí je vidět, když spadá do některé z oblastí vybraných
// u jeho soudu (dokud ho AI nezařadí, platí oblasti podle úředních údajů).
function vidiPodleOblasti(soud) {
  return item => {
    const v = vyber || vychoziVyber();
    return (item.oblasti || []).some(o => v[soud].oblasti.indexOf(o) >= 0);
  };
}

function vidiCasopis(item) {
  const v = vyber || vychoziVyber();
  return v.skryte_casopisy.indexOf(item.casopis) < 0;
}

// Definice sloupců sdílíme mezi živým feedem a novými položkami.
const colsNsoud = [
  { label: "Spisová značka", cls: "col-name", render: nameCell },
  { label: "Heslo", cls: "col-heslo", render: hesloCell },
  { label: "Shrnutí", cls: "col-summary", render: summaryCell }
];

const colsNss = [
  { label: "Číslo jednací", cls: "col-name", render: nameCell },
  { label: "Heslo", cls: "col-heslo", render: hesloCell },
  { label: "Shrnutí", cls: "col-summary", render: summaryCell }
];

const colsSdeu = [
  { label: "Druh", cls: "col-type", render: typeCell },
  { label: "Věc", cls: "col-name", render: nameCell },
  { label: "Heslo", cls: "col-heslo", render: hesloCell },
  { label: "Shrnutí", cls: "col-summary", render: summaryCell }
];

const colsJournals = [
  { label: "Časopis", cls: "col-type", render: typeCell },
  { label: "Název", cls: "col-name", render: nameCell, width: 26 },
  { label: "Autor", cls: "col-author", render: authorCell },
  // U časopisů, které datum vydání neuvádějí, je datum pod názvem to, kdy
  // článek ve feedu přibyl (viz pub_date_odhad ve scraper_journals.py).
  { label: "Shrnutí", cls: "col-summary", render: summaryCell }
];

// Nové položky – kombinovaná tabulka přes všechny tři zdroje.
// Ve sloupci Zdroj je vedle sebe štítek zdroje a štítek časopisu/typu.
const colsToday = [
  { label: "Zdroj", cls: "col-src", render: i =>
      '<span class="src src-' + i._src + '">' + i._srcLabel + "</span> " + typeCell(i) },
  { label: "Název", cls: "col-name", render: nameAuthorCell },
  { label: "Heslo", cls: "col-heslo", render: hesloCell },
  { label: "Shrnutí", cls: "col-summary", render: summaryCell }
];

// Zdroje stránky: okna judikatury (data/judikatura/) a časopisy
// (data/casopisy.json); `filtr` je výběr, co z okna ukázat.
const FEEDS = [
  { key: "nsoud",    label: "NS",      json: "data/judikatura/ns.json", cols: colsNsoud,
    containerId: "feed-nsoud", filtr: vidiNS, stavId: "stav-nsoud" },
  { key: "nss",      label: "NSS",     json: "data/judikatura/nss.json", cols: colsNss,
    containerId: "feed-nss", filtr: vidiPodleOblasti("nss"), stavId: "stav-nss" },
  // ÚS: značka a pod ní populární název (nameCell), jinak stejné sloupce jako NS.
  { key: "us",       label: "ÚS",      json: "data/judikatura/us.json", cols: colsNsoud,
    containerId: "feed-us", filtr: vidiPodleOblasti("us"), stavId: "stav-us" },
  { key: "sdeu",     label: "SDEU",    json: "data/judikatura/sdeu.json", cols: colsSdeu,
    containerId: "feed-sdeu", filtr: vidiPodleOblasti("sdeu"), stavId: "stav-sdeu" },
  { key: "journals", label: "Časopis", json: "data/casopisy.json", prevod: zCasopisu, cols: colsJournals,
    containerId: "feed-journals", filtr: vidiCasopis }
];

// Stránka zdroje v navigaci (časopisy mají stránku #casopisy).
const STRANKA_ZDROJE = { journals: "casopisy" };

// V boční navigaci u každého zdroje nenápadně vpravo, za jak dlouhou dobu
// ukazuje novinky (okno dat, okno_dni v JSON – u soudů 14 nebo 30 dní).
function ukazOkna() {
  FEEDS.forEach(f => {
    const a = document.querySelector('#sidenav a[href="#' + (STRANKA_ZDROJE[f.key] || f.key) + '"]');
    if (!a || !f.oknoDni) return;
    let el = a.querySelector(".nav-okno");
    if (!el) {
      el = document.createElement("span");
      el.className = "nav-okno";
      a.appendChild(el);
    }
    el.textContent = tvar(f.oknoDni, "den", "dny", "dní");
    el.title = "Novinky za posledních " + el.textContent;
  });
}

// Položky zdroje jako objekty; u zdroje si poznamená, kdy byl aktualizován.
function nactiZdroj(f) {
  return fetchJson(f.json).then(d => {
    f.aktualizovano = d.generated || "";
    f.oknoDni = d.okno_dni || 0;
    if (Array.isArray(d.casopisy)) nastavCasopisy(d.casopisy);
    return (d.polozky || []).map(f.prevod || zJson);
  });
}

function resizerHtml(column) {
  return '<span class="col-resizer" role="separator" aria-orientation="vertical"' +
    ' tabindex="0" aria-label="Šířka sloupce ' + esc(column.label) + '"' +
    ' title="Táhnutím změníte šířku sloupce, dvojklikem obnovíte výchozí"></span>';
}

// `prazdno` je HTML hlášky, když nic není (u filtrovaných karet odkaz na výběr).
// `moznosti.skupina(item)` vrací HTML nadpisu skupiny (Novinky: den) – když
// se změní, vloží se řádek přes celou šířku; `moznosti.trida(item)` třídu řádku.
function renderTable(items, container, columns, key, prazdno, moznosti) {
  const m = moznosti || {};
  if (items.length === 0) {
    container.innerHTML = '<p class="feed-empty">' + (prazdno || "Žádné nové položky.") + "</p>";
    return;
  }
  tableColumns[key] = columns;
  // Role výslovně: v blokovém zobrazení (úzká karta) by display: block/flex
  // tabulce vzal význam a čtečka by ztratila řádky a záhlaví.
  let html = '<div class="table-wrap"><table role="table" data-cols="' + esc(key) + '"><colgroup>';
  widthsFor(key, columns).forEach(w => { html += '<col style="width:' + w.toFixed(2) + '%">'; });
  html += '</colgroup><thead role="rowgroup"><tr role="row">';
  columns.forEach((c, i) => {
    // Poslední sloupec už nemá kam růst – hranice je vždy mezi dvěma sloupci.
    html += '<th role="columnheader"' + (c.cls ? ' class="' + c.cls + '"' : "") + ">" + c.label +
      (i < columns.length - 1 ? resizerHtml(c) : "") + "</th>";
  });
  html += '</tr></thead><tbody role="rowgroup">';
  let skupina = null;
  items.forEach(item => {
    const sk = m.skupina ? m.skupina(item) : null;
    if (sk !== null && sk !== skupina) {
      skupina = sk;
      html += '<tr class="den" role="row"><th role="rowheader" colspan="' + columns.length + '" scope="colgroup">' +
        sk + "</th></tr>";
    }
    const trida = m.trida ? m.trida(item) : "";
    html += '<tr role="row"' + (trida ? ' class="' + trida + '"' : "") + ">";
    columns.forEach(c => {
      html += '<td role="cell"' + (c.cls ? ' class="' + c.cls + '"' : "") + ">" + c.render(item) + "</td>";
    });
    html += "</tr>";
  });
  html += "</tbody></table></div>";
  container.innerHTML = html;
  prizpusobSirky(container.querySelector("table"));
}

/* ========== Novinky: posledních 7 dní po dnech ========== */
// Minulá návštěva: od kdy se novinky značí tečkou. Návštěva končí půl hodiny
// po posledním pohybu na stránce (zápis při startu, skrytí a odchodu), takže
// obnovení stránky tečky nesmaže; při další návštěvě se počítá od konce té
// minulé. Při první návštěvě tečky nejsou.
const NAVSTEVA_KLIC = "owl:navsteva";
const NAVSTEVA_OD_KLIC = "owl:navsteva-od";
const RELACE_MS = 30 * 60 * 1000;

function zapisNavstevu() {
  try {
    localStorage.setItem(NAVSTEVA_KLIC, new Date().toISOString());
  } catch (e) { /* bez úložiště prostě bez teček */ }
}

const noveOd = (function () {
  try {
    const posledni = Date.parse(localStorage.getItem(NAVSTEVA_KLIC) || "");
    let od = Date.parse(localStorage.getItem(NAVSTEVA_OD_KLIC) || "");
    if (!isNaN(posledni) && Date.now() - posledni > RELACE_MS) {
      od = posledni;
      localStorage.setItem(NAVSTEVA_OD_KLIC, new Date(od).toISOString());
    }
    return od;
  } catch (e) {
    return NaN;
  }
})();
zapisNavstevu();
window.addEventListener("pagehide", zapisNavstevu);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") zapisNavstevu();
});

function jeNoveOdMinula(item) {
  return !isNaN(noveOd) && item.prvni > noveOd;
}

// „Dnes", „Včera", „Po 21. 9." – den prvního výskytu v místním čase.
function nadpisDne(ts) {
  const d = new Date(ts);
  const dnes = new Date();
  const vcera = new Date(dnes.getFullYear(), dnes.getMonth(), dnes.getDate() - 1);
  if (isoOf(d) === isoOf(dnes)) return "Dnes";
  if (isoOf(d) === isoOf(vcera)) return "Včera";
  return CAL_DOWS[(d.getDay() + 6) % 7] + " " + d.getDate() + ". " + (d.getMonth() + 1) + ".";
}

// `results` jsou výsledky Promise.allSettled nad položkami jednotlivých feedů.
function renderToday(results) {
  const container = document.getElementById("feed-today");
  // Když selžou všechny feedy, není to „nic nového", ale chyba načítání.
  if (results.every(r => r.status === "rejected")) {
    failed("feed-today");
    return;
  }
  const polozky = [];
  results.forEach((r, idx) => {
    if (r.status !== "fulfilled") return;
    r.value.forEach(item => {
      if (item.nove) {
        item._src = FEEDS[idx].key;
        item._srcLabel = FEEDS[idx].label;
        polozky.push(item);
      }
    });
  });
  // Po dnech prvního výskytu od nejnovějšího, v rámci dne podle data.
  polozky.sort((a, b) => (isoOf(new Date(b.prvni)) > isoOf(new Date(a.prvni)) ? 1
    : isoOf(new Date(b.prvni)) < isoOf(new Date(a.prvni)) ? -1 : new Date(b.datum) - new Date(a.datum)));
  if (polozky.length === 0) {
    container.innerHTML = '<p class="feed-empty">Za posledních ' + tvar(NOVE_DNI, "den", "dny", "dní") +
      " nic nepřibylo. Sběr běží jednou denně ve 2:00 v noci.</p>";
    return;
  }
  const poctyDni = {};
  polozky.forEach(i => { const d = isoOf(new Date(i.prvni)); poctyDni[d] = (poctyDni[d] || 0) + 1; });
  const odMinula = polozky.filter(jeNoveOdMinula).length;
  renderTable(polozky, container, colsToday, "today", "", {
    skupina: i => {
      const d = isoOf(new Date(i.prvni));
      return esc(nadpisDne(i.prvni)) + ' <span class="den-pocet">' + poctyDni[d] + "</span>";
    },
    trida: i => (jeNoveOdMinula(i) ? "nove" : "")
  });
  if (odMinula) {
    container.insertAdjacentHTML("afterbegin", '<p class="nove-souhrn"><span class="tecka" aria-hidden="true">' +
      "</span>" + tvar(odMinula, "novinka", "novinky", "novinek") + " od vaší minulé návštěvy</p>");
  }
}

// Stažené položky zdrojů (výsledky Promise.allSettled). Při změně výběru se
// jen znovu profiltrují a vykreslí – nic se znovu nestahuje.
let zdrojeVysledky = null;

// Nad kartou judikatury: kolik rozhodnutí z celého okna (ne jen z výběru)
// ještě nemá shrnutí. Rozhodnutí bez oblasti se do výběru podle oblastí
// dostanou až po AI, tak ať je jasné, že ještě přibudou. (U NSS a ÚS má
// většina oblast už podle údajů soudu.)
function stavShrnutiText(polozky, oknoDni) {
  const cekajici = polozky.filter(i => i.stav === "pripravuje");
  const bezOblasti = cekajici.filter(i => !(i.oblasti || []).length).length;
  const bezTextu = polozky.filter(i => i.stav === "ceka_na_text").length;
  const casti = [];
  if (cekajici.length) {
    casti.push("AI ještě zpracovává " + cekajici.length + " z " + polozky.length + " rozhodnutí" +
      (oknoDni ? " za posledních " + oknoDni + " dní" : "") + ". Shrnutí a oblasti doplní " +
      "při nočním sběru (ve 2:00)" +
      (bezOblasti === cekajici.length
        ? ", do výběru podle oblastí se tato rozhodnutí dostanou až potom."
        : bezOblasti
          ? "; " + bezOblasti + " z nich zatím " +
            (bezOblasti >= 2 && bezOblasti <= 4
              ? "nemají oblast a do výběru podle oblastí se dostanou až potom."
              : "nemá oblast a do výběru podle oblastí se dostane až potom.")
          : ", do té doby jsou zařazená podle údajů soudu."));
  }
  if (bezTextu) casti.push("U " + bezTextu + " rozhodnutí soud ještě nezveřejnil text.");
  return casti.join(" ");
}

function vykresliStavShrnuti() {
  FEEDS.forEach((f, idx) => {
    const el = f.stavId && document.getElementById(f.stavId);
    if (!el) return;
    const r = zdrojeVysledky[idx];
    const text = r.status === "fulfilled" ? stavShrnutiText(r.value, f.oknoDni) : "";
    el.textContent = text;
    el.hidden = !text;
  });
}

function vykresliZdroje() {
  if (!zdrojeVysledky) return;
  vykresliStavShrnuti();
  const vybrane = zdrojeVysledky.map((r, idx) => {
    const f = FEEDS[idx];
    if (r.status !== "fulfilled" || !f.filtr) return r;
    return { status: "fulfilled", value: r.value.filter(f.filtr) };
  });
  FEEDS.forEach((f, idx) => {
    if (zdrojeVysledky[idx].status === "rejected") {
      failed(f.containerId);
      vykresliFiltr(f.key, null, null);
      return;
    }
    const vse = zdrojeVysledky[idx].value;
    const vyb = vybrane[idx].value;
    vykresliFiltr(f.key, vyb.length, vse.length);
    renderTable(rezim(f.key) === "vse" ? vse : vyb, document.getElementById(f.containerId),
                f.cols, f.key, prazdnoHtml(f, vse.length));
  });
  const nove = vysledky => vysledky.reduce((n, r) =>
    n + (r.status === "fulfilled" ? r.value.filter(i => i.nove).length : 0), 0);
  vykresliFiltr("today", nove(vybrane), nove(zdrojeVysledky));
  renderToday(rezim("today") === "vse" ? zdrojeVysledky : vybrane);
}

/* ========== Lišta filtru: Můj výběr | Vše ========== */
// Stránky zdrojů ukazují jen to, co odpovídá výběru (nepřihlášenému
// výchozímu IP a IT). Lišta pod nadpisem to říká nahlas – kolik z kolika
// a podle čeho – a jedním klikem přepne na všechno. Přepnutí platí jen
// v této relaci prohlížeče, uložený výběr nemění.
const REZIM_KLIC = "owl:rezim";
const SOUD_ZDROJE = { nsoud: "ns", nss: "nss", us: "us", sdeu: "sdeu" };
let rezimy = {};
try {
  rezimy = JSON.parse(sessionStorage.getItem(REZIM_KLIC) || "{}") || {};
} catch (e) {
  rezimy = {};
}

function rezim(key) {
  return rezimy[key] === "vse" ? "vse" : "vyber";
}

function nastavRezim(key, r) {
  rezimy[key] = r === "vse" ? "vse" : "vyber";
  try {
    sessionStorage.setItem(REZIM_KLIC, JSON.stringify(rezimy));
  } catch (e) { /* bez úložiště platí přepnutí jen do obnovení stránky */ }
  vykresliZdroje();
  const tl = document.querySelector('#filtr-' + key + ' [data-rezim="' + rezimy[key] + '"]');
  if (tl) tl.focus({ preventScroll: true });
}

function stejneMnoziny(a, b) {
  return a.length === b.length && a.every(x => b.indexOf(x) >= 0);
}

function popisOblasti(oblasti) {
  if (stejneMnoziny(oblasti, VYCHOZI_OBLASTI)) return "IP a IT";
  if (oblasti.length === OBLASTI_SEZNAM.length) return "všechny oblasti";
  return oblasti.length ? tvar(oblasti.length, "oblast", "oblasti", "oblastí") : "";
}

// Krátký popis, podle čeho se filtruje: „IP a IT, senát 23", „6 oblastí".
function popisVyberu(key) {
  const v = vyber || vychoziVyber();
  if (key === "journals") {
    const n = CASOPISY.length - v.skryte_casopisy.length;
    return n + " z " + CASOPISY.length + " časopisů";
  }
  const casti = [];
  const soud = SOUD_ZDROJE[key] || "ns";
  const obl = popisOblasti(v[soud].oblasti);
  if (obl) casti.push(obl);
  if (key === "nsoud" || key === "today") {
    const sen = souhrnSenatu(v);
    if (sen !== "žádný") casti.push((v.ns.senaty.length > 1 ? "senáty " : "senát ") + sen);
  }
  return casti.join(", ");
}

function vykresliFiltr(key, nVyb, nVse) {
  const el = document.getElementById("filtr-" + key);
  if (!el) return;
  // Časopisy bez skrytých: výběr nic nefiltruje, lišta by nic neřekla.
  if (nVyb == null || (key === "journals" && nVyb === nVse)) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  const r = rezim(key);
  const tl = (hodnota, text, n) => '<button type="button" data-rezim="' + hodnota + '" data-zdroj="' + key +
    '" aria-pressed="' + (r === hodnota) + '">' + text + ' <span class="pocet">' + n + "</span></button>";
  let odkaz = "";
  if (clerkStav === "pripraven") {
    odkaz = prihlaseny ? '<a href="#nastaveni">Upravit</a>'
      : '<a href="#nastaveni">Přihlaste se a nastavte si vlastní</a>';
  }
  const popis = popisVyberu(key);
  el.innerHTML = '<div class="prepinac" role="group" aria-label="Co ukázat">' +
    tl("vyber", prihlaseny ? "Můj výběr" : "Výchozí výběr", nVyb) + tl("vse", "Vše", nVse) + "</div>" +
    (popis ? '<span class="filtr-popis">' + esc(popis) + "</span>" : "") +
    (odkaz ? '<span class="filtr-odkaz">' + odkaz + "</span>" : "");
  el.hidden = false;
}

// Prázdná tabulka řekne proč: za dobu okna nic nepřibylo, nebo přibylo,
// ale do výběru nic nespadá – a nabídne to ukázat.
function prazdnoHtml(f, nVse) {
  const za = f.oknoDni ? "za posledních " + tvar(f.oknoDni, "den", "dny", "dní") : "za tu dobu";
  if (!nVse) return "Nic nového " + za + ".";
  return "Do výběru nic nespadá (celkem " + za + ": " + nVse + "). " +
    '<button type="button" class="odkaz-tl" data-rezim="vse" data-zdroj="' + f.key + '">Zobrazit vše</button>';
}

/* ========== Dvoutýdenní přehled (digest.json) ========== */

// Klíč zdroje pouštíme do class jen z uzavřeného seznamu – ať se přes data
// z JSONu nedá do stránky propašovat cizí třída.
const SRC_KEYS = FEEDS.map(f => f.key);

function digestSource(s) {
  const key = SRC_KEYS.indexOf(s.src) >= 0 ? s.src : "";
  const badge = '<span class="src' + (key ? " src-" + key : "") + '">' + esc(s.label || "") + "</span>";
  // U článků ještě zkratka časopisu ([JIPLP], [IIC], …), u CJEU typ řízení.
  const inner = badge + tagBadge(s.tag) + "<span>" + esc(s.title || "") + "</span>";
  const href = safeHref(s.link || "");
  return href
    ? '<a class="digest-source" href="' + href + '">' + inner + "</a>"
    : '<span class="digest-source">' + inner + "</span>";
}

function renderDigest(data) {
  const container = document.getElementById("feed-digest");
  // Přehled se na rozdíl od feedů generuje jen jednou týdně, takže datum
  // poslední aktualizace patří k němu – to v hlavičce stránky je z feedů.
  const stamp = document.getElementById("digest-updated");
  if (stamp && data && data.generated) {
    stamp.textContent = "aktualizováno " + czDate(data.generated);
  }
  if (!data || !Array.isArray(data.blocks) || data.blocks.length === 0) {
    container.innerHTML = '<p class="feed-empty">Přehled zatím není k dispozici.</p>';
    return;
  }
  let html = "";
  if (data.intro) html += '<p class="digest-intro">' + esc(data.intro) + "</p>";
  data.blocks.forEach(b => {
    html += '<div class="digest-block">';
    html += '<h3 class="digest-title">' + esc(b.title || "") + "</h3>";
    html += '<p class="digest-text">' + esc(b.text || "") + "</p>";
    const sources = Array.isArray(b.sources) ? b.sources : [];
    if (sources.length) {
      html += '<div class="digest-sources">' + sources.map(digestSource).join("") + "</div>";
    }
    html += "</div>";
  });

  const from = czDate(data.from), to = czDate(data.to);
  const meta = [];
  if (data.total) meta.push("vybráno z " + data.total + " položek");
  if (from && to) meta.push("období " + from + " – " + to);
  if (meta.length) {
    html += '<p class="digest-meta">' + esc(meta.join(" · ")) + "</p>";
  }
  container.innerHTML = html;
}

/* ========== Kalendář jednání (hearings.json) ========== */
// Jednání MS a VS Praha v agendě duševního vlastnictví. V mřížce je štítek
// se jménem sporu; detail se otevře jako bublina u toho štítku, ať kalendář
// pod sebou nemění výšku a obsah stránky nepodskakuje.
const CAL_DOWS = ["Po", "Út", "St", "Čt", "Pá", "So", "Ne"];
const COURT_LABELS = { MS: "MS Praha", VS: "VS Praha" };
// Kalendář neukazuje měsíc, ale okno tří týdnů, které začíná tímhle
// týdnem: minulé týdny už si nikdo nevypisuje a soudy stejně vypisují
// jednání jen zhruba na dva týdny dopředu. Šipky posouvají o týden,
// takže do minulosti se dá dojít, když je potřeba.
const CAL_TYDNU = 3;
const CAL_POSUN_DNU = 7;

let calData = null;      // obsah hearings.json
let calStart = null;     // pondělí prvního zobrazeného týdne (Date)
let calPopKey = null;    // co je rozkliknuté: "datum#idx" (idx -1 = celý den)

// ISO datum z lokálního Date – toISOString by přes UTC ukrajovalo den.
function isoOf(d) {
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") +
    "-" + String(d.getDate()).padStart(2, "0");
}

// Pondělí týdne, do kterého datum spadá.
function mondayOf(d) {
  const p = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  p.setDate(p.getDate() - ((p.getDay() + 6) % 7));
  return p;
}

// Výchozí okno: začíná pondělím tohoto týdne.
function calDefaultStart() {
  return mondayOf(new Date());
}

// „17. 8. – 13. 9. 2026"; rok u začátku jen tehdy, když se okno láme přes něj.
function calRangeLabel(od, do_) {
  const den = d => d.getDate() + ". " + (d.getMonth() + 1) + ".";
  const zacatek = den(od) + (od.getFullYear() === do_.getFullYear()
    ? "" : " " + od.getFullYear());
  return zacatek + " – " + den(do_) + " " + do_.getFullYear();
}

function calFilters() {
  const on = id => {
    const el = document.getElementById(id);
    return !el || el.getAttribute("aria-pressed") !== "false";
  };
  return { MS: on("flt-ms"), VS: on("flt-vs") };
}

// „9:30" vs „10:00" se řetězcově řadí špatně (dokumenty píšou i jednocifernou
// hodinu), proto porovnáváme minuty od půlnoci; jednání bez času jdou na konec.
function minutesOf(hodina) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(hodina || ""));
  return m ? Number(m[1]) * 60 + Number(m[2]) : 24 * 60 + 1;
}

// Jednání po aplikaci filtrů, seskupená podle ISO data.
function calEventsByDay() {
  const f = calFilters();
  const byDay = {};
  (calData.jednani || []).forEach(j => {
    if (!j || !j.datum || !f[j.soud] || !j.ip) return;
    (byDay[j.datum] = byDay[j.datum] || []).push(j);
  });
  Object.values(byDay).forEach(list => list.sort(
    (a, b) => minutesOf(a.hodina) - minutesOf(b.hodina) ||
      String(a.spz || "").localeCompare(String(b.spz || ""), "cs")));
  return byDay;
}

// Jméno sporu („OSA v. BH Drink") počítá scraper; starší data ho nemají,
// tak padáme zpátky na spisovou značku.
function caseName(j) {
  return j.nazev || j.spz || "Jednání";
}

function infosoudUrl(j) {
  const court = (calData.courts || {})[j.soud] || {};
  // Rejstřík musí jít malými písmeny (druhVeci=co), jinak InfoSoud řízení
  // nenajde.
  return "https://infosoud.gov.cz/InfoSoud/detail-rizeni?typOrganizace=VSECHNY_KRAJE" +
    "&druhOrganizace=" + encodeURIComponent(court.infosoud_org || "") +
    "&cisloSenatu=" + encodeURIComponent(j.cislo_senatu) +
    "&druhVeci=" + encodeURIComponent(String(j.rejstrik || "").toLowerCase()) +
    "&bcVec=" + encodeURIComponent(j.bc) +
    "&rocnik=" + encodeURIComponent(j.rocnik);
}

function courtBadge(j) {
  const key = j.soud === "MS" ? "ms" : "vs";
  return '<span class="src src-' + key + '">' + esc(COURT_LABELS[j.soud] || j.soud) + "</span>";
}

// V mřížce i v mobilním seznamu je vidět čas a jméno sporu; spisová
// značka až po rozkliknutí.
function chipHtml(j, iso, idx) {
  const chip = j.soud === "MS" ? "cal-chip-ms" : "cal-chip-vs";
  return '<button type="button" class="cal-chip ' + chip +
    '" data-date="' + iso + '" data-idx="' + idx + '" aria-expanded="false" title="' +
    esc(caseName(j) + " · " + (j.spz || "")) + '">' +
    '<span class="cal-dot"></span>' +
    (j.hodina ? '<span class="cal-chip-time">' + esc(j.hodina) + "</span>" : "") +
    '<span class="cal-chip-name">' + esc(caseName(j)) + "</span></button>";
}

// Na telefonu se mřížka tří týdnů nedá číst – vedle ní se proto vykreslí
// seznam dnů, ve kterých něco je, a CSS podle šířky ukáže jedno z toho.
// Obojí se kreslí ze stejných dat, takže bublina funguje v obou.
function renderCalAgenda(byDay, start, konec) {
  const odIso = isoOf(start), doIso = isoOf(konec);
  const vOkne = Object.keys(byDay).sort().filter(d => d >= odIso && d <= doIso);
  if (!vOkne.length) {
    return '<p class="feed-empty">V tomhle období nejsou žádná jednání.</p>';
  }
  let html = "";
  vOkne.forEach(iso => {
    const d = new Date(iso);
    // Rok je v nadpisu okna nad seznamem, u každého dne by jen překážel.
    const nadpis = CAL_DOWS[(d.getDay() + 6) % 7] + " " +
      czDate(iso).replace(/ \d{4}$/, "");
    html += '<div class="cal-agenda-day"><div class="cal-agenda-date">' +
      esc(nadpis) + "</div>" +
      byDay[iso].map((j, idx) => chipHtml(j, iso, idx)).join("") + "</div>";
  });
  return html;
}

function renderCalGrid() {
  const byDay = calEventsByDay();
  const start = new Date(calStart);
  const todayIso = isoOf(new Date());

  // Víkendy soudy nezasedají, tak se v mřížce jen pletou do cesty – bez
  // nich je na pracovní dny víc místa. CAL_DOWS zůstává celý (používá ho
  // i agenda níž), tady se vezme jen pracovní část.
  let html = CAL_DOWS.slice(0, 5).map(d => '<div class="cal-dow">' + d + "</div>").join("");
  for (let i = 0; i < CAL_TYDNU * 7; i++) {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    if (d.getDay() === 0 || d.getDay() === 6) continue;
    const iso = isoOf(d);
    const events = byDay[iso] || [];
    const cls = ["cal-day"];
    if (iso === todayIso) cls.push("today");
    else if (iso < todayIso) cls.push("past");
    html += '<div class="' + cls.join(" ") + '">';
    // Okno přesahuje přes měsíce, tak u prvního dne (a u prvního v měsíci)
    // patří k číslu i měsíc – jinak by „1" nešlo zařadit.
    const cislo = (i === 0 || d.getDate() === 1)
      ? d.getDate() + ". " + (d.getMonth() + 1) + "." : d.getDate();
    html += '<span class="cal-daynum">' + cislo + "</span>";
    events.forEach((j, idx) => {
      html += chipHtml(j, iso, idx);
    });
    html += "</div>";
  }
  const konec = new Date(start);
  konec.setDate(start.getDate() + CAL_TYDNU * 7 - 1);
  document.getElementById("cal-grid").innerHTML = html;
  document.getElementById("cal-agenda").innerHTML = renderCalAgenda(byDay, start, konec);
  document.getElementById("cal-title").textContent = calRangeLabel(start, konec);
}

// Co se s jednáním stalo od minulého přehledu. Scraper porovnává každý
// nový přehled s tím, co už má, a změny i s popisem ukládá do hearings.json.
function zmenaText(z) {
  const popis = z.popis || z.typ;
  if (z.typ === "presun") return popis + " z " + czDate(z.z);
  if (z.typ === "cas" || z.typ === "sin") return popis + " (dřív " + z.z + ")";
  return popis;
}

function zmenyJednani(j) {
  return (calData.zmeny || []).filter(z =>
    z.soud === j.soud && z.spz === j.spz && z.datum === j.datum);
}

/* ========== Jedno jednání jako .ics ========== */
// Celý kalendář se dá přihlásit z hearings.ics; tohle je pro jedno jednání,
// které si člověk chce prostě jen jednou hodit do svého kalendáře. Událost
// se vyřízne z hotového hearings.ics podle uid (scraper ho ukládá i do
// hearings.json), takže stažená událost je do písmene ta, kterou má
// přihlášený kalendář – a nic se tu neskládá podruhé.
let icsPromise = null;

function fetchIcs() {
  if (!icsPromise) {
    icsPromise = fetch("hearings.ics").then(r => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.text();
    });
  }
  return icsPromise;
}

// Vlastnosti celého kalendáře (jméno, popis, interval obnovy) do jedné
// události nepatří – klient by si podle nich založil nový kalendář.
const ICS_DROP_RE = /^(X-WR-CALNAME|X-WR-CALDESC|REFRESH-INTERVAL|X-PUBLISHED-TTL)/;

function icsForEvent(j) {
  return fetchIcs().then(ics => {
    const lines = ics.split(/\r?\n/);
    const uidAt = lines.findIndex(l => l.startsWith("UID:" + j.uid + "@"));
    if (uidAt < 0) throw new Error("jednání v hearings.ics není");
    let od = uidAt, do_ = uidAt;
    while (od > 0 && lines[od] !== "BEGIN:VEVENT") od--;
    while (do_ < lines.length && lines[do_] !== "END:VEVENT") do_++;

    // Hlavička = vše před první událostí, bez vlastností celého kalendáře
    // (i s jejich zalomenými pokračováními, která začínají mezerou).
    const head = [];
    let drop = false;
    for (const line of lines.slice(0, lines.indexOf("BEGIN:VEVENT"))) {
      if (line.startsWith(" ")) { if (!drop) head.push(line); continue; }
      drop = ICS_DROP_RE.test(line);
      if (!drop) head.push(line);
    }
    return head.concat(lines.slice(od, do_ + 1), ["END:VCALENDAR"]).join("\r\n") + "\r\n";
  });
}

// „jednani-3-cmo-25-2026-2026-08-17.ics" – bez diakritiky a mezer, ať se
// jméno souboru přenese i tam, kde na ně nejsou zvyklí.
function icsFileName(j) {
  const zaklad = (j.spz || caseName(j)) + " " + (j.datum || "");
  return "jednani-" + zaklad.normalize("NFD").replace(/[\u0300-\u036f]/g, "")
    .replace(/[^0-9A-Za-z]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase() + ".ics";
}

function stahniIcs(j) {
  icsForEvent(j).then(text => {
    const url = URL.createObjectURL(
      new Blob([text], { type: "text/calendar;charset=utf-8" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = icsFileName(j);
    // Kliknutí na pomocný odkaz nesmí probublat na dokument – tam ho čeká
    // posluchač, který zavírá bublinu s detailem, a ta by po stažení zmizela.
    a.addEventListener("click", e => e.stopPropagation());
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Odkaz na blob se ruší až po kliknutí, jinak by stahování nedoběhlo.
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }).catch(e => {
    console.error("Stažení .ics selhalo:", e);
    // Když jednotlivá událost nejde vyříznout, ať člověk dostane aspoň celý kalendář.
    window.open("hearings.ics", "_blank");
  });
}

// Jméno úseku z metadat soudu (scraper ho ukládá vedle období a zdroje
// přehledu). Civilní úsek je většina kalendáře a nepopisuje se; správní
// soudnictví se ukáže, ať je jasné, proč je v IP kalendáři senát „15 A"
// – je to žaloba proti Úřadu průmyslového vlastnictví.
function usekNazev(j) {
  if (!j.usek || j.usek === "civilni") return "";
  const court = (calData.courts || {})[j.soud] || {};
  const meta = (court.useky || {})[j.usek] || {};
  return meta.nazev || j.usek;
}

// Detail jednání do bubliny. `nadpis` je jen u celodenního výpisu –
// u jednoho jednání by nad jeho jménem jen zabíral místo.
// `datum` a `zaklad` (pořadí prvního jednání v tom dni) nese tlačítko na
// stažení .ics – podle nich si ho posluchač v bublině zase najde.
function calPopHtml(events, nadpis, datum, zaklad) {
  let html = '<span class="cal-pop-arrow"></span>' +
    '<button type="button" class="cal-pop-close" aria-label="Zavřít">✕</button>' +
    '<div class="cal-pop-body">';
  if (nadpis) html += '<h3 class="cal-detail-title">' + esc(nadpis) + "</h3>";
  events.forEach((j, poradi) => {
    // Oddělovač „v." zvýrazníme, ať se neztratí v názvech typu
    // „Hudební divadlo v Karlíně v. Jiří Strach".
    const name = esc(caseName(j)).replace(
      / v\. /, ' <span class="cal-event-vs">v.</span> ');
    html += '<div class="cal-event">';
    html += '<div class="cal-event-head">' +
      '<span class="cal-event-time">' + esc(j.hodina || "–") + "</span>" +
      courtBadge(j) +
      '<span class="cal-event-name">' + name + "</span>" +
      "</div>";

    const rows = [];
    if (j.spz) rows.push(["Spisová značka", esc(j.spz)]);
    if (j.predseda) rows.push(["Předseda senátu", esc(j.predseda)]);
    if (j.senat) rows.push(["Senát", esc(j.senat)]);
    const usek = usekNazev(j);
    if (usek) rows.push(["Úsek", esc(usek)]);
    if (j.sin) rows.push(["Jednací síň", esc(j.sin)]);
    const parties = (j.ucastnici || []).filter(Boolean);
    if (parties.length) rows.push(["Účastníci", parties.map(esc).join("<br>")]);
    html += '<dl class="cal-event-rows">' +
      rows.map(r => "<dt>" + r[0] + "</dt><dd>" + r[1] + "</dd>").join("") + "</dl>";

    const zmeny = zmenyJednani(j);
    if (zmeny.length) {
      html += '<div class="cal-event-links">' + zmeny.map(z =>
        '<span class="cal-zmena">' + esc(zmenaText(z)) + "</span>").join("") + "</div>";
    }
    html += '<div class="cal-event-links">' +
      '<a href="' + esc(infosoudUrl(j)) + '" target="_blank" rel="noopener">' +
      "Otevřít na InfoSoudu ↗</a>";
    // Bez uid (starší data) není co vyříznout – tlačítko se nenabídne.
    if (j.uid) {
      html += '<button type="button" class="cal-ics" data-date="' + esc(datum) +
        '" data-idx="' + (zaklad + poradi) + '" title="Uložit tohle jednání ' +
        'jako událost do vlastního kalendáře">Stáhnout .ics</button>';
    }
    html += "</div></div>";
  });
  return html + "</div>";
}

function closeCalPop() {
  calPopKey = null;
  const box = document.getElementById("cal-pop");
  if (box) { box.hidden = true; box.innerHTML = ""; }
  document.querySelectorAll(".cal-chip.is-open, .cal-more.is-open").forEach(el => {
    el.classList.remove("is-open");
    el.setAttribute("aria-expanded", "false");
  });
}

// Bublinu posadíme na tu stranu štítku, kde je v okně víc místa, a obsah
// omezíme tak, aby se celá vešla na obrazovku. Šipka míří na štítek.
function placeCalPop(anchor) {
  const box = document.getElementById("cal-pop");
  const telo = box.querySelector(".cal-pop-body");
  const s = document.getElementById("cal-shell").getBoundingClientRect();
  const a = anchor.getBoundingClientRect();
  const okno = document.documentElement.clientHeight;
  const podStitkem = okno - a.bottom - 16;
  const nadStitkem = a.top - 16;
  const dolu = podStitkem >= nadStitkem;
  box.style.left = "0px";
  box.style.top = "0px";
  if (telo) {
    telo.style.maxHeight = "";
    const ramecek = box.offsetHeight - telo.offsetHeight;   // odsazení + linka
    telo.style.maxHeight =
      Math.max(140, (dolu ? podStitkem : nadStitkem) - ramecek) + "px";
  }
  const w = box.offsetWidth, h = box.offsetHeight;
  const stred = a.left + a.width / 2 - s.left;
  const left = Math.max(6, Math.min(stred - w / 2, s.width - w - 6));
  box.style.left = left + "px";
  box.style.top = (dolu ? a.bottom - s.top + 8 : a.top - s.top - h - 8) + "px";
  const arrow = box.querySelector(".cal-pop-arrow");
  if (arrow) {
    arrow.style.left = Math.max(8, Math.min(stred - left - 5, w - 24)) + "px";
    arrow.classList.toggle("is-below", !dolu);
  }
}

function openCalPop(anchor) {
  const datum = anchor.dataset.date;
  const idx = Number(anchor.dataset.idx);
  const den = calEventsByDay()[datum] || [];
  const events = idx >= 0 ? [den[idx]].filter(Boolean) : den;
  if (!events.length) return;
  closeCalPop();
  const box = document.getElementById("cal-pop");
  box.innerHTML = calPopHtml(events, idx < 0 ? "Jednání " + czDate(datum) : "",
    datum, idx < 0 ? 0 : idx);
  box.hidden = false;
  placeCalPop(anchor);
  anchor.classList.add("is-open");
  anchor.setAttribute("aria-expanded", "true");
  calPopKey = datum + "#" + idx;
}

function redrawCal() {
  // Překreslením zmizí štítek, ke kterému byla bublina přišpendlená.
  closeCalPop();
  renderCalGrid();
}

function calShiftDays(delta) {
  const p = new Date(calStart);
  p.setDate(p.getDate() + delta);
  calStart = p;
  redrawCal();
}

function renderKalendar(data) {
  const container = document.getElementById("feed-kalendar");
  if (!data || !Array.isArray(data.jednani)) {
    container.innerHTML = '<p class="feed-empty">Kalendář jednání zatím není k dispozici.</p>';
    return;
  }
  calData = data;
  calStart = calDefaultStart();

  container.innerHTML =
    '<div class="cal-toolbar">' +
      '<div class="cal-nav">' +
        '<button type="button" id="cal-prev" aria-label="O týden zpět">‹</button>' +
        '<span class="cal-title" id="cal-title"></span>' +
        '<button type="button" id="cal-next" aria-label="O týden vpřed">›</button>' +
        '<button type="button" id="cal-today">Dnes</button>' +
      "</div>" +
      '<div class="cal-filters">' +
        // Klikací barevné štítky – legenda mřížky a filtr v jednom.
        '<button type="button" class="cal-key cal-key-ms" id="flt-ms" ' +
          'aria-pressed="true" title="Skrýt nebo zobrazit jednání Městského ' +
          'soudu v Praze">MS Praha</button>' +
        '<button type="button" class="cal-key cal-key-vs" id="flt-vs" ' +
          'aria-pressed="true" title="Skrýt nebo zobrazit jednání Vrchního ' +
          'soudu v Praze">VS Praha</button>' +
      "</div>" +
    "</div>" +
    '<div class="cal-shell" id="cal-shell">' +
      '<div class="cal-wrap"><div class="cal-grid" id="cal-grid"></div></div>' +
      '<div class="cal-agenda" id="cal-agenda"></div>' +
      '<div class="cal-pop" id="cal-pop" role="dialog" aria-label="Detail jednání" hidden></div>' +
    "</div>";

  const info = document.getElementById("cal-info");
  if (info && data.generated) {
    const kdy = czDate(data.generated);
    if (kdy) info.textContent = "aktualizováno " + kdy;
  }

  document.getElementById("cal-prev")
    .addEventListener("click", () => calShiftDays(-CAL_POSUN_DNU));
  document.getElementById("cal-next")
    .addEventListener("click", () => calShiftDays(CAL_POSUN_DNU));
  document.getElementById("cal-today").addEventListener("click", () => {
    calStart = calDefaultStart();
    redrawCal();
  });
  ["flt-ms", "flt-vs"].forEach(id => {
    const el = document.getElementById(id);
    el.addEventListener("click", () => {
      el.setAttribute("aria-pressed",
        el.getAttribute("aria-pressed") === "false" ? "true" : "false");
      redrawCal();
    });
  });
  // Posluchač je na celé skořápce, ať obslouží mřížku i mobilní seznam.
  document.getElementById("cal-shell").addEventListener("click", e => {
    const btn = e.target.closest(".cal-chip, .cal-more");
    if (!btn) return;
    const key = btn.dataset.date + "#" + btn.dataset.idx;
    // Druhý klik na týž štítek bublinu zavře.
    if (calPopKey === key) closeCalPop();
    else openCalPop(btn);
  });
  document.getElementById("cal-pop").addEventListener("click", e => {
    if (e.target.closest(".cal-pop-close")) { closeCalPop(); return; }
    const ics = e.target.closest(".cal-ics");
    if (!ics) return;
    const den = calEventsByDay()[ics.dataset.date] || [];
    const j = den[Number(ics.dataset.idx)];
    if (j) stahniIcs(j);
  });
  // Klik jinam a Esc bublinu zavřou; při změně velikosti okna by šipka
  // ukazovala mimo, tak ji taky zavřeme.
  document.addEventListener("click", e => {
    if (calPopKey && !e.target.closest("#cal-pop, .cal-chip, .cal-more")) closeCalPop();
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && calPopKey) closeCalPop();
  });
  window.addEventListener("resize", () => { if (calPopKey) closeCalPop(); });

  redrawCal();
}

/* ========== Popisy karet a sbalování přehledu ========== */
// Popis karty se ukazuje při najetí myší (čistě v CSS). Klik ho připne –
// na dotykových zařízeních žádné najetí není a na delší text se hodí čas.
function closeHelp(krome) {
  document.querySelectorAll(".help[aria-expanded='true']").forEach(b => {
    if (b === krome) return;
    b.setAttribute("aria-expanded", "false");
    const d = b.parentElement.querySelector(".card-desc");
    if (d) d.hidden = true;
  });
}

function initHelp() {
  document.addEventListener("click", e => {
    const btn = e.target.closest(".help");
    if (!btn) { closeHelp(null); return; }
    const desc = btn.parentElement.querySelector(".card-desc");
    if (!desc) return;
    closeHelp(btn);
    desc.hidden = !desc.hidden;
    btn.setAttribute("aria-expanded", String(!desc.hidden));
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") closeHelp(null);
  });
}

/* ========== Přihlášení (Clerk) ========== */
// Clerk se načítá až po vykreslení obsahu; web na něj nečeká a při jeho
// výpadku (nebo zablokovaném skriptu) běží dál ve výchozím výběru.
// Publishable key je veřejný a patří do stránky – podle hostitele se volí
// instance. Tajný klíč sem nikdy nepatří (je jen v GitHub secrets).
const CLERK_KLICE = {
  "owl.davidzavada.cz": "pk_live_Y2xlcmsub3dsLmRhdmlkemF2YWRhLmN6JA",  // produkce
  "*": "pk_test_cHJvdmVuLWpheS0zOTI5LmNsZXJrLmFjY291bnRzLmRldiQ"        // náhledy, localhost
};
// Clerk JS (v6) a jeho komponenty (@clerk/ui) z Frontend API instance.
const CLERK_JS = "@clerk/clerk-js@6/dist/clerk.browser.js";
const CLERK_UI = "@clerk/ui@1/dist/ui.browser.js";

let clerk = null;              // načtený Clerk
let clerkStav = "nacitam";     // nacitam | pripraven | nedostupny
let prihlaseny = null;         // id přihlášeného uživatele

function clerkKlic() {
  return CLERK_KLICE[location.hostname] || CLERK_KLICE["*"];
}

// Frontend API instance je v klíči: pk_live_/pk_test_<base64("host$")>.
function clerkFrontendApi(klic) {
  try {
    const host = atob(String(klic).split("_").slice(2).join("_")).replace(/\$$/, "");
    return /^[a-z0-9.-]+$/i.test(host) ? host : "";
  } catch (e) {
    return "";
  }
}

function nactiSkript(src, atributy) {
  return new Promise((resolve, reject) => {
    const el = document.createElement("script");
    Object.keys(atributy || {}).forEach(k => el.setAttribute(k, atributy[k]));
    el.src = src;
    el.async = true;
    el.onload = resolve;
    el.onerror = () => reject(new Error("Nenačetl se " + src));
    document.head.appendChild(el);
  });
}

function initClerk() {
  const klic = clerkKlic();
  const host = clerkFrontendApi(klic);
  if (!host) {
    clerkNedostupny(new Error("neplatný klíč Clerku"));
    return;
  }
  const cdn = "https://" + host + "/npm/";
  Promise.all([
    nactiSkript("vendor/clerk-cs-CZ.js"),
    nactiSkript(cdn + CLERK_UI, { crossorigin: "anonymous" }),
    nactiSkript(cdn + CLERK_JS, { crossorigin: "anonymous", "data-clerk-publishable-key": klic })
  ])
    .then(() => window.Clerk.load({
      ui: { ClerkUI: window.__internal_ClerkUICtor },
      localization: window.clerkCsCZ
    }))
    .then(() => {
      clerk = window.Clerk;
      clerkStav = "pripraven";
      clerk.addListener(({ user }) => zmenaUctu(user));
      zmenaUctu(clerk.user, true);
      // Stránka otevřená s kotvou #nastaveni: dialog, jakmile je znám účet.
      if (vyberZKotvy) {
        vyberZKotvy = false;
        if (prihlaseny) otevriVyber();
      }
    })
    .catch(clerkNedostupny);
}

function clerkNedostupny(e) {
  console.warn("Přihlášení není k dispozici:", e);
  clerkStav = "nedostupny";
  vykresliUcet();
  vykresliNastaveni();
}

// Výběr se přepíná jen při přihlášení a odhlášení (jiný účet). Změny
// výběru dělá tahle stránka sama, takže pozdější ozvěny od Clerku po
// uložení nic nepřepisují. `vzdy` = první stav po načtení Clerku.
function zmenaUctu(user, vzdy) {
  const id = user ? user.id : null;
  if (vzdy || id !== prihlaseny) {
    prihlaseny = id;
    vyber = user ? normalizujVyber((user.unsafeMetadata || {}).owl) : vychoziVyber();
    // Nový účet začíná se sbalenými sekcemi a bez rozpracovaných změn.
    otevreneSekce = new Set();
    koncept = null;
    stavUlozeni = "";
    const dialog = document.getElementById("vyber-dialog");
    if (dialog && dialog.open) dialog.close();
    vykresliZdroje();
    vykresliNastaveni();
    if (user) uvitatNovy(user);
  }
  vykresliUcet();
}

// Po registraci se Můj výběr otevře sám – jen u čerstvě založeného účtu bez
// uloženého výběru a jen jednou (příznak v localStorage). Průvodce není
// potřeba: stačí okno s výchozím výběrem a jednou větou nahoře.
const UVITANI_MIN = 15;

function uvitatNovy(user) {
  if ((user.unsafeMetadata || {}).owl) return;
  const zalozen = user.createdAt ? new Date(user.createdAt).getTime() : NaN;
  if (!(Date.now() - zalozen < UVITANI_MIN * 60 * 1000)) return;
  const klic = "owl:uvitani:" + user.id;
  try {
    if (localStorage.getItem(klic)) return;
    localStorage.setItem(klic, "1");
  } catch (e) { /* bez úložiště se okno otevře, jen si to nezapamatuje */ }
  uvitani = true;
  otevreneSekce.add("oblasti");
  otevriVyber();
}

function prihlasit(registrace) {
  if (!clerk) return;
  const zpet = { fallbackRedirectUrl: location.href };
  if (registrace) clerk.openSignUp(zpet);
  else clerk.openSignIn(zpet);
}

// Hlavička: nepřihlášený má „Přihlásit se" a „Registrace", přihlášený
// odkaz na svůj výběr a tlačítko účtu od Clerku.
function vykresliUcet() {
  const el = document.getElementById("ucet");
  if (!el) return;
  if (clerkStav !== "pripraven") {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  const tlacitko = el.querySelector(".ucet-user");
  if (prihlaseny) {
    if (tlacitko) return;
    el.innerHTML = '<span class="ucet-user"></span>';
    // Můj výběr je první položka nabídky účtu (před správou účtu a odhlášením).
    clerk.mountUserButton(el.querySelector(".ucet-user"), {
      customMenuItems: [
        { label: "Můj výběr", onClick: otevriVyber,
          mountIcon: node => { node.innerHTML = '<svg class="nav-ico ucet-menu-ico" aria-hidden="true">' +
            '<use href="#icon-sliders"></use></svg>'; },
          unmountIcon: node => { if (node) node.innerHTML = ""; } },
        { label: "manageAccount" },
        { label: "signOut" }
      ]
    });
  } else if (!el.querySelector(".ucet-prihlasit")) {
    if (tlacitko) clerk.unmountUserButton(tlacitko);
    el.innerHTML = '<button type="button" class="ucet-btn ucet-prihlasit">Přihlásit se</button>' +
      '<button type="button" class="ucet-btn ucet-btn-hlavni ucet-registrace">Registrace</button>';
  }
}

/* ========== Nastavení výběru (#nastaveni) ========== */

const KOLEGIA = { civilni: "Občanskoprávní a obchodní kolegium", trestni: "Trestní kolegium" };

// Senáty NS k výběru: ze seznamu a navíc ty, které jsou v datech nebo ve
// výběru a v seznamu chybí (aspoň s číslem).
function senatyKVyberu(v) {
  const znam = SENATY_NS.map(s => s.senat);
  const r = zdrojeVysledky && zdrojeVysledky[FEEDS.findIndex(f => f.key === "nsoud")];
  const vDatech = r && r.status === "fulfilled" ? r.value.map(i => i.senat) : [];
  const navic = bezDuplicit(vDatech.concat((v || vyber).ns.senaty))
    .filter(n => Number.isInteger(n) && znam.indexOf(n) < 0)
    .sort((a, b) => a - b)
    .map(n => ({ senat: n, kolegium: "ostatni", popis: "" }));
  return SENATY_NS.concat(navic);
}

// Registr časopisů z data/casopisy.json: štítky v nastavení a seznam
// v nápovědě karty.
function nastavCasopisy(seznam) {
  CASOPISY = seznam.filter(c => c && c.id && c.zkratka);
  const ul = document.getElementById("casopisy-seznam");
  if (ul) {
    ul.innerHTML = CASOPISY.map(c => "<li>" + esc(c.zkratka === c.nazev ? c.nazev : c.zkratka + " – " + c.nazev) +
      (c.vydavatel ? ' <span class="legend">(' + esc(c.vydavatel) + ")</span>" : "") + "</li>").join("");
  }
}

// Časopisy k výběru: všechny z registru (id, zkratka, název).
function casopisyKVyberu() {
  return CASOPISY.slice();
}

// Stránka výběru. Sekce jsou rozbalovací řádky (shadcn Accordion), na
// začátku sbalené; v hlavičce každé je souhrn toho, co je vybrané. Vybírá
// se jedním kliknutím na čip (tlačítko s aria-pressed). Změny se sbírají
// v konceptu a platí – na webu i v účtu – až po „Uložit".
let otevreneSekce = new Set();
let uvitani = false;              // dialog otevřený po registraci – nahoře uvítací věta
let otevrenoZ = null;             // prvek, na který se po zavření vrátí fokus
let koncept = null;               // rozpracovaný výběr; null = stejný jako uložený
let stavUlozeni = "";             // „Ukládám…", „Uloženo", chyba
let ukladam = false;

function kopie(v) {
  return JSON.parse(JSON.stringify(v));
}

function rozpracovano() {
  return !!koncept && JSON.stringify(koncept) !== JSON.stringify(vyber);
}

// Čip má vždy stejně široké místo na znak (+ / ✓ / –), takže se po
// kliknutí nezmění jeho velikost.
function cipHtml(atributy, stav, obsah, title, trida) {
  const attrs = Object.keys(atributy).map(k => " " + k + '="' + esc(atributy[k]) + '"').join("");
  return '<button type="button" class="cip' + (trida ? " " + trida : "") + '"' + attrs +
    ' aria-pressed="' + stav + '"' + (title ? ' title="' + esc(title) + '"' : "") +
    '><span class="cip-znak" aria-hidden="true"></span>' + obsah + "</button>";
}

function skupinyOblasti() {
  const skupiny = [];
  OBLASTI_SEZNAM.forEach(o => {
    let sk = skupiny.find(x => x.nazev === o.skupina);
    if (!sk) skupiny.push(sk = { nazev: o.skupina, oblasti: [] });
    sk.oblasti.push(o);
  });
  return skupiny;
}

function soudyOblasti(v, id) {
  return SOUDY_VYBERU.filter(s => v[s.soud].oblasti.indexOf(id) >= 0);
}

// Oblast se vybírá pro všechny soudy najednou. „mixed" zbyde jen po
// dřívějším výběru zvlášť pro jednotlivé soudy.
function stavOblasti(v, id) {
  const n = soudyOblasti(v, id).length;
  return n === SOUDY_VYBERU.length ? "true" : n ? "mixed" : "false";
}

function oblastiHtml(v) {
  return skupinyOblasti().map((sk, i) => {
    const vybrano = sk.oblasti.filter(o => stavOblasti(v, o.id) === "true").length;
    const vse = vybrano === sk.oblasti.length;
    return '<div class="vyber-skupina"><div class="skupina-hlava"><span class="skupina-nazev">' +
      esc(sk.nazev) + '</span><span class="skupina-pocet">' + vybrano + "/" + sk.oblasti.length + "</span>" +
      '<button type="button" class="skupina-vse" data-skupina="' + i + '">' +
      (vse ? "Zrušit vše" : "Vybrat vše") + "</button></div>" + '<div class="cipy">' +
      sk.oblasti.map(o => {
        const stav = stavOblasti(v, o.id);
        const title = stav === "mixed"
          ? "Vybráno jen u některých soudů (" + soudyOblasti(v, o.id).map(s => s.zkratka).join(", ") +
            "), kliknutím u všech"
          : o.popis;
        return cipHtml({ "data-oblast": o.id }, stav, esc(o.nazev), title);
      }).join("") + "</div></div>";
  }).join("") + '<p class="vyber-poznamka">Platí pro všechny soudy. Rozhodnutí se ukáže, když ho AI ' +
    "zařadí do některé z vybraných oblastí.</p>";
}

function tvar(n, jeden, dva, pet) {
  return n + " " + (n === 1 ? jeden : n >= 2 && n <= 4 ? dva : pet);
}

function souhrnOblasti(v) {
  const vse = OBLASTI_SEZNAM.filter(o => stavOblasti(v, o.id) === "true").length;
  const cast = OBLASTI_SEZNAM.filter(o => stavOblasti(v, o.id) === "mixed").length;
  if (!vse && !cast) return "žádná";
  return vse + " z " + OBLASTI_SEZNAM.length + (cast ? ", " + cast + " jen u některých soudů" : "");
}

// Senáty NS. Občanskoprávní jednotlivě, pod sebou (ve sloupcích shora
// dolů); trestní kolegium jediným vypínačem pro všechny jeho senáty.
// Senát, který je v datech a v seznamu chybí, se nabídne čipem.
function senatyKolegia(k) {
  return SENATY_NS.filter(s => s.kolegium === k).map(s => s.senat);
}

function celeKolegium(v, k) {
  const senaty = senatyKolegia(k);
  return senaty.length > 0 && senaty.every(n => v.ns.senaty.indexOf(n) >= 0);
}

function seznamCisel(cisla) {
  return cisla.length > 1 ? cisla.slice(0, -1).join(", ") + " a " + cisla[cisla.length - 1] : String(cisla[0]);
}

function senatyHtml(v) {
  const skupiny = {};
  senatyKVyberu(v).forEach(s => { (skupiny[s.kolegium] = skupiny[s.kolegium] || []).push(s); });
  const skupina = (nazev, obsah, pocet) => '<div class="vyber-skupina"><div class="skupina-hlava">' +
    '<span class="skupina-nazev">' + esc(nazev) + "</span>" +
    (pocet ? '<span class="skupina-pocet">' + pocet + "</span>" : "") + "</div>" + obsah + "</div>";
  const cip = (s, trida) => cipHtml({ "data-senat": s.senat }, String(v.ns.senaty.indexOf(s.senat) >= 0),
    '<span class="senat-cislo">' + s.senat + "</span>" +
    (s.popis ? '<span class="senat-popis">' + esc(s.popis) + "</span>" : ""), "", trida);
  let html = "";
  if (skupiny.civilni) {
    const vybrane = skupiny.civilni.filter(s => v.ns.senaty.indexOf(s.senat) >= 0).length;
    html += skupina(KOLEGIA.civilni, '<div class="seznam-voleb">' +
      skupiny.civilni.map(s => cip(s, "cip-radek")).join("") + "</div>",
      vybrane + "/" + skupiny.civilni.length);
  }
  const trestni = senatyKolegia("trestni");
  if (trestni.length) {
    html += '<div class="vyber-skupina"><button type="button" class="volba-prepinac" role="switch" ' +
      'aria-checked="' + celeKolegium(v, "trestni") + '" data-kolegium="trestni">' +
      '<span class="volba-text"><span class="volba-titul">Trestní kolegium</span>' +
      '<span class="volba-popis">Všechna rozhodnutí trestních senátů (' + esc(seznamCisel(trestni)) +
      ").</span></span>" + '<span class="switch" aria-hidden="true"></span></button></div>';
  }
  if (skupiny.ostatni) {
    html += skupina("Další senáty", '<div class="cipy">' + skupiny.ostatni.map(s => cip(s)).join("") + "</div>");
  }
  return html + '<p class="vyber-poznamka">Z vybraných senátů se ukáže všechno, bez ohledu na oblast.</p>';
}

function souhrnSenatu(v) {
  const trestni = celeKolegium(v, "trestni") ? senatyKolegia("trestni") : [];
  const cisla = v.ns.senaty.filter(n => trestni.indexOf(n) < 0).sort((a, b) => a - b);
  const casti = [];
  if (cisla.length) casti.push(seznamCisel(cisla));
  if (trestni.length) casti.push("trestní kolegium");
  return casti.length ? casti.join(" + ") : "žádný";
}

// Časopisy: vybraný se ukazuje.
function casopisyHtml(v, casopisy) {
  return '<div class="cipy">' + casopisy.map(c => cipHtml({ "data-casopis": c.id },
      String(v.skryte_casopisy.indexOf(c.id) < 0), esc(c.zkratka), c.nazev)).join("") + "</div>";
}

function souhrnCasopisu(v, casopisy) {
  const skryte = casopisy.filter(c => v.skryte_casopisy.indexOf(c.id) >= 0);
  if (!skryte.length) return "všech " + casopisy.length;
  if (skryte.length === casopisy.length) return "žádný";
  return (casopisy.length - skryte.length) + " z " + casopisy.length;
}

// Rozbalovací sekce: hlavička s názvem a souhrnem, obsah skrytý, dokud ho
// uživatel neotevře.
function sekceHtml(klic, nadpis, souhrn, obsah) {
  const otevreno = otevreneSekce.has(klic);
  return '<div class="vyber-sekce">' +
    '<button type="button" class="sekce-hlava" aria-expanded="' + otevreno + '" aria-controls="sekce-' +
    klic + '" data-sekce="' + klic + '">' +
    '<svg class="fold-ico" aria-hidden="true"><use href="#icon-chevron"></use></svg>' +
    '<span class="sekce-titul">' + esc(nadpis) + '</span><span class="sekce-souhrn">' + esc(souhrn) +
    "</span></button>" +
    '<div class="sekce-obsah" id="sekce-' + klic + '"' + (otevreno ? "" : " hidden") + ">" + obsah + "</div></div>";
}

function akceHtml() {
  const zmeny = rozpracovano();
  return '<div class="vyber-akce" id="vyber-akce">' +
    '<button type="button" class="ucet-btn" id="vyber-vychozi">Obnovit výchozí</button>' +
    '<span class="vyber-stav" id="vyber-stav" role="status">' +
    esc(stavUlozeni || (zmeny ? "Neuložené změny" : "")) + "</span>" +
    '<button type="button" class="ucet-btn" id="vyber-zrusit"' + (zmeny && !ukladam ? "" : " hidden") +
    ">Zrušit změny</button>" +
    '<button type="button" class="ucet-btn ucet-btn-hlavni" id="vyber-ulozit"' +
    (zmeny && !ukladam ? "" : " disabled") + ">Uložit</button></div>";
}

function vykresliNastaveni() {
  const el = document.getElementById("nastaveni-obsah");
  if (!el) return;
  // Dialog se otevírá jen přihlášenému (z nabídky účtu); jinak je prázdný.
  if (clerkStav !== "pripraven" || !prihlaseny) {
    el.innerHTML = "";
    return;
  }
  if (!koncept) koncept = kopie(vyber);
  const v = koncept;
  const casopisy = casopisyKVyberu();
  el.innerHTML = '<div class="nastaveni">' +
    (uvitani ? '<p class="nastaveni-uvitani">Vítejte v Owl. Vyberte si, co chcete sledovat – výchozí ' +
      "je IP a IT. Výběr kdykoli změníte v nabídce účtu.</p>" : "") +
    '<div class="vyber-sekce-seznam">' +
    sekceHtml("oblasti", "Oblasti práva", souhrnOblasti(v), oblastiHtml(v)) +
    sekceHtml("senaty", "Senáty Nejvyššího soudu", souhrnSenatu(v), senatyHtml(v)) +
    (casopisy.length ? sekceHtml("casopisy", "Časopisy", souhrnCasopisu(v, casopisy), casopisyHtml(v, casopisy)) : "") +
    "</div>" + akceHtml() + "</div>";
  hlidejOdchod();
}

// Jen lišta s tlačítky (stav ukládání) – zbytek stránky se nemění.
function obnovAkce() {
  const lista = document.getElementById("vyber-akce");
  if (lista) lista.outerHTML = akceHtml();
  hlidejOdchod();
}

// Neuložené změny by zavřením nebo obnovením stránky zmizely. Posluchač
// beforeunload visí jen, když nějaké jsou – stálý by prohlížeči bránil
// uložit stránku pro návrat zpět (bfcache).
let strazAktivni = false;

function strazOdchodu(e) {
  e.preventDefault();
  e.returnValue = "";
}

function hlidejOdchod() {
  const treba = rozpracovano();
  if (treba === strazAktivni) return;
  strazAktivni = treba;
  if (treba) window.addEventListener("beforeunload", strazOdchodu);
  else window.removeEventListener("beforeunload", strazOdchodu);
}

// Seznamy ve výběru drží stálé pořadí (oblasti podle seznamu, senáty podle
// čísla, časopisy podle registru) – výběr, který se proklikáním vrátí
// k uloženému nebo výchozímu, se pak jako takový i pozná.
function seradVyber(v) {
  const poradi = (seznam, klic) => (a, b) =>
    seznam.findIndex(x => x[klic] === a) - seznam.findIndex(x => x[klic] === b);
  SOUDY_VYBERU.forEach(s => v[s.soud].oblasti.sort(poradi(OBLASTI_SEZNAM, "id")));
  v.ns.senaty.sort((a, b) => a - b);
  v.skryte_casopisy.sort(poradi(CASOPISY, "id"));
  return v;
}

function nastavPolozku(seznam, x, zapnout) {
  const i = seznam.indexOf(x);
  if (zapnout && i < 0) seznam.push(x);
  else if (!zapnout && i >= 0) seznam.splice(i, 1);
}

function prepniOblasti(v, ids) {
  const zapnout = !ids.every(id => stavOblasti(v, id) === "true");
  ids.forEach(id => SOUDY_VYBERU.forEach(s => nastavPolozku(v[s.soud].oblasti, id, zapnout)));
}

// Uložení: koncept se zapíše do účtu a teprve po úspěchu začne platit na
// webu. Výchozí výběr se neukládá – z účtu se `owl` smaže.
function ulozVyber() {
  const user = clerk && clerk.user;
  if (!user || !koncept || ukladam) return;
  const novy = kopie(koncept);
  const meta = Object.assign({}, user.unsafeMetadata);
  if (jeVychozi(novy)) delete meta.owl;
  else meta.owl = novy;
  ukladam = true;
  stavUlozeni = "Ukládám…";
  obnovAkce();
  user.update({ unsafeMetadata: meta }).then(() => {
    ukladam = false;
    vyber = novy;
    koncept = null;
    stavUlozeni = "";
    vykresliZdroje();
    const dialog = document.getElementById("vyber-dialog");
    if (dialog && dialog.open) dialog.close();
  }, () => {
    ukladam = false;
    stavUlozeni = "Nepodařilo se uložit – zkuste to znovu.";
    obnovAkce();
  });
}

// Po změně se stránka výběru vykreslí znovu (souhrny, stavy čipů, lišta);
// fokus zůstane na prvku, na který se klikalo.
function prekresli(el, t) {
  const klic = ["oblast", "skupina", "senat", "kolegium", "casopis"].find(k => t.dataset[k] !== undefined);
  const selektor = klic ? "[data-" + klic + '="' + CSS.escape(t.dataset[klic]) + '"]' : (t.id ? "#" + t.id : "");
  vykresliNastaveni();
  const cil = selektor && el.querySelector(selektor);
  if (cil && !cil.disabled && !cil.hidden) cil.focus({ preventScroll: true });
}

// Dialog Můj výběr: otevře se s uloženým výběrem; nepřihlášenému nabídne
// přihlášení. Zavřít s neuloženými změnami jde až po potvrzení.
function otevriVyber() {
  const dialog = document.getElementById("vyber-dialog");
  if (!dialog) return;
  if (clerkStav !== "pripraven") return;
  if (!prihlaseny) {
    prihlasit(false);
    return;
  }
  koncept = null;
  stavUlozeni = "";
  vykresliNastaveni();
  if (!dialog.open) {
    otevrenoZ = document.activeElement;
    dialog.showModal();
  }
}

function zavriVyber() {
  const dialog = document.getElementById("vyber-dialog");
  if (!dialog || !dialog.open) return;
  if (rozpracovano() && !window.confirm("Výběr má neuložené změny. Zavřít bez uložení?")) return;
  koncept = null;
  stavUlozeni = "";
  dialog.close();
}

function initNastaveni() {
  const el = document.getElementById("nastaveni-obsah");
  const dialog = document.getElementById("vyber-dialog");
  document.addEventListener("click", e => {
    const rezimTl = e.target.closest("[data-rezim]");
    if (rezimTl) nastavRezim(rezimTl.dataset.zdroj, rezimTl.dataset.rezim);
    else if (e.target.closest(".ucet-prihlasit")) prihlasit(false);
    else if (e.target.closest(".ucet-registrace")) prihlasit(true);
    else if (e.target.closest('a[href="#nastaveni"]')) {
      e.preventDefault();
      otevriVyber();
    }
  });
  if (dialog) {
    // Esc, křížek i klik vedle dialogu (na jeho pozadí) zavírají stejně.
    dialog.addEventListener("cancel", e => { e.preventDefault(); zavriVyber(); });
    dialog.addEventListener("click", e => { if (e.target === dialog) zavriVyber(); });
    const zavrit = document.getElementById("vyber-zavrit");
    if (zavrit) zavrit.addEventListener("click", zavriVyber);
    // Po zavření fokus tam, odkud se dialog otevřel; položka z nabídky účtu
    // mezitím zmizí, pak na tlačítko účtu.
    dialog.addEventListener("close", () => {
      uvitani = false;
      hlidejOdchod();
      const videt = x => x && x.isConnected && x !== document.body && x.getClientRects().length > 0;
      const cil = videt(otevrenoZ) ? otevrenoZ
        : document.querySelector("#ucet .cl-userButtonTrigger, #ucet button");
      otevrenoZ = null;
      if (cil && typeof cil.focus === "function") cil.focus();
    });
  }
  if (!el) return;
  el.addEventListener("click", e => {
    const t = e.target.closest("button");
    if (!t || !el.contains(t) || !koncept) return;
    const d = t.dataset;
    if (d.sekce) {
      const otevrit = t.getAttribute("aria-expanded") !== "true";
      t.setAttribute("aria-expanded", String(otevrit));
      const obsah = document.getElementById("sekce-" + d.sekce);
      if (obsah) obsah.hidden = !otevrit;
      otevreneSekce[otevrit ? "add" : "delete"](d.sekce);
      return;
    }
    if (t.id === "vyber-ulozit") {
      ulozVyber();
      return;
    }
    if (ukladam) return;
    if (t.id === "vyber-zrusit") koncept = kopie(vyber);
    else if (t.id === "vyber-vychozi") koncept = vychoziVyber();
    else if (d.oblast) prepniOblasti(koncept, [d.oblast]);
    else if (d.skupina !== undefined) {
      prepniOblasti(koncept, skupinyOblasti()[Number(d.skupina)].oblasti.map(o => o.id));
    } else if (d.senat) {
      const n = Number(d.senat);
      nastavPolozku(koncept.ns.senaty, n, koncept.ns.senaty.indexOf(n) < 0);
    } else if (d.casopis) {
      nastavPolozku(koncept.skryte_casopisy, d.casopis, koncept.skryte_casopisy.indexOf(d.casopis) < 0);
    } else if (d.kolegium) {
      const zapnout = !celeKolegium(koncept, d.kolegium);
      senatyKolegia(d.kolegium).forEach(n => nastavPolozku(koncept.ns.senaty, n, zapnout));
    } else return;
    seradVyber(koncept);
    stavUlozeni = "";
    prekresli(el, t);
  });
}

/* ========== Stránky a navigace ========== */
// Obsah je rozdělený na stránky; přepíná se podle adresy (#kotva).
// Kotvy sekcí zůstávají platné – odkaz na #dnesni otevře Novinky.
// Dvoutýdenní přehled i každý zdroj mají vlastní stránku.
const PAGES = [
  { id: "prehled",  sections: ["dnesni"] },
  { id: "dvatydny", sections: [] },
  { id: "nsoud",    sections: [] },
  { id: "nss",      sections: [] },
  { id: "us",       sections: [] },
  { id: "sdeu",     sections: [] },
  { id: "casopisy", sections: [] },
  { id: "kalendar", sections: ["jednani"] }
];
// Kotvy z uložených odkazů: dřív byly všechny zdroje na jedné stránce
// „Všechno nové" a SDEU se jmenoval cjeu. Můj výběr byl stránkou #nastaveni –
// teď je to dialog, kotva ho otevře nad Novinkami.
const STARE_KOTVY = { recentni: "nsoud", cjeu: "sdeu", nastaveni: "prehled" };
let vyberZKotvy = false;

let currentPage = PAGES[0];
let updateNav = function () {};

function pageOf(hash) {
  const id = String(hash || "").replace(/^#/, "");
  return PAGES.find(p => p.id === id || p.sections.indexOf(id) >= 0) || PAGES[0];
}

// Přepne na stránku a odscrolluje – buď na sekci, nebo na začátek stránky.
function navigate(hash, push) {
  let id = String(hash || "").replace(/^#/, "");
  if (id === "nastaveni") {
    // Dialog potřebuje Clerk – když ještě není, otevře se po přihlášení.
    if (clerkStav === "pripraven") otevriVyber();
    else vyberZKotvy = true;
  }
  if (STARE_KOTVY[id]) {
    id = STARE_KOTVY[id];
    history.replaceState(null, "", "#" + id);
  }
  const page = pageOf(id);
  currentPage = page;
  PAGES.forEach(p => {
    const el = document.getElementById(p.id);
    if (el) el.hidden = (p !== page);
  });
  // Tabulky na dosud skryté stránce teprve teď mají rozměry.
  prizpusobViditelne(document.getElementById(page.id));

  const section = page.sections.indexOf(id) >= 0 ? document.getElementById(id) : null;
  if (section) section.scrollIntoView({ block: "start" });
  else window.scrollTo(0, 0);

  if (push) {
    history.pushState(null, "", "#" + (id || page.id));
    const nadpis = document.querySelector("#" + page.id + " .card-title");
    if (nadpis) {
      nadpis.setAttribute("tabindex", "-1");
      nadpis.focus({ preventScroll: true });
    }
  }
  updateNav();
  odhalZalozku();
}

// V úzkém okně se přepínač stránek posouvá do strany: aktivní záložka
// musí být vidět a u okraje, za kterým jsou další záložky, text vybledne.
function odhalZalozku() {
  const tabs = document.querySelector(".pagetabs");
  const a = tabs && tabs.querySelector("a.active");
  if (!a || tabs.scrollWidth <= tabs.clientWidth) return;
  const t = tabs.getBoundingClientRect();
  const r = a.getBoundingClientRect();
  if (r.left < t.left + 28) tabs.scrollLeft -= t.left + 28 - r.left;
  else if (r.right > t.right - 28) tabs.scrollLeft += r.right - (t.right - 28);
}

function initPagetabs() {
  const tabs = document.querySelector(".pagetabs");
  if (!tabs) return;
  const okraje = () => {
    const max = tabs.scrollWidth - tabs.clientWidth;
    tabs.classList.toggle("dalsi-vlevo", tabs.scrollLeft > 2);
    tabs.classList.toggle("dalsi-vpravo", tabs.scrollLeft < max - 2);
  };
  tabs.addEventListener("scroll", okraje, { passive: true });
  window.addEventListener("resize", okraje);
  okraje();
}

function initNav() {
  const links = Array.from(document.querySelectorAll("#sidenav a, .pagetabs a"));

  function update() {
    // Sekce se hledají až tady – po přepnutí stránky jsou vidět jiné.
    const targets = currentPage.sections
      .map(id => ({ id, el: document.getElementById(id) }))
      .filter(t => t.el);
    let current = targets[0];
    targets.forEach(t => {
      if (t.el.getBoundingClientRect().top <= 140) current = t;
    });
    // U konce stránky zvýrazni poslední položku (sekce dole se nemusí
    // doscrollovat až k hornímu okraji okna).
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) {
      current = targets[targets.length - 1];
    }
    const active = current ? current.id : "";
    links.forEach(a => {
      const href = String(a.getAttribute("href") || "").replace(/^#/, "");
      // Zvýrazněná je aktuální sekce a k ní i její stránka.
      a.classList.toggle("active", href === active || href === currentPage.id);
      if (href === currentPage.id) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
  }

  links.forEach(a => a.addEventListener("click", e => {
    // Skok si řešíme sami – cílová sekce může být na skryté stránce.
    e.preventDefault();
    navigate(a.getAttribute("href"), true);
  }));
  // Logo vede na Novinky (na začátek stránky); když už tam jsme,
  // nový záznam do historie nepřidá.
  const znacka = document.querySelector(".znacka");
  if (znacka) znacka.addEventListener("click", e => {
    e.preventDefault();
    navigate("#prehled", location.hash !== "#prehled");
  });
  window.addEventListener("popstate", () => navigate(location.hash, false));
  document.addEventListener("scroll", update, { passive: true });
  updateNav = update;
  initPagetabs();
}

// Datum poslední aktualizace = nejnovější aktualizace ze všech zdrojů
// (`generated` v JSONu).
function showUpdated() {
  const latest = FEEDS.map(f => new Date(f.aktualizovano || ""))
    .filter(d => !isNaN(d)).sort((a, b) => b - a)[0];
  if (!latest) return;
  document.getElementById("updated").textContent =
    "aktualizováno " + latest.toLocaleDateString("cs-CZ") + " " +
    latest.toLocaleTimeString("cs-CZ", { hour: "2-digit", minute: "2-digit" });
}

function initApp() {
  // Prohlížeč se po refreshi snaží obnovit pozici scrollu, jenže obsah tu
  // dorazí až po fetchi – stránka by se pod ním nafoukla a poskočila.
  // Necháme si scroll pod kontrolou a začínáme nahoře.
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";

  // Každý zdroj stáhneme jen jednou a sdílíme mezi sekcemi (všechno / nové).
  // Výchozí výběr (oblasti, senáty NS) se musí znát dřív, než se filtruje.
  const vyberPromise = Promise.all([
    fetchJson("data/oblasti.json").catch(() => null),
    fetchJson("data/ns_senaty.json").catch(() => null)
  ]).then(([oblasti, senaty]) => nastavVyber(oblasti, senaty));
  // Filtruje se až při vykreslení (vykresliZdroje) – výběr se po přihlášení
  // může změnit a data se kvůli tomu znovu nestahují.
  const feedPromises = FEEDS.map(f => vyberPromise.then(() => nactiZdroj(f)));
  // Dvoutýdenní přehled a kalendář se generují zvlášť – když chybí, jen se
  // nevykreslí; zbytek stránky na ně nečeká déle než na feedy.
  const digestPromise = fetchJson("digest.json").catch(() => null);
  const hearingsPromise = fetchJson("hearings.json").catch(() => null);
  initNav();
  initHelp();
  initNastaveni();
  // Tažení šířek visí na dokumentu, takže platí i pro tabulky, které
  // se vykreslí až po dotažení feedů.
  initColumnResize();

  // Vykreslíme až všechny feedy dorazí (stahují se paralelně, jsou ze
  // stejného původu). Jedno překreslení místo tří – stránka při načítání
  // neposkakuje. Selhání jednoho feedu ostatní nezdrží.
  // Čekáme i na fonty, ať text po odkrytí nepřeskočí na jiné písmo.
  const fontsReady = document.fonts ? document.fonts.ready.catch(() => {}) : Promise.resolve();
  Promise.all([Promise.allSettled(feedPromises), digestPromise, hearingsPromise, fontsReady])
    .then(([results, digest, hearings]) => {
      zdrojeVysledky = results;
      ukazOkna();
      vykresliZdroje();
      vykresliNastaveni();
      renderDigest(digest);
      renderKalendar(hearings);
      // Až teď je jasná výška stránky – otevřeme kotvu z adresy (a přepočítáme
      // zvýraznění) ještě než stránku odkryjeme, ať nic nepřeskočí.
      navigate(location.hash, false);
      document.documentElement.classList.remove("is-loading");
      // Přihlášení až teď – obsah na Clerk nečeká.
      initClerk();
    });

  Promise.allSettled(feedPromises).then(showUpdated);
}

initApp();
