import { pathToFileURL } from "node:url";
import {
  batchUpdateValues,
  getValues,
  quoteSheetName,
} from "./lib/google-sheets.mjs";

const STORE_TABS = [
  "Cherrywood Monthly Sales ",
  "Arbor Monthly Sales",
  "TsoCo Monthly Sales",
  "Round Rock Monthly Sales",
  "Menchaca Monthly Sales",
];

export function centralDate(asOf = new Date()) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Chicago",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(asOf);
  const get = (type) => Number(parts.find((part) => part.type === type)?.value);
  return { year: get("year"), month: get("month"), day: get("day") };
}

export function daysInMonth(year, month) {
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}

export function completedDays(asOf) {
  const date = typeof asOf === "string" ? parseIsoDate(asOf) : asOf;
  const { year, month, day } = centralDate(date);
  return Math.min(Math.max(day - 1, 0), daysInMonth(year, month));
}

export function monthLabel(year, month) {
  return `${month}.${year}`;
}

export function parseMonthLabel(value) {
  const match = String(value ?? "").trim().match(/^(\d{1,2})\.(\d{4})$/);
  if (!match) return null;
  return { month: Number(match[1]), year: Number(match[2]) };
}

export function parseIsoDate(value) {
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match) throw new Error("--as-of must be YYYY-MM-DD");
  return new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]), 12));
}

function sameNumber(left, right) {
  return Number(String(left ?? "").replace(/[$,\s]/g, "")) === Number(right);
}

export function buildUpdates(tabRows, asOf) {
  const date = typeof asOf === "string" ? parseIsoDate(asOf) : asOf;
  const { year, month } = centralDate(date);
  const previous = month === 1 ? { year: year - 1, month: 12 } : { year, month: month - 1 };
  const monthTargets = [
    { year, month, targetDays: completedDays(date) },
    { ...previous, targetDays: daysInMonth(previous.year, previous.month) },
  ];
  const updates = [];
  const report = [];

  for (const { tab, rows } of tabRows) {
    const header = String(rows[0]?.[2] ?? "").trim().toLowerCase();
    if (header !== "days in month") {
      throw new Error(`${tab}: C1 is not "Days in Month" (found "${header || "blank"}")`);
    }

    for (const target of monthTargets) {
      const targetLabel = monthLabel(target.year, target.month);
      const rowIndex = rows.findIndex((row, index) => index > 0 && String(row?.[0] ?? "").trim() === targetLabel);
      if (rowIndex === -1) {
        throw new Error(`${tab}: no row found for ${targetLabel}`);
      }

      const rowNumber = rowIndex + 1;
      const current = rows[rowIndex]?.[2] ?? "";
      const changed = !sameNumber(current, target.targetDays);
      if (changed) {
        updates.push({
          range: `${quoteSheetName(tab)}!C${rowNumber}`,
          values: [[target.targetDays]],
        });
      }
      report.push({ tab, rowNumber, month: targetLabel, current, targetDays: target.targetDays, changed });
    }
  }

  return { updates, report };
}

function spreadsheetIdFromEnv() {
  const value = process.env.SALES_TRENDS_SPREADSHEET_ID;
  if (!value) throw new Error("Missing SALES_TRENDS_SPREADSHEET_ID");
  return value;
}

function parseArgs(argv) {
  const args = { write: false, asOf: undefined };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--write") args.write = true;
    else if (argv[i] === "--as-of") args.asOf = argv[++i];
    else throw new Error(`Unknown argument: ${argv[i]}`);
  }
  return args;
}

export async function main(argv = process.argv.slice(2)) {
  const args = parseArgs(argv);
  const asOf = args.asOf ? parseIsoDate(args.asOf) : new Date();
  const spreadsheetId = spreadsheetIdFromEnv();
  const tabRows = [];

  for (const tab of STORE_TABS) {
    const rows = await getValues(spreadsheetId, `${quoteSheetName(tab)}!A1:C20`);
    tabRows.push({ tab, rows });
  }

  const { updates, report } = buildUpdates(tabRows, asOf);
  console.log(`Days in Month — completed Central days: ${completedDays(asOf)}`);
  for (const item of report) {
    console.log(`${item.tab}: row ${item.rowNumber}, ${item.month}, ${item.current || "blank"} -> ${item.targetDays}${item.changed ? "" : " (already current)"}`);
  }

  if (!args.write || updates.length === 0) {
    console.log(args.write ? "No changes needed." : "Dry run only. Re-run with --write to update the sheet.");
    return;
  }

  const result = await batchUpdateValues(spreadsheetId, updates, {
    forceChunk: true,
    chunkSize: 1,
    pauseMs: 1000,
  });
  console.log(`Updated ${result.totalUpdatedCells ?? 0} cell(s).`);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exit(1);
  });
}

export { STORE_TABS };
