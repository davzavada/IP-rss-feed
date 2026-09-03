/* Owl – skript stránky. Čte hotové soubory vedle sebe (feed.xml,
   ipcuria_feed.xml, journals_feed.xml, digest.json, hearings.json,
   hearings.ics) a vykresluje je; nic nepočítá, co si už spočítaly scrapery. */

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
const COL_DEFAULTS = { type: 12, name: 20, heslo: 22, src: 15, date: 10, author: 14 };
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
  const title = esc(text(item, "title").replace(/^\[[^\]]+\]\s*/, ""));
  const href = safeHref(text(item, "link"));
  let html = href ? '<a href="' + href + '">' + title + "</a>" : title;
  // U žádostí o předběžnou otázku ještě odkaz na samotný dokument – shrnutí
  // je jen shrnutí, znění otázek je v něm.
  const doc = safeHref(text(item, "document-url"));
  if (doc) html += ' <a class="doc-link" href="' + doc + '">PDF</a>';
  return html;
}

// Autoři jsou ve feedu v <dc:creator> – čteme je přes jmenný prostor,
// se záložním hledáním podle celého názvu značky (starší prohlížeče).
const DC_NS = "http://purl.org/dc/elements/1.1/";

function authorText(item) {
  const el = item.getElementsByTagNameNS(DC_NS, "creator")[0] ||
    item.getElementsByTagName("dc:creator")[0];
  return el ? el.textContent.trim() : "";
}

function authorCell(item) {
  const authors = authorText(item);
  return authors ? '<span class="author">' + esc(authors) + "</span>" : "";
}

// V přehledu přes všechny zdroje jdou autoři pod název (má je jen část položek).
function nameAuthorCell(item) {
  return nameCell(item) + authorCell(item);
}

function hesloCell(item) {
  const tag = text(item, "ai-tag");
  return tag ? '<span class="heslo" title="' + esc(tag) + '">' + esc(tag) + "</span>" : "";
}

function summaryCell(item) {
  const summary = text(item, "ai-summary");
  if (summary) return '<span class="summary">' + esc(summary) + "</span>";
  // Bez shrnutí ještě může být poznámka, proč žádné není – třeba že u žádosti
  // o předběžnou otázku zatím nejsou zveřejněné otázky.
  const note = text(item, "note");
  return note ? '<span class="summary note">' + esc(note) + "</span>" : "";
}

function dateCell(item) {
  return czDate(text(item, "pubDate"));
}

function typeCell(item) {
  return tagBadge(tagOf(text(item, "title")));
}

// Definice sloupců sdílíme mezi živým feedem a novými položkami.
const colsNsoud = [
  { label: "Název", cls: "col-name", render: nameCell },
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

const FEEDS = [
  { key: "nsoud",    label: "NS 23 Cdo", url: "feed.xml",          cols: colsNsoud,    containerId: "feed-nsoud" },
  { key: "cjeu",     label: "CJEU",      url: "ipcuria_feed.xml",  cols: colsCjeu,     containerId: "feed-cjeu" },
  { key: "journals", label: "Časopis",   url: "journals_feed.xml", cols: colsJournals, containerId: "feed-journals" }
];

function resizerHtml(column) {
  return '<span class="col-resizer" role="separator" aria-orientation="vertical"' +
    ' tabindex="0" aria-label="Šířka sloupce ' + esc(column.label) + '"' +
    ' title="Táhnutím změníte šířku sloupce, dvojklikem obnovíte výchozí"></span>';
}

function renderTable(items, container, columns, key) {
  if (items.length === 0) {
    container.innerHTML = '<p class="feed-empty">Žádné nové položky.</p>';
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
      if (item.querySelector("is-new")) {
        item._src = FEEDS[idx].key;
        item._srcLabel = FEEDS[idx].label;
        today.push(item);
      }
    });
  });
  today.sort((a, b) => new Date(text(b, "pubDate")) - new Date(text(a, "pubDate")));
  if (today.length === 0) {
    container.innerHTML = '<p class="feed-empty">Za posledních 24 hodin nic nepřibylo. ' +
      "Feedy se obnovují ráno v 7:00 a odpoledne ve 14:00.</p>";
  } else {
    renderTable(today, container, colsToday, "today");
  }
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
// Kolik štítků se vejde do dne, než se zbytek schová pod „+N další".
const CAL_MAX_CHIPS = 4;
// Kalendář neukazuje měsíc, ale okno šesti týdnů: dva zpět (čerstvá
// minulost je pořád zajímavá) a čtyři dopředu, ať se vejde všechno, co
// soudy stihly vypsat. Šipky posouvají o dva týdny.
const CAL_TYDNU = 6;
const CAL_POSUN_DNU = 14;

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

// Výchozí okno: dva týdny zpět od pondělí tohoto týdne.
function calDefaultStart() {
  const p = mondayOf(new Date());
  p.setDate(p.getDate() - 14);
  return p;
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

// Na telefonu se mřížka šesti týdnů nedá číst – vedle ní se proto vykreslí
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
    events.slice(0, CAL_MAX_CHIPS).forEach((j, idx) => {
      html += chipHtml(j, iso, idx);
    });
    // „+2 další" otevře bublinu s celým dnem, ať se ke skrytým jde dostat.
    if (events.length > CAL_MAX_CHIPS) {
      html += '<button type="button" class="cal-more" data-date="' + iso +
        '" data-idx="-1" aria-expanded="false">+' +
        (events.length - CAL_MAX_CHIPS) + " další</button>";
    }
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

// Seznam změn pod mřížkou – kvůli němu se přehledy porovnávají.
function zmenyHtml() {
  const zmeny = (calData.zmeny || []).filter(z => z && z.spz && z.typ);
  let html = '<div class="cal-zmeny"><h3>Změny v přehledech soudů</h3>';
  if (!zmeny.length) {
    return html + '<p class="feed-empty">Od minulých přehledů se nic nezměnilo.</p></div>';
  }
  html += "<ul>";
  zmeny.forEach(z => {
    html += "<li>" +
      '<span class="cal-zmena-kdy">' + esc(czDate(z.kdy)) + "</span>" +
      courtBadge(z) +
      '<span class="cal-zmena-spz">' + esc(z.spz) + "</span>" +
      '<span class="cal-zmena">' + esc(zmenaText(z)) + "</span>" +
      '<span class="cal-zmena-kdy">jednání ' + esc(czDate(z.datum)) + "</span>" +
      "</li>";
  });
  return html + "</ul></div>";
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
        '<button type="button" id="cal-prev" aria-label="O dva týdny zpět">‹</button>' +
        '<span class="cal-title" id="cal-title"></span>' +
        '<button type="button" id="cal-next" aria-label="O dva týdny vpřed">›</button>' +
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
    zmenyHtml();

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
  const digestHead = document.querySelector("#dvatydny .card-header");
  if (digestHead) {
    digestHead.addEventListener("click", () => setDigestFolded(!isDigestFolded()));
  }
}

/* ========== Stránky a navigace ========== */
// Obsah je rozdělený na stránky; přepíná se podle adresy (#kotva).
// Kotvy sekcí zůstávají platné – odkaz na #nsoud otevře druhou stránku.
const PAGES = [
  { id: "prehled",  sections: ["dnesni", "dvatydny"] },
  { id: "recentni", sections: ["nsoud", "cjeu", "casopisy"] },
  { id: "kalendar", sections: ["jednani"] }
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
  // Kdo jde přímo na přehled (třeba odkazem z newsletteru), chce ho číst.
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

// Datum poslední aktualizace = nejnovější <lastBuildDate> ze všech feedů.
function showUpdated(docPromises) {
  Promise.all(docPromises.map(p => p.then(
    doc => {
      const d = new Date((doc.querySelector("lastBuildDate") || {}).textContent || "");
      return isNaN(d) ? null : d;
    },
    () => null
  ))).then(dates => {
    const latest = dates.filter(Boolean).sort((a, b) => b - a)[0];
    if (!latest) return;
    document.getElementById("updated").textContent =
      "aktualizováno " + latest.toLocaleDateString("cs-CZ") + " " +
      latest.toLocaleTimeString("cs-CZ", { hour: "2-digit", minute: "2-digit" });
  });
}

function initApp() {
  // Prohlížeč se po refreshi snaží obnovit pozici scrollu, jenže obsah tu
  // dorazí až po fetchi – stránka by se pod ním nafoukla a poskočila.
  // Necháme si scroll pod kontrolou a začínáme nahoře.
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";

  // Každý feed stáhneme jen jednou a sdílíme mezi sekcemi (všechno / nové).
  const feedDocs = FEEDS.map(f => fetchFeed(f.url));
  const feedPromises = feedDocs.map(
    p => p.then(doc => Array.from(doc.querySelectorAll("item")))
  );
  // Dvoutýdenní přehled a kalendář se generují zvlášť – když chybí, jen se
  // nevykreslí; zbytek stránky na ně nečeká déle než na feedy.
  const digestPromise = fetchJson("digest.json").catch(() => null);
  const hearingsPromise = fetchJson("hearings.json").catch(() => null);
  initNav();
  initHelp();
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
      FEEDS.forEach((f, idx) => {
        if (results[idx].status === "rejected") {
          failed(f.containerId);
          return;
        }
        // Druhá stránka je úplný výpis za okno feedu – nové položky z ní
        // nevynecháváme, na jednu stránku se položky nedostanou dvakrát.
        renderTable(results[idx].value, document.getElementById(f.containerId), f.cols, f.key);
      });
      renderToday(results);
      renderDigest(digest);
      renderKalendar(hearings);
      // Až teď je jasná výška stránky – otevřeme kotvu z adresy (a přepočítáme
      // zvýraznění) ještě než stránku odkryjeme, ať nic nepřeskočí.
      navigate(location.hash, false);
      document.documentElement.classList.remove("is-loading");
    });

  showUpdated(feedDocs);
}

initApp();
