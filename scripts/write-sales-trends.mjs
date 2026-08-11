import { loadDotEnv } from "./lib/env.mjs";
import {
  batchUpdateValues,
  columnLetter,
  getValues,
  quoteSheetName,
} from "./lib/google-sheets.mjs";

await loadDotEnv();

const BASE_URL = process.env.TRAY_API_BASE_URL ?? "https://api-hq.vendsy.com";
const API_KEY = process.env.TRAY_API_KEY;
const spreadsheetId = process.env.SALES_TRENDS_SPREADSHEET_ID;

// ── Store-to-tab mapping ──────────────────────────────────────────────────────

const stores = {
  cherrywood: { name: "Cherrywood",  siteId: "589", tab: "Cherrywood Monthly Sales ", phoneAi: "UrbanPiper" },
  arbor:       { name: "Arbor",      siteId: "590", tab: "Arbor Monthly Sales",        phoneAi: "AIAssistant.co" },
  tsoco:       { name: "TsoCo",      siteId: "586", tab: "TsoCo Monthly Sales",        phoneAi: "Voicify" },
  "round-rock":{ name: "Round Rock", siteId: "591", tab: "Round Rock Monthly Sales",   phoneAi: "AIAssistant.co" },
  menchaca:    { name: "Menchaca",   siteId: "514", tab: "Menchaca Monthly Sales",     phoneAi: "AIAssistant.co" },
};

// ── Column layout ─────────────────────────────────────────────────────────────
// Verified from live sheet (header row 1)
// A=DATE, B=(blank), C=Days in Month, D=Total Tix, E=Avg Tix/Day,
// F=Total AOV, G=1P Sales, H=% 1P, I=TsoGiving Donation, J=TsoGiving Sales,
// K=1P-Carryout Sales, L=% Carryout, M=Carryout Tix,
// N=1P-Delivery Sales, O=% Delivery, P=Delivery Tix,
// Q=3P Sales, R=% 3P, S=3P Tix, T=Avg 3P Ticket,
// U=1P-Kiosk Sales, V=% Kiosk, W=Kiosk Tix

// ── Tray API helpers ──────────────────────────────────────────────────────────

function asArray(payload, key) {
  if (Array.isArray(payload)) return payload;
  if (payload?.[key] && Array.isArray(payload[key])) return payload[key];
  if (payload?.data && Array.isArray(payload.data)) return payload.data;
  return [];
}

async function fetchJson(path, params = {}) {
  const url = new URL(path, BASE_URL);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  const res = await fetch(url, {
    headers: { accept: "application/json", authorization: API_KEY },
  });
  const text = await res.text();
  if (res.ok) return text ? JSON.parse(text) : {};
  throw new Error(`Tray failed ${path}: ${res.status} ${text.slice(0, 300)}`);
}

function normalizeServiceName(name) {
  const map = new Map([
    ["ubereats", "Uber Eats"], ["doordash", "DoorDash"],
    ["grubhub", "Grubhub"], ["favor", "Favor"],
    ["7now", "7NOW"], ["kiosktakeout", "Kiosk Take Out"],
    ["tsochinese.comtakeout", "tsochinese.com Take Out"],
    ["tsochinese.comdelivery", "tsochinese.com Delivery"],
    ["classpass", "ClassPass"], ["questom", "Questom"],
  ]);
  const key = String(name ?? "").trim().toLowerCase().replaceAll(/[\s_-]/g, "");
  return map.get(key) ?? String(name ?? "").trim();
}

// ── Daily Tray fetch ──────────────────────────────────────────────────────────

async function fetchDay(siteId, date) {
  const [rcResp, itemsResp, checksResp] = await Promise.all([
    fetchJson("/v1/revenueCenters", { siteId }),
    fetchJson("/v2/items", { siteId, date }),
    fetchJson("/v3/checks", { siteId, date }),
  ]);

  const rcByEid = new Map(
    asArray(rcResp, "revenueCenters").map((c) => [c.eid, c]),
  );
  const checks = asArray(checksResp, "checks");
  const checksById = new Map(checks.map((c) => [c.id, c]));
  const groups = new Map();

  for (const item of asArray(itemsResp, "items")) {
    const check = checksById.get(item.checkId);
    if (!check) continue;
    const rc = rcByEid.get(check.revenueCenterEid);
    const svc = normalizeServiceName(rc?.name ?? `eid:${check.revenueCenterEid}`);
    if (!groups.has(svc)) groups.set(svc, { grossSales: 0, checkIds: new Set() });
    const g = groups.get(svc);
    g.checkIds.add(item.checkId);
    if (Number(item.lineType) === 5 && !item.isModifier && !item.voided) {
      g.grossSales += Number(item.grossPrice ?? 0);
    }
  }

  return new Map(
    [...groups].map(([name, g]) => [name, {
      grossSales: Number(g.grossSales.toFixed(2)),
      checks: g.checkIds.size,
    }]),
  );
}

// ── Aggregate one month ───────────────────────────────────────────────────────

function daysInMonth(ym) {
  return new Date(Number(ym.slice(0, 4)), Number(ym.slice(5, 7)), 0).getDate();
}

async function aggregateMonth(siteId, ym) {
  const year  = Number(ym.slice(0, 4));
  const month = Number(ym.slice(5, 7));
  const totalDays = daysInMonth(ym);
  const all = { grossSales: 0, checks: 0 };
  const byService = new Map();

  for (let d = 1; d <= totalDays; d++) {
    const date = `${year}-${String(month).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    const dayData = await fetchDay(siteId, date);
    if (dayData.size === 0) continue;

    for (const [svc, data] of dayData) {
      if (!byService.has(svc)) byService.set(svc, { grossSales: 0, checks: 0 });
      const acc = byService.get(svc);
      acc.grossSales  += data.grossSales;
      acc.checks      += data.checks;
    }

    const oneP = ["tsochinese.com Take Out", "tsochinese.com Delivery", "Kiosk Take Out"];
    for (const svc of oneP) {
      const data = dayData.get(svc);
      if (data) {
        all.grossSales += data.grossSales;
        all.checks     += data.checks;
      }
    }
  }

  return { totals: all, byService, daysWithData: totalDays };
}

// ── Sheet helpers ─────────────────────────────────────────────────────────────

function displayMonth(ym) {
  const m = Number(ym.slice(5, 7));
  const y = ym.slice(0, 4);
  return `${m}.${y}`;
}

async function findMonthRow(tab, ym) {
  const expected = displayMonth(ym);
  const rows = await getValues(spreadsheetId, `${quoteSheetName(tab)}!A:A`);
  for (let i = 0; i < rows.length; i++) {
    if (String(rows[i]?.[0] ?? "").trim() === expected) {
      return i + 1;
    }
  }
  return null;
}

// ── Compute row values from Tray data ─────────────────────────────────────────

function computeRowData(monthly, phoneAiName) {
  const bs = monthly.byService;
  const takeout = bs.get("tsochinese.com Take Out") || { grossSales: 0, checks: 0 };
  const delivery = bs.get("tsochinese.com Delivery") || { grossSales: 0, checks: 0 };
  const kiosk = bs.get("Kiosk Take Out") || { grossSales: 0, checks: 0 };

  const onePSales = takeout.grossSales + delivery.grossSales + kiosk.grossSales;
  const onePTix   = takeout.checks + delivery.checks + kiosk.checks;

  // 3P = everything that is not 1P and not phone AI
  const threeP = { grossSales: 0, checks: 0 };
  for (const [svc, data] of bs) {
    if (svc !== "tsochinese.com Take Out" && svc !== "tsochinese.com Delivery" && svc !== "Kiosk Take Out"
        && svc !== phoneAiName) {
      threeP.grossSales += data.grossSales;
      threeP.checks += data.checks;
    }
  }

  // Phone AI only
  const phoneAi = bs.get(phoneAiName) || { grossSales: 0, checks: 0 };

  const totalSales = onePSales + threeP.grossSales;

  return {
    onePSales,
    onePTix,
    carryoutSales: takeout.grossSales,
    carryoutTix:   takeout.checks,
    deliverySales: delivery.grossSales,
    deliveryTix:   delivery.checks,
    kioskSales:    kiosk.grossSales,
    kioskTix:      kiosk.checks,
    threePSales:   threeP.grossSales,
    threePTix:     threeP.checks,
    phoneAiSales:  Number(phoneAi.grossSales.toFixed(2)),
    phoneAiTix:    phoneAi.checks,
    threePAvg:     threeP.checks > 0 ? threeP.grossSales / threeP.checks : 0,
    pctCarryout:   onePSales > 0 ? (takeout.grossSales / onePSales * 100) : 0,
    pctDelivery:   onePSales > 0 ? (delivery.grossSales / onePSales * 100) : 0,
    pctKiosk:      onePSales > 0 ? (kiosk.grossSales / onePSales * 100) : 0,
    pctThreeP:     totalSales > 0 ? (threeP.grossSales / totalSales * 100) : 0,
    avgTixDay:     onePTix > 0 ? onePTix / daysInMonth("2026-01") : 0,
    aov:           onePTix > 0 ? onePSales / onePTix : 0,
  };
}

// ── CLI parsing ───────────────────────────────────────────────────────────────

function usage() {
  console.error("Usage: node write-sales-trends.mjs --month YYYY-MM [--stores s1,s2,...] [--write]");
  console.error("Stores: arbor, cherrywood, menchaca, round-rock, tsoco (default: all)");
  process.exit(1);
}

const args = { month: "", stores: [], write: false };
const argv = process.argv.slice(2);
for (let i = 0; i < argv.length; i++) {
  if (argv[i] === "--write") { args.write = true; continue; }
  if (argv[i] === "--month") { args.month = argv[++i]; continue; }
  if (argv[i] === "--stores") {
    args.stores = argv[++i].split(",").map((s) => s.trim()).filter(Boolean);
    continue;
  }
}
if (!/^\d{4}-\d{2}$/.test(args.month)) usage();

const selectedStores = args.stores.length > 0
  ? args.stores.map((s) => {
      const key = s.trim().toLowerCase().replace(/[\s_-]/g, "");
      const alias = new Map([["rr", "round-rock"], ["roundrock", "round-rock"]]);
      const store = stores[alias.get(key) ?? key];
      if (!store) throw new Error(`Unknown store "${s}". Options: ${Object.keys(stores).join(", ")}`);
      return store;
    })
  : Object.values(stores);

// ── Main ──────────────────────────────────────────────────────────────────────

if (!API_KEY) throw new Error("Missing TRAY_API_KEY");
if (!spreadsheetId) throw new Error("Missing SALES_TRENDS_SPREADSHEET_ID");

console.log(`\nSales & Trends — ${args.write ? "Writing" : "Dry run"} for ${args.month} (${displayMonth(args.month)})`);
console.log("─".repeat(60));

for (const store of selectedStores) {
  try {
    await processStore(store, args);
  } catch (err) {
    console.error(`   ❌ ${store.name}: ${err.message}`);
  }
}

async function processStore(store, args) {
  console.log(`\n📍 ${store.name} (site ${store.siteId}) — tab: "${store.tab}"`);

  const monthRow = await findMonthRow(store.tab, args.month);
  if (monthRow === null) {
    console.log(`   ⚠ No row found for ${displayMonth(args.month)} — would need to append`);
    return;
  }
  console.log(`   Found row ${monthRow}`);

  console.log(`   Fetching ${daysInMonth(args.month)} days from Tray...`);
  const monthly = await aggregateMonth(store.siteId, args.month);
  console.log(`   Tray services found: ${[...monthly.byService.keys()].join(", ") || "(none)"}`);
  console.log(`   Days with data: ${monthly.daysWithData}`);

  const rowData = computeRowData(monthly, store.phoneAi);

  console.log(`   1P Sales: $${rowData.onePSales.toFixed(2)} | 1P Tix: ${rowData.onePTix}`);
  console.log(`     Carryout: $${rowData.carryoutSales.toFixed(2)} | ${rowData.carryoutTix} tix`);
  console.log(`     Delivery: $${rowData.deliverySales.toFixed(2)} | ${rowData.deliveryTix} tix`);
  console.log(`     Kiosk:    $${rowData.kioskSales.toFixed(2)} | ${rowData.kioskTix} tix`);
  console.log(`     Phone AI: $${rowData.phoneAiSales.toFixed(2)} | ${rowData.phoneAiTix} tix`);
  console.log(`   3P Sales: $${rowData.threePSales.toFixed(2)} | ${rowData.threePTix} tix`);

  if (args.write) {
    // Fetch existing row — read up to AE (col index 30) to check what's filled
    const existing = await getValues(spreadsheetId,
      `${quoteSheetName(store.tab)}!A${monthRow}:AE${monthRow}`);

    // Only write to cells that are (a) empty AND (b) our Tray value is non-zero
    const values = new Array(31).fill("");
    const ex = existing?.[0] ?? [];

    // G=1P Sales (index 6) — was written by mistake previously, now skip (done elsewhere)
    // AB=Phone AI Sales (index 27)
    if (isEmpty(ex[27]) && rowData.phoneAiSales > 0) values[27] = rowData.phoneAiSales;
    // AD=Phone AI Tix (index 29)
    if (isEmpty(ex[29]) && rowData.phoneAiTix > 0) values[29] = rowData.phoneAiTix;

    // Build column ranges for each non-empty value
    const updates = [];
    const writeCols = {
      AB: 27, AD: 29,
    };
    for (const [colLetter, idx] of Object.entries(writeCols)) {
      if (values[idx] !== "") {
        updates.push({
          range: `${quoteSheetName(store.tab)}!${colLetter}${monthRow}`,
          values: [[values[idx]]],
        });
      }
    }

    if (updates.length > 0) {
      const result = await batchUpdateValues(spreadsheetId, updates);
      console.log(`   ✅ Wrote ${result.totalUpdatedCells} cell(s)`);
    } else {
      console.log(`   ℹ️ All target columns already filled — nothing to write`);
    }
  }
}

if (!args.write) {
  console.log(`\nNo cells were written. Re-run with --write to update the sheet.`);
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function isEmpty(v) {
  return v === "" || v === undefined || v === null || v === 0 || v === "0" || v === "#DIV/0!";
}

function roundPct(v) {
  return `${Number(v).toFixed(1)}%`;
}