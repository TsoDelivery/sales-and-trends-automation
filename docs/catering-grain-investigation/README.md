# Catering grain investigation — 2026-08-14

Why the catering columns are sourced from R365 and not the emailed P&L, and what
the reconciliation turned up.

## The question

An earlier ingester read the emailed TRIS P&L and found 26 cells where the P&L
disagreed with the hand-keyed sheet. Which side was right?

## Answer: the sheet was right. The P&L is the wrong grain.

The catering columns hold **calendar-month** revenue. The P&L only reports
**28-day fiscal periods**. Those windows differ by roughly a quarter, so the P&L
is systematically lower — the "disagreements" were an artefact of comparing
different date ranges, not errors in the sheet.

## How it was settled

R365 is the system the P&L is generated *from*, so it can arbitrate. Taking the
same journal lines and summing them two ways:

| Aggregation | Cherrywood Lunchdrop | Matches |
|---|---|---|
| Fiscal P3 2026 (2/22–3/21) | 4,888.10 | the P&L exactly |
| Calendar March 2026 | 6,731.85 | the sheet (6,732) to the cent |

Same result for all five stores. Two further checks agree: the sheet's own
**Days in Month** column reads 31 for row `3.2026` and 28 for `2.2026` — calendar
lengths; and across all populated catering cells, R365-by-calendar-month matches
the sheet on 120 of 154 comparable cells.

## The near-miss worth remembering

A gross-vs-net theory — that the sheet was gross and the P&L net of commission —
fit three stores to within 0.1%. It was a **coincidence**: Lunchdrop's commission
is ~25% and the fiscal-window shortfall is ~27%. Curve-fitting between two
artefacts produced a plausible, confident, wrong answer. Only reconciling against
the generating system exposed it.

**Rule:** when two reports disagree, reconcile against the system that produces
them. Never fit one artefact to the other.

## Reconciling the outliers

With R365 as the source the picture inverted: most remaining disagreements are
errors *in the sheet*, not in the extraction.

> **Superseded.** The table that used to sit here listed "six stale partial-month
> cells". Three of those six were artefacts of the column-mapping bug described
> below, not stale cells. Corrected figures are in *The corrections* section.

**Stale partial-month cells** each match R365 exactly through an early weekly
journal and then stop — keyed before the month's last journal posted. Confirmed
by prefix test:

| Cell | Sheet | Matches R365 through | True total |
|---|---|---|---|
| TsoCo EZCater `6.2026` | 4,630.00 | 3 of 4 weekly journals | 6,648.73 |
| Cherrywood EZCater `6.2026` | 3,142.00 | 3 of 4 | 3,432.75 |
| Arbor EZCater `6.2026` | 2,060.00 | 3 of 4 | 2,275.25 |
| Cherrywood America To Go `12.2025` | 1,033.01 | 1 of 2 | 1,300.16 |

## The Round Rock "America To Go" mystery — it was my bug

Round Rock appeared to have America To Go revenue that R365 had no account for.
The cause: **the catering columns are not in the same order on every tab.**

| Column | Cherrywood / Arbor | Round Rock | TsoCo |
|--------|--------------------|------------|-------|
| BL | My Hot Lunchbox | My Hot Lunchbox | **Try Hungry** |
| BM | **America To Go** | **Try Hungry** | *(none)* |

The first version hardcoded `BM = America To Go` from Cherrywood's layout, so it
compared Round Rock's *Try Hungry* cells against *America To Go* revenue — which
for Round Rock is legitimately zero, because ATG is booked **only to Cherrywood**
company-wide (verified: 4445 is the sole ATG account and every line is Cherrywood).

With columns resolved from each tab's own header row, **Try Hungry matches 7/7**
and **Sharebite 43/43**.

Lunchdrop validating at 64/65 is what hid this: Lunchdrop is column BH on *every*
tab, so the column checked most carefully was the one where the bug was
invisible. **Never infer a spreadsheet's layout from one tab.**

## A second bug the header fix exposed

`4441` (EZCater tax-exempt) was being folded into the EZCater total, but the sheet
has a dedicated **`EZCater (non-Tax)`** column for it. That inflated every EZCater
figure. Split correctly, EZCater (non-Tax) matches 3/3.

## Agreement after both fixes

187 comparable cells: 83 exact, 71 within rounding, **10 mismatches**, 22 blank in
the sheet, 1 present in the sheet but absent from R365.

| Column | Agreement |
|---|---|
| Sharebite | 43/43 100% |
| Try Hungry | 7/7 100% |
| EZCater (non-Tax) | 3/3 100% |
| Lunchdrop | 64/65 98.5% |
| EZCater | 31/37 83.8% |
| America To Go | 6/8 75% |

## The corrections, each diagnosed rather than assumed

| Cause | Evidence | Fixed |
|-------|----------|-------|
| Keyed before the last weekly journal posted | sheet equals an exact **prefix** of the month's journals | yes |
| Last week **double-counted** | Cherrywood Jun Lunchdrop 4,160.00 = full month 3,539.95 **+ 619.95 again** | yes |
| Tax-exempt lumped in by hand | Arbor Sep 2025: sheet 2,750.30 = 4440 (1,415.15) **+ 4441 (1,335.15)** | yes |
| R365 has 0.00, sheet has a real figure | Arbor Jul 2025 My Hot Lunchbox 4,348.75 vs 0.00 | **no — left alone** |

### Deliberately left alone

- **Arbor Nov 2025 EZCater** — sheet 2,550.90 vs 2,771.09. Not a prefix, not an
  account subset. Ratio 0.92 is suggestive of a commission but no other month
  behaves that way, so it stays a hypothesis, not a fix.
- **Cherrywood Nov 2025 America To Go** — sheet 7,796.96 vs 3,673.17. Not
  cumulative either (Sep–Nov = 9,542.60).
- **Arbor Jul 2025 My Hot Lunchbox** — sheet 4,348.75, R365 0.00.

The writer **refuses to overwrite a real figure with 0.00**. R365 having no
revenue is not evidence the sheet is wrong — the money may sit under an account
this mapping does not know, and writing the zero would destroy the only record of
it. Guarded by a test.

### Rounding is not a correction

The maintainer keys whole dollars; R365 carries cents. Differences under $1.01 are
classified `unchanged`, so 8 cosmetic rewrites no longer bury the real corrections
in the diff.

- **BF (In-house)** still does not reconcile on either grain: a widened account set
  lands Menchaca Dec 2025 exactly but leaves Cherrywood Oct 2025 off by ~4,400.
  Excluded from writes until someone confirms the definition.

## R365 API notes (each learned the hard way)

- `TransactionDetail` has no business-date column. Filter `createdOn` (posting
  time) and resolve the business date from `Transaction.date`.
- `TransactionDetail.locationName` is **null** — resolve the store from
  `Transaction`, not the detail line.
- **The posting lag is large and variable**: median 8 days, p95 58, max ~110 in
  the target window. An early version padded the sweep by 12 days and silently
  dropped **64%** of lines. Business dates are now resolved by `transactionId`
  with no date filter — `Transaction` accepts an id-only `$filter` — so nothing
  is lost to a guessed pad.
- **Bulk history loads distort any lag check.** 163 back-dated lines were posted
  on 2025-08-25 (lags to 230 days) and more on 2026-01-21. The completeness check
  ignores posting *days* carrying a large batch of old lines; otherwise the alarm
  fires on every run, and an alarm that always fires is one nobody reads.
- `number` is not a selectable field on `Transaction`; `$select` it and the
  request fails.
- Date-filtered `$filter` clauses are limited to 31-day ranges.
- Datetime literals must be full: `date ge 2026-03-01T00:00:00Z`. A bare date
  returns HTTP 400.
- GUIDs are bare in filters: `glAccountId eq 2ce8...`, never `guid'...'`.
- `$top` caps at 5000; follow `@odata.nextLink`.
- Basic auth needs one literal backslash in `tsochinese\<user>`; build it with
  `chr(92)` in a `.py` file or a shell heredoc mangles it into a silent 401.
- Sales accounts are credit-side: value = `credit - debit`, so refunds net out.
- The 2023–24 migration batch (all posted 2025-08-25) shows lags up to 603 days.
  It sits outside any current window but will skew a naive lag statistic.
- Google Sheets can return a transient 403 "caller does not have permission" for
  a service account that genuinely has access. Retry before concluding anything.

## Files

- `verdict.py` — per-cell comparison of sheet vs calendar-month vs fiscal-period
- `confirm_grain.py` — the same test across all 179 cells
- `r365_months.py` — full-year R365 pull aggregated by calendar month
- `settle.py` — reads the sheet's Days-in-Month column
- `verdict.json` — raw per-cell output

Live scripts: `scripts/r365_catering.py`, `scripts/ingest-catering-r365.py`,
`scripts/validate-r365-catering.py`.
