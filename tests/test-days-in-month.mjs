import assert from "node:assert/strict";
import test from "node:test";
import {
  buildUpdates,
  completedDays,
  daysInMonth,
  monthLabel,
  parseMonthLabel,
} from "../scripts/update-days-in-month.mjs";

test("completedDays uses completed Central calendar days", () => {
  assert.equal(completedDays("2026-08-13"), 12);
  assert.equal(completedDays("2026-08-01"), 0);
  assert.equal(completedDays("2026-08-31"), 30);
});

test("daysInMonth handles leap years", () => {
  assert.equal(daysInMonth(2028, 2), 29);
  assert.equal(daysInMonth(2027, 2), 28);
});

test("month labels parse and format consistently", () => {
  assert.deepEqual(parseMonthLabel("8.2026"), { month: 8, year: 2026 });
  assert.equal(monthLabel(2026, 8), "8.2026");
});

test("buildUpdates targets only column C on the current month row", () => {
  const tabs = [
    {
      tab: "Arbor Monthly Sales",
      rows: [
        ["DATE", "", "Days in Month"],
        ["", "", ""],
        [8.2026, "", 31],
        [7.2026, "", 30],
      ],
    },
  ];

  const result = buildUpdates(tabs, "2026-08-13");
  assert.deepEqual(result.updates, [
    {
      range: "'Arbor Monthly Sales'!C3",
      values: [[12]],
    },
    {
      range: "'Arbor Monthly Sales'!C4",
      values: [[31]],
    },
  ]);
  assert.equal(result.report[0].rowNumber, 3);
  assert.equal(result.report[0].targetDays, 12);
});

test("buildUpdates is idempotent when the day count is current", () => {
  const result = buildUpdates([
    {
      tab: "TsoCo Monthly Sales",
      rows: [["", "", "Days in Month"], [8.2026, "", 12], [7.2026, "", 31]],
    },
  ], "2026-08-13");
  assert.deepEqual(result.updates, []);
  assert.equal(result.report[0].changed, false);
});

test("buildUpdates rejects a tab whose day-count header moved", () => {
  assert.throws(
    () => buildUpdates([
      { tab: "Bad Tab", rows: [["", "", "Wrong Header"], [8.2026, "", 31]] },
    ], "2026-08-13"),
    /C1 is not "Days in Month"/,
  );
});

test("buildUpdates rejects a missing current-month row", () => {
  assert.throws(
    () => buildUpdates([
      { tab: "Empty Tab", rows: [["", "", "Days in Month"], [7.2026, "", 31]] },
    ], "2026-08-13"),
    /no row found for 8\.2026/,
  );
});
