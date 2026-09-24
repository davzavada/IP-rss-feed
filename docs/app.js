/* Owl – skript stránky. Čte hotové soubory vedle sebe (data/judikatura/*.json,
   ipcuria_feed.xml, journals_feed.xml, data/oblasti.json, digest.json,
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

function text(item, sel) {
  const el = item.querySelector(sel);
  return el ? el.textContent.trim() : "";
}

/* Položky ze všech zdrojů převádíme na obyčejné objekty, ať buňky tabulek
   nemusí rozlišovat XML feed od JSONu:
     title, link, doc (PDF), heslo, shrnuti, poznamka, datum, nove, autori
   a u judikatury navíc oblasti, senat, druh, procesni. */

// Autoři jsou ve feedu v <dc:creator> – čteme je přes jmenný prostor,
// se záložním hledáním podle celého názvu značky (starší prohlížeče).
const DC_NS = "http://purl.org/dc/elements/1.1/";

function zXml(el) {
  const autor = el.getElementsByTagNameNS(DC_NS, "creator")[0] ||
    el.getElementsByTagName("dc:creator")[0];
  return {
    title: text(el, "title"),
    link: text(el, "link"),
    doc: text(el, "document-url"),
    heslo: text(el, "ai-tag"),
    shrnuti: text(el, "ai-summary"),
    poznamka: text(el, "note"),
    datum: text(el, "pubDate"),
    nove: !!el.querySelector("is-new"),
    autori: autor ? autor.textContent.trim() : ""
  };
}

// „Nové" = poprvé viděné za posledních 24 hodin (stejně jako u feedů).
const NOVE_MS = 24 * 60 * 60 * 1000;

function zJson(r) {
  const prvni = Date.parse(r.first_seen || "");
  return {
    title: r.spz || r.nazev || "",
    link: r.url || "",
    doc: r.pdf && r.pdf !== r.url ? r.pdf : "",
    heslo: r.heslo || "",
    shrnuti: r.shrnuti || "",
    poznamka: r.poznamka || "",
    datum: r.zverejneno || r.first_seen || "",
    nove: !isNaN(prvni) && Date.now() - prvni < NOVE_MS,
    autori: "",
    oblasti: r.oblasti || [],
    senat: r.senat,
    druh: r.druh || "",
    procesni: !!r.procesni
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

function fetchFeed(url) {
  return fetch(url)
    .then(r => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.text();
    })
    .then(xml => {
      const doc = new DOMParser().parseFromString(xml, "text/xml");
      // Chybová stránka (404 aj.) není XML – radši ohlásit chybu než „Žádné položky".
      if (doc.querySelector("parsererror")) throw new Error("neplatné XML");
      return doc;
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
// (Shrnutí), si rozebere zbytek řádku. Každá tabulka má jinou skladbu
// sloupců, takže se výchozí hodnoty počítají pro každou zvlášť.
const COL_DEFAULTS = { type: 12, name: 20, oblasti: 14, heslo: 18, src: 15, date: 10, author: 14 };
const MIN_COL_PCT = 4;          // pod tuhle šířku sloupec nepustíme
const KEY_STEP_PCT = 2;         // krok při ovládání šipkami
const COL_COOKIE = "colw";
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
function widthsFor(key, columns) {
  const saved = storedWidths[key];
  const usable = Array.isArray(saved) && saved.length === columns.length &&
    saved.every(w => typeof w === "number" && isFinite(w) && w >= 1);
  if (!usable) return defaultWidths(columns);
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
}

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

function nameCell(item) {
  const title = esc(item.title.replace(/^\[[^\]]+\]\s*/, ""));
  const href = safeHref(item.link);
  let html = href ? '<a href="' + href + '">' + title + "</a>" : title;
  // Odkaz ještě na samotný dokument (PDF rozhodnutí, znění předběžné otázky)
  // – shrnutí je jen shrnutí.
  const doc = safeHref(item.doc);
  if (doc) html += ' <a class="doc-link" href="' + doc + '">PDF</a>';
  return html;
}

function authorCell(item) {
  return item.autori ? '<span class="author">' + esc(item.autori) + "</span>" : "";
}

// V přehledu přes všechny zdroje jdou autoři pod název (má je jen část položek).
function nameAuthorCell(item) {
  return nameCell(item) + authorCell(item);
}

function hesloCell(item) {
  const tag = item.heslo;
  return tag ? '<span class="heslo" title="' + esc(tag) + '">' + esc(tag) + "</span>" : "";
}

function summaryCell(item) {
  if (item.shrnuti) return '<span class="summary">' + esc(item.shrnuti) + "</span>";
  // Bez shrnutí ještě může být poznámka, proč žádné není – třeba že u žádosti
  // o předběžnou otázku zatím nejsou zveřejněné otázky.
  return item.poznamka ? '<span class="summary note">' + esc(item.poznamka) + "</span>" : "";
}

function dateCell(item) {
  return czDate(item.datum);
}

function typeCell(item) {
  return tagBadge(tagOf(item.title));
}

/* ========== Oblasti a výběr (výchozí, nebo uložený u účtu) ========== */
// Seznam oblastí a senátů NS čte stránka z data/ (stejné soubory jako AI).
// Nepřihlášený vidí výchozí výběr (IP a IT); přihlášený svůj, uložený
// v Clerku jako user.unsafeMetadata.owl:
//   {"v":1,"ns":{"senaty":[23],"oblasti":[…]},"skryt_procesni":false,
//    "skryte_casopisy":[]}
let OBLASTI = {};                 // id -> název
let OBLASTI_SEZNAM = [];          // oblasti v pořadí seznamu (se skupinou)
let ALIAS_OBLASTI = {};           // přejmenované id -> nové
let VYCHOZI_OBLASTI = [];
let VYCHOZI_SENATY_NS = [23];
let SENATY_NS = [];               // [{senat, kolegium, popis}]

// Soudy, u kterých se vybírají oblasti – sloupce matice v nastavení.
// Další soudy přibudou s jejich daty.
const SOUDY_VYBERU = [{ soud: "ns", nazev: "Nejvyšší soud" }];

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
  const v = { v: 1, skryt_procesni: false, skryte_casopisy: [] };
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
  v.skryt_procesni = ulozeny.skryt_procesni === true;
  if (Array.isArray(ulozeny.skryte_casopisy)) {
    v.skryte_casopisy = bezDuplicit(ulozeny.skryte_casopisy.filter(c => typeof c === "string"));
  }
  return v;
}

function jeVychozi(v) {
  return JSON.stringify(v) === JSON.stringify(vychoziVyber());
}

// Rozhodnutí NS je vidět, když je z vybraného senátu, nebo když spadá do
// některé z vybraných oblastí; rutinní procesní jen když je uživatel nechce
// skrýt.
function vidiNS(item) {
  const v = vyber || vychoziVyber();
  if (v.skryt_procesni && item.procesni) return false;
  if (v.ns.senaty.indexOf(item.senat) >= 0) return true;
  return (item.oblasti || []).some(o => v.ns.oblasti.indexOf(o) >= 0);
}

function vidiCasopis(item) {
  const v = vyber || vychoziVyber();
  return v.skryte_casopisy.indexOf(tagOf(item.title)) < 0;
}

// Definice sloupců sdílíme mezi živým feedem a novými položkami.
const colsNsoud = [
  { label: "Spisová značka", cls: "col-name", render: nameCell },
  { label: "Heslo", cls: "col-heslo", render: hesloCell },
  { label: "Shrnutí", cls: "col-summary", render: summaryCell },
  { label: "Datum", cls: "col-date", render: dateCell }
];

const colsCjeu = [
  { label: "Typ", cls: "col-type", render: typeCell },
  { label: "Případ", cls: "col-name", render: nameCell },
  { label: "Heslo", cls: "col-heslo", render: hesloCell },
  { label: "Shrnutí", cls: "col-summary", render: summaryCell },
  { label: "Datum", cls: "col-date", render: dateCell }
];

const colsJournals = [
  { label: "Časopis", cls: "col-type", render: typeCell },
  { label: "Název", cls: "col-name", render: nameCell, width: 26 },
  { label: "Autor", cls: "col-author", render: authorCell },
  { label: "Shrnutí", cls: "col-summary", render: summaryCell },
  // U časopisů, které datum vydání neuvádějí, je to datum, kdy článek
  // ve feedu přibyl (viz pub_date_odhad ve scraper_journals.py).
  { label: "Datum", cls: "col-date", render: dateCell }
];

// Nové položky – kombinovaná tabulka přes všechny tři zdroje.
// Ve sloupci Zdroj je vedle sebe štítek zdroje a štítek časopisu/typu.
const colsToday = [
  { label: "Zdroj", cls: "col-src", render: i =>
      '<span class="src src-' + i._src + '">' + i._srcLabel + "</span> " + typeCell(i) },
  { label: "Název", cls: "col-name", render: nameAuthorCell },
  { label: "Heslo", cls: "col-heslo", render: hesloCell },
  { label: "Shrnutí", cls: "col-summary", render: summaryCell },
  { label: "Datum", cls: "col-date", render: dateCell }
];

// Zdroje stránky. Judikatura je v JSONu s oknem pro web (data/judikatura/),
// časopisy a SDEU zatím v XML; `filtr` je výběr, co z okna ukázat.
const PRAZDNY_VYBER = 'Ve vašem výběru za tu dobu nic nepřibylo. <a href="#nastaveni">Upravit výběr</a>';
const FEEDS = [
  { key: "nsoud",    label: "NS",      json: "data/judikatura/ns.json", cols: colsNsoud,
    containerId: "feed-nsoud", filtr: vidiNS, prazdno: PRAZDNY_VYBER },
  { key: "cjeu",     label: "CJEU",    url: "ipcuria_feed.xml",  cols: colsCjeu,     containerId: "feed-cjeu" },
  { key: "journals", label: "Časopis", url: "journals_feed.xml", cols: colsJournals, containerId: "feed-journals",
    filtr: vidiCasopis, prazdno: PRAZDNY_VYBER }
];

// Položky zdroje jako objekty; u zdroje si poznamená, kdy byl aktualizován.
function nactiZdroj(f) {
  if (f.json) {
    return fetchJson(f.json).then(d => {
      f.aktualizovano = d.generated || "";
      return (d.polozky || []).map(zJson);
    });
  }
  return fetchFeed(f.url).then(doc => {
    f.aktualizovano = (doc.querySelector("lastBuildDate") || {}).textContent || "";
    return Array.from(doc.querySelectorAll("item")).map(zXml);
  });
}

function resizerHtml(column) {
  return '<span class="col-resizer" role="separator" aria-orientation="vertical"' +
    ' tabindex="0" aria-label="Šířka sloupce ' + esc(column.label) + '"' +
    ' title="Táhnutím změníte šířku sloupce, dvojklikem obnovíte výchozí"></span>';
}

// `prazdno` je HTML hlášky, když nic není (u filtrovaných karet odkaz na výběr).
function renderTable(items, container, columns, key, prazdno) {
  if (items.length === 0) {
    container.innerHTML = '<p class="feed-empty">' + (prazdno || "Žádné nové položky.") + "</p>";
    return;
  }
  tableColumns[key] = columns;
  let html = '<div class="table-wrap"><table data-cols="' + esc(key) + '"><colgroup>';
  widthsFor(key, columns).forEach(w => { html += '<col style="width:' + w.toFixed(2) + '%">'; });
  html += "</colgroup><thead><tr>";
  columns.forEach((c, i) => {
    // Poslední sloupec už nemá kam růst – hranice je vždy mezi dvěma sloupci.
    html += "<th" + (c.cls ? ' class="' + c.cls + '"' : "") + ">" + c.label +
      (i < columns.length - 1 ? resizerHtml(c) : "") + "</th>";
  });
  html += "</tr></thead><tbody>";
  items.forEach(item => {
    html += "<tr>";
    columns.forEach(c => {
      html += "<td" + (c.cls ? ' class="' + c.cls + '"' : "") + ">" + c.render(item) + "</td>";
    });
    html += "</tr>";
  });
  html += "</tbody></table></div>";
  container.innerHTML = html;
}

// `results` jsou výsledky Promise.allSettled nad položkami jednotlivých feedů.
function renderToday(results) {
  const container = document.getElementById("feed-today");
  // Když selžou všechny feedy, není to „nic nového", ale chyba načítání.
  if (results.every(r => r.status === "rejected")) {
    failed("feed-today");
    return;
  }
  const today = [];
  results.forEach((r, idx) => {
    if (r.status !== "fulfilled") return;
    r.value.forEach(item => {
      if (item.nove) {
        item._src = FEEDS[idx].key;
        item._srcLabel = FEEDS[idx].label;
        today.push(item);
      }
    });
  });
  today.sort((a, b) => new Date(b.datum) - new Date(a.datum));
  if (today.length === 0) {
    container.innerHTML = '<p class="feed-empty">Za posledních 24 hodin nic nepřibylo. ' +
      "Feedy se obnovují ráno v 7:00 a odpoledne ve 14:00.</p>";
  } else {
    renderTable(today, container, colsToday, "today");
  }
}

// Stažené položky zdrojů (výsledky Promise.allSettled). Při změně výběru se
// jen znovu profiltrují a vykreslí – nic se znovu nestahuje.
let zdrojeVysledky = null;

function vykresliZdroje() {
  if (!zdrojeVysledky) return;
  const filtrovane = zdrojeVysledky.map((r, idx) => {
    const f = FEEDS[idx];
    if (r.status !== "fulfilled" || !f.filtr) return r;
    return { status: "fulfilled", value: r.value.filter(f.filtr) };
  });
  FEEDS.forEach((f, idx) => {
    if (filtrovane[idx].status === "rejected") {
      failed(f.containerId);
      return;
    }
    // Druhá stránka je úplný výpis za okno feedu – nové položky z ní
    // nevynecháváme, na jednu stránku se položky nedostanou dvakrát.
    renderTable(filtrovane[idx].value, document.getElementById(f.containerId), f.cols, f.key,
                f.prazdno);
  });
  renderToday(filtrovane);
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

// Sbalí nebo rozbalí kartu s přehledem; hlavička s datem zůstává vidět vždy.
function setDigestFolded(folded) {
  const card = document.getElementById("dvatydny");
  const btn = document.getElementById("digest-fold");
  const content = document.getElementById("feed-digest");
  if (!card || !btn || !content) return;
  card.classList.toggle("is-folded", folded);
  content.hidden = folded;
  btn.setAttribute("aria-expanded", String(!folded));
  btn.setAttribute("aria-label", folded ? "Rozbalit přehled" : "Sbalit přehled");
}

function isDigestFolded() {
  const btn = document.getElementById("digest-fold");
  return !btn || btn.getAttribute("aria-expanded") !== "true";
}

function isToday(iso) {
  const d = new Date(iso);
  return !isNaN(d) && d.toDateString() === new Date().toDateString();
}

function renderDigest(data) {
  const container = document.getElementById("feed-digest");
  // Přehled se na rozdíl od feedů generuje jen jednou týdně, takže datum
  // poslední aktualizace patří k němu – to v hlavičce stránky je z feedů.
  const stamp = document.getElementById("digest-updated");
  if (stamp && data && data.generated) {
    stamp.textContent = "aktualizováno " + czDate(data.generated);
  }
  // Rozbalený je přehled jen v den, kdy vznikl – po zbytek týdne je to
  // stále stejný text, tak ať neodsouvá zbytek stránky. Bez data je sbalený.
  setDigestFolded(!(data && data.generated && isToday(data.generated)));
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

// Seznam změn pod mřížkou – kvůli němu se přehledy porovnávají. Sbalený je
// ve výchozím stavu, ať dlouhý výpis neodsouvá kalendář; rozbalí se kliknutím
// na hlavičku.
function zmenyHtml() {
  const zmeny = (calData.zmeny || []).filter(z => z && z.spz && z.typ);
  let html = '<div class="cal-zmeny">' +
    '<div class="cal-zmeny-head" id="zmeny-fold" role="button" tabindex="0" ' +
      'aria-expanded="false" aria-controls="zmeny-content">' +
      '<h3>Změny v přehledech soudů</h3>' +
      '<span class="fold" aria-hidden="true"><svg class="fold-ico"><use href="#icon-chevron"></use></svg></span>' +
    "</div>";
  if (!zmeny.length) {
    return html + '<div class="card-content" id="zmeny-content" hidden>' +
      '<p class="feed-empty">Od minulých přehledů se nic nezměnilo.</p></div></div>';
  }
  html += '<div class="card-content" id="zmeny-content" hidden><ul>';
  zmeny.forEach(z => {
    html += "<li>" +
      '<span class="cal-zmena-kdy">' + esc(czDate(z.kdy)) + "</span>" +
      courtBadge(z) +
      '<span class="cal-zmena-spz">' + esc(z.spz) + "</span>" +
      '<span class="cal-zmena">' + esc(zmenaText(z)) + "</span>" +
      '<span class="cal-zmena-kdy">jednání ' + esc(czDate(z.datum)) + "</span>" +
      "</li>";
  });
  return html + "</ul></div></div>";
}

/* ========== Koho kalendář sleduje ========== */
// Patička kalendáře: které senáty se sledují a kteří soudci je vedou.
// Předseda, členové a agenda jsou z rozvrhu práce (sestavy v hearings.json),
// vedle nich soudci, kteří jednáním v přehledech skutečně předsedají – u VS
// se v jednom senátu střídají, z rozvrhu samotného by to vidět nebylo.
const SOUD_NAZVY = { MS: "Městský soud v Praze", VS: "Vrchní soud v Praze" };
const TITULY_RE = /\b(?:JUDr|Mgr|Bc|Ing|PhDr|MUDr|RNDr|Dr|doc|prof|Ph\.?\s?D|LL\.?\s?M|MBA|DiS|CSc)\b\.?/gi;

// „JUDr. Mgr. Petr Košík, Ph.D." -> „Petr Košík".
function jmenoSoudce(jmeno) {
  return String(jmeno || "").replace(TITULY_RE, " ").replace(/[,;()]/g, " ")
    .replace(/\s+/g, " ").trim();
}

// Senát -> soudci, kteří v přehledech předsedají, od nejčastějšího. Bere
// i minulá jednání z archivu, ať seznam nezávisí na tom, co je zrovna v okně.
function predsedajiciSoudci(soud) {
  const pocty = {};
  (calData.jednani || []).forEach(j => {
    const jmeno = jmenoSoudce(j.predseda);
    if (j.soud !== soud || !j.senat || !jmeno) return;
    const senat = pocty[j.senat] || (pocty[j.senat] = {});
    senat[jmeno] = (senat[jmeno] || 0) + 1;
  });
  const out = {};
  Object.keys(pocty).forEach(k => {
    out[k] = Object.keys(pocty[k]).sort((a, b) => pocty[k][b] - pocty[k][a]);
  });
  return out;
}

function senatCislo(k) {
  return parseInt(String(k).split(" ")[0], 10) || 0;
}

// „2 C", „2 Cm", „2 EC" -> „2 C, Cm, EC"; jinak klíče čárkou za sebou.
function senatySpolu(klice) {
  const cisla = new Set(klice.map(senatCislo));
  if (cisla.size === 1 && klice.length > 1) {
    return senatCislo(klice[0]) + " " + klice.map(k => k.split(" ").slice(1).join(" ")).join(", ");
  }
  return klice.join(", ");
}

function sestavySouduHtml(soud) {
  const vedou = predsedajiciSoudci(soud);
  const radky = [];
  const pokryte = new Set();
  ((calData.sestavy || {})[soud] || []).forEach(s => {
    const klice = (s && s.senaty) || [];
    if (!klice.length) return;
    klice.forEach(k => pokryte.add(k));
    radky.push({ senaty: klice, predseda: s.predseda, clenove: s.clenove || [],
                 agenda: s.agenda });
  });
  // Senáty mimo sestavy z rozvrhu. U městského soudu patří rejstříky C, Cm,
  // EC a ECm s týmž číslem k jednomu oddělení (a jednomu soudci), proto se
  // slučují; u vrchního soudu je „1 Co" jiný senát než „1 Cmo".
  const skupiny = new Map();
  ((calData.senaty || {})[soud] || []).filter(k => !pokryte.has(k)).forEach(k => {
    const klic = soud === "MS" ? String(senatCislo(k)) : k;
    if (!skupiny.has(klic)) skupiny.set(klic, []);
    skupiny.get(klic).push(k);
  });
  skupiny.forEach(klice => radky.push({ senaty: klice }));
  if (!radky.length) return "";
  radky.sort((a, b) => senatCislo(a.senaty[0]) - senatCislo(b.senaty[0]));

  let html = '<div class="cal-sestavy-soud"><h4>' +
    esc(SOUD_NAZVY[soud] || COURT_LABELS[soud] || soud) + "</h4><ul>";
  radky.forEach(r => {
    const predsedaji = [];
    r.senaty.forEach(k => (vedou[k] || []).forEach(j => {
      if (predsedaji.indexOf(j) < 0) predsedaji.push(j);
    }));
    const casti = [];
    if (r.predseda) {
      casti.push("předseda " + esc(r.predseda) +
        (r.clenove && r.clenove.length ? ", členové " + esc(r.clenove.join(", ")) : ""));
    }
    if (r.agenda) casti.push(esc(r.agenda));
    if (predsedaji.length) {
      casti.push('<span class="cal-sestava-vedou">v přehledech ' +
        (predsedaji.length > 1 ? "předsedají " : "předsedá ") +
        esc(predsedaji.join(", ")) + "</span>");
    } else if (!r.predseda) {
      casti.push('<span class="cal-sestava-vedou">v přehledech zatím bez jednání</span>');
    }
    html += '<li><span class="cal-sestava-senaty">' + esc(senatySpolu(r.senaty)) + "</span>" +
      '<span class="cal-sestava-kdo">' + casti.join(" · ") + "</span></li>";
  });
  return html + "</ul></div>";
}

function sestavyHtml() {
  const soudy = Object.keys(calData.senaty || {})
    .filter(s => ((calData.senaty || {})[s] || []).length);
  if (!soudy.length) return "";
  return '<div class="cal-sestavy"><h3>Koho kalendář sleduje</h3>' +
    soudy.map(sestavySouduHtml).join("") +
    '<p class="cal-sestavy-pozn">Předběžná opatření (rejstřík Nc) se berou u předsedů ' +
    "těchto senátů. Žaloby proti Úřadu průmyslového vlastnictví (úsek správního " +
    "soudnictví Městského soudu) se berou v kterémkoli senátu – podle žalovaného.</p>" +
    "</div>";
}

// Sbalí/rozbalí seznam změn – stejný vzor jako setDigestFolded() u přehledu.
function setZmenyFolded(folded) {
  const head = document.getElementById("zmeny-fold");
  const content = document.getElementById("zmeny-content");
  if (!head || !content) return;
  content.hidden = folded;
  head.setAttribute("aria-expanded", String(!folded));
  head.setAttribute("aria-label", folded ? "Rozbalit změny" : "Sbalit změny");
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
    "</div>" +
    zmenyHtml() +
    sestavyHtml();

  const info = document.getElementById("cal-info");
  if (info && data.generated) {
    const kdy = czDate(data.generated);
    if (kdy) info.textContent = "aktualizováno " + kdy;
  }

  const zmenyHead = document.getElementById("zmeny-fold");
  if (zmenyHead) {
    setZmenyFolded(true);
    zmenyHead.addEventListener("click", () =>
      setZmenyFolded(zmenyHead.getAttribute("aria-expanded") === "true"));
    zmenyHead.addEventListener("keydown", e => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      setZmenyFolded(zmenyHead.getAttribute("aria-expanded") === "true");
    });
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
  const digestHead = document.querySelector("#dvatydny .card-header");
  if (digestHead) {
    digestHead.addEventListener("click", () => setDigestFolded(!isDigestFolded()));
  }
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
    vykresliZdroje();
    vykresliNastaveni();
  }
  vykresliUcet();
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
    el.innerHTML = '<a class="ucet-odkaz" href="#nastaveni">Můj výběr</a>' +
      '<span class="ucet-user"></span>';
    clerk.mountUserButton(el.querySelector(".ucet-user"));
  } else if (!el.querySelector(".ucet-prihlasit")) {
    if (tlacitko) clerk.unmountUserButton(tlacitko);
    el.innerHTML = '<button type="button" class="ucet-btn ucet-prihlasit">Přihlásit se</button>' +
      '<button type="button" class="ucet-btn ucet-btn-hlavni ucet-registrace">Registrace</button>';
  }
}

/* ========== Nastavení výběru (#nastaveni) ========== */

const KOLEGIA = { civilni: "Občanskoprávní a obchodní kolegium", trestni: "Trestní kolegium" };

// Senáty NS k výběru: ze seznamu a navíc ty, které jsou v datech a v seznamu
// chybí (aspoň s číslem).
function senatyKVyberu() {
  const znam = SENATY_NS.map(s => s.senat);
  const r = zdrojeVysledky && zdrojeVysledky[FEEDS.findIndex(f => f.key === "nsoud")];
  const vDatech = r && r.status === "fulfilled" ? r.value.map(i => i.senat) : [];
  const navic = bezDuplicit(vDatech.concat(vyber.ns.senaty))
    .filter(n => Number.isInteger(n) && znam.indexOf(n) < 0)
    .sort((a, b) => a - b)
    .map(n => ({ senat: n, kolegium: "ostatni", popis: "" }));
  return SENATY_NS.concat(navic);
}

// Časopisy k výběru: štítky z feedu a navíc skryté, které teď ve feedu nejsou.
function casopisyKVyberu() {
  const r = zdrojeVysledky && zdrojeVysledky[FEEDS.findIndex(f => f.key === "journals")];
  const vDatech = r && r.status === "fulfilled" ? r.value.map(i => tagOf(i.title)) : [];
  return bezDuplicit(vDatech.concat(vyber.skryte_casopisy)).filter(Boolean)
    .sort((a, b) => a.localeCompare(b, "cs"));
}

function zaskrtavatko(atributy, zaskrtnuto, popisek) {
  const attrs = Object.keys(atributy).map(k => " " + k + '="' + esc(atributy[k]) + '"').join("");
  return '<label class="volba"><input type="checkbox"' + attrs + (zaskrtnuto ? " checked" : "") +
    "> <span>" + popisek + "</span></label>";
}

function senatyHtml() {
  const skupiny = {};
  senatyKVyberu().forEach(s => { (skupiny[s.kolegium] = skupiny[s.kolegium] || []).push(s); });
  return Object.keys(skupiny).map(k =>
    '<div class="volby-skupina"><h4>' + esc(KOLEGIA[k] || "Další senáty") + "</h4>" +
    skupiny[k].map(s => zaskrtavatko({ "data-senat": s.senat }, vyber.ns.senaty.indexOf(s.senat) >= 0,
      "<b>" + s.senat + "</b>" + (s.popis ? " " + esc(s.popis) : ""))).join("") +
    "</div>").join("");
}

// Matice oblastí × soudy (mřížka, ne tabulka – na tabulky se na úzkých
// oknech vztahuje blokové rozložení seznamů). Hlavička sloupce a řádek
// skupiny přepínají celý sloupec, resp. skupinu; stav „částečně" doplní
// obnovStavySkupin. Při jediném soudu přepíná zaškrtávátko i název oblasti.
function oblastiHtml() {
  const soudy = SOUDY_VYBERU;
  const skupiny = [];
  OBLASTI_SEZNAM.forEach(o => {
    let sk = skupiny.find(x => x.nazev === o.skupina);
    if (!sk) skupiny.push(sk = { nazev: o.skupina, oblasti: [] });
    sk.oblasti.push(o);
  });
  let html = '<div class="matice" style="--sloupcu:' + soudy.length + '">' +
    '<div class="matice-hlava">Oblast</div>' +
    soudy.map(s => '<label class="matice-hlava matice-bunka"><input type="checkbox" data-sloupec="' +
      s.soud + '"> ' + esc(s.nazev) + "</label>").join("");
  skupiny.forEach((sk, i) => {
    html += '<div class="matice-skupina">' + esc(sk.nazev) + "</div>" +
      soudy.map(s => '<div class="matice-skupina matice-bunka"><input type="checkbox" data-skupina="' + i +
        '" data-soud="' + s.soud + '" aria-label="' + esc(sk.nazev + " – " + s.nazev) + '"></div>').join("");
    sk.oblasti.forEach(o => {
      const id = s => "ob-" + s.soud + "-" + o.id;
      html += (soudy.length === 1
        ? '<label class="matice-oblast" for="' + id(soudy[0]) + '"'
        : '<div class="matice-oblast"') + ' title="' + esc(o.popis || "") + '">' + esc(o.nazev) +
        (soudy.length === 1 ? "</label>" : "</div>") +
        soudy.map(s => '<div class="matice-bunka"><input type="checkbox" id="' + id(s) + '" data-oblast="' +
          esc(o.id) + '" data-soud="' + s.soud + '" data-skupina-oblasti="' + i + '"' +
          (vyber[s.soud].oblasti.indexOf(o.id) >= 0 ? " checked" : "") +
          ' aria-label="' + esc(o.nazev + " – " + s.nazev) + '"></div>').join("");
    });
  });
  return html + "</div>";
}

// Zaškrtávátka skupin a sloupců: zaškrtnuté, prázdné, nebo „částečně".
function obnovStavySkupin(el) {
  function nastav(cil, polozky) {
    const vybrano = polozky.filter(x => x.checked).length;
    cil.checked = vybrano > 0 && vybrano === polozky.length;
    cil.indeterminate = vybrano > 0 && vybrano < polozky.length;
  }
  el.querySelectorAll("input[data-skupina]").forEach(cil => {
    nastav(cil, Array.from(el.querySelectorAll('input[data-skupina-oblasti="' + cil.dataset.skupina +
      '"][data-soud="' + cil.dataset.soud + '"]')));
  });
  el.querySelectorAll("input[data-sloupec]").forEach(cil => {
    nastav(cil, Array.from(el.querySelectorAll('input[data-oblast][data-soud="' + cil.dataset.sloupec + '"]')));
  });
}

const POPIS_VYCHOZIHO = "Bez přihlášení web ukazuje výchozí výběr: u Nejvyššího soudu všechna " +
  "rozhodnutí senátu 23 Cdo a z ostatních senátů ta, která AI zařadila do oblastí duševního " +
  "vlastnictví a IT; všechny časopisy.";

function vykresliNastaveni() {
  const el = document.getElementById("nastaveni-obsah");
  if (!el) return;
  if (clerkStav === "nacitam") {
    el.innerHTML = '<p class="loading">Načítám...</p>';
    return;
  }
  if (clerkStav === "nedostupny") {
    el.innerHTML = '<p class="feed-empty">Přihlášení teď není k dispozici. ' + POPIS_VYCHOZIHO + "</p>";
    return;
  }
  if (!prihlaseny) {
    el.innerHTML =
      '<p class="nastaveni-uvod">Po přihlášení si vyberete, co chcete sledovat: senáty Nejvyššího ' +
      "soudu, oblasti práva, jestli skrýt rutinní procesní rozhodnutí a které časopisy. Výběr se " +
      "uloží k účtu a platí na všech zařízeních.</p>" +
      '<p class="nastaveni-uvod">' + POPIS_VYCHOZIHO + "</p>" +
      '<p class="nastaveni-akce"><button type="button" class="ucet-btn ucet-prihlasit">Přihlásit se</button>' +
      '<button type="button" class="ucet-btn ucet-btn-hlavni ucet-registrace">Registrace</button></p>' +
      '<p class="nastaveni-pozn">Přihlášení zajišťuje služba Clerk. Ukládá e-mailovou adresu a váš ' +
      "výběr, nic dalšího.</p>";
    return;
  }
  const casopisy = casopisyKVyberu();
  el.innerHTML =
    '<p class="nastaveni-uvod">Rozhodnutí Nejvyššího soudu uvidíte, když je z vybraného senátu, ' +
    "nebo když ho AI zařadila do některé z vybraných oblastí. Změny se ukládají hned. Dvoutýdenní " +
    "přehled zatím vychází z výchozího výběru.</p>" +
    '<h3 class="nastaveni-nadpis">Senáty Nejvyššího soudu</h3>' +
    '<div class="volby">' + senatyHtml() + "</div>" +
    '<h3 class="nastaveni-nadpis">Oblasti práva</h3>' + oblastiHtml() +
    '<h3 class="nastaveni-nadpis">Procesní rozhodnutí</h3>' +
    zaskrtavatko({ "data-procesni": "1" }, vyber.skryt_procesni,
      "Skrýt rutinní procesní rozhodnutí (odmítnutí bez věcného posouzení, zastavení, " +
      "příslušnost, poplatky)") +
    (casopisy.length ? '<h3 class="nastaveni-nadpis">Časopisy</h3><div class="volby volby-radek">' +
      casopisy.map(c => zaskrtavatko({ "data-casopis": c }, vyber.skryte_casopisy.indexOf(c) < 0, esc(c)))
        .join("") + "</div>" : "") +
    '<p class="nastaveni-akce"><button type="button" class="ucet-btn" id="vyber-vychozi">' +
    'Obnovit výchozí výběr</button> <span class="legend" id="vyber-stav" role="status"></span></p>';
  obnovStavySkupin(el);
}

// Výběr z formuláře (co je zaškrtnuté, to platí).
function vyberZFormulare(el) {
  const v = JSON.parse(JSON.stringify(vyber));
  v.ns.senaty = Array.from(el.querySelectorAll("input[data-senat]:checked")).map(i => Number(i.dataset.senat));
  SOUDY_VYBERU.forEach(s => {
    v[s.soud].oblasti = Array.from(el.querySelectorAll('input[data-oblast][data-soud="' + s.soud + '"]:checked'))
      .map(i => i.dataset.oblast);
  });
  const procesni = el.querySelector("input[data-procesni]");
  if (procesni) v.skryt_procesni = procesni.checked;
  v.skryte_casopisy = Array.from(el.querySelectorAll("input[data-casopis]"))
    .filter(i => !i.checked).map(i => i.dataset.casopis);
  return v;
}

let ulozCasovac = null;

// Ukládá se se zpožděním, ať série kliknutí skončí jedním zápisem. Výchozí
// výběr se neukládá – z účtu se `owl` smaže.
function ulozVyber() {
  const stav = document.getElementById("vyber-stav");
  if (stav) stav.textContent = "Ukládám…";
  clearTimeout(ulozCasovac);
  ulozCasovac = setTimeout(() => {
    const user = clerk && clerk.user;
    if (!user) return;
    const meta = Object.assign({}, user.unsafeMetadata);
    if (jeVychozi(vyber)) delete meta.owl;
    else meta.owl = vyber;
    user.update({ unsafeMetadata: meta }).then(
      () => { const s = document.getElementById("vyber-stav"); if (s) s.textContent = "Uloženo"; },
      () => { const s = document.getElementById("vyber-stav"); if (s) s.textContent = "Nepodařilo se uložit – zkuste to znovu."; }
    );
  }, 600);
}

function initNastaveni() {
  const el = document.getElementById("nastaveni-obsah");
  document.addEventListener("click", e => {
    if (e.target.closest(".ucet-prihlasit")) prihlasit(false);
    else if (e.target.closest(".ucet-registrace")) prihlasit(true);
  });
  if (!el) return;
  el.addEventListener("change", e => {
    const t = e.target;
    if (t.dataset.skupina !== undefined) {
      el.querySelectorAll('input[data-skupina-oblasti="' + t.dataset.skupina + '"][data-soud="' +
        t.dataset.soud + '"]').forEach(i => { i.checked = t.checked; });
    } else if (t.dataset.sloupec !== undefined) {
      el.querySelectorAll('input[data-oblast][data-soud="' + t.dataset.sloupec + '"]')
        .forEach(i => { i.checked = t.checked; });
    }
    obnovStavySkupin(el);
    vyber = vyberZFormulare(el);
    vykresliZdroje();
    ulozVyber();
  });
  el.addEventListener("click", e => {
    if (!e.target.closest("#vyber-vychozi")) return;
    vyber = vychoziVyber();
    vykresliNastaveni();
    vykresliZdroje();
    ulozVyber();
  });
}

/* ========== Stránky a navigace ========== */
// Obsah je rozdělený na stránky; přepíná se podle adresy (#kotva).
// Kotvy sekcí zůstávají platné – odkaz na #nsoud otevře druhou stránku.
const PAGES = [
  { id: "prehled",  sections: ["dnesni", "dvatydny"] },
  { id: "recentni", sections: ["nsoud", "cjeu", "casopisy"] },
  { id: "kalendar", sections: ["jednani"] },
  { id: "nastaveni", sections: ["vyber"] }
];

let currentPage = PAGES[0];
let updateNav = function () {};

function pageOf(hash) {
  const id = String(hash || "").replace(/^#/, "");
  return PAGES.find(p => p.id === id || p.sections.indexOf(id) >= 0) || PAGES[0];
}

// Přepne na stránku a odscrolluje – buď na sekci, nebo na začátek stránky.
function navigate(hash, push) {
  const page = pageOf(hash);
  const id = String(hash || "").replace(/^#/, "");
  currentPage = page;
  PAGES.forEach(p => {
    const el = document.getElementById(p.id);
    if (el) el.hidden = (p !== page);
  });

  const section = page.sections.indexOf(id) >= 0 ? document.getElementById(id) : null;
  // Kdo jde přímo na přehled (třeba uloženým odkazem), chce ho číst.
  if (id === "dvatydny") setDigestFolded(false);
  if (section) section.scrollIntoView({ block: "start" });
  else window.scrollTo(0, 0);

  if (push) history.pushState(null, "", "#" + (id || page.id));
  updateNav();
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
    });
  }

  links.forEach(a => a.addEventListener("click", e => {
    // Skok si řešíme sami – cílová sekce může být na skryté stránce.
    e.preventDefault();
    navigate(a.getAttribute("href"), true);
  }));
  window.addEventListener("popstate", () => navigate(location.hash, false));
  document.addEventListener("scroll", update, { passive: true });
  updateNav = update;
}

// Datum poslední aktualizace = nejnovější aktualizace ze všech zdrojů
// (<lastBuildDate> u XML feedů, `generated` u JSONu).
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
