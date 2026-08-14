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

Re-run with R365 as the source, the picture inverted: most remaining
disagreements are errors *in the sheet*, not in the extraction.

**Six stale partial-month cells.** Each matches R365 exactly through an early
weekly journal and then stops — keyed before the month's last journal posted:

| Cell | Sheet | Matches R365 through | True total |
|---|---|---|---|
| TsoCo BJ `6.2026` | 4,630.00 | 3 of 4 weekly journals | 6,648.73 |
| Cherrywood BJ `6.2026` | 3,142.00 | 3 of 4 | 3,432.75 |
| Arbor BJ `6.2026` | 2,060.00 | 3 of 4 | 2,275.25 |
| Arbor BJ `11.2025` | 2,550.90 | 5 of 7 | 4,648.45* |
| Cherrywood BJ `11.2025` | 577.30 | 3 of 5 | 709.97 |
| Cherrywood BM `12.2025` | 1,033.01 | 1 of 2 | 1,300.16 |

**Arbor BJ `10.2025` = 838.45** is a different error: it captures only account
4440 and omits 4441 (EZCater tax-exempt). True total 4,648.45.

**Still unexplained — do not guess:**

- **Cherrywood BH `6.2026` = 4,160.00.** R365 gives 3,539.95 by business date and
  3,768.85 by posting date. Neither is 4,160.
- **Round Rock BM `3.2026`, `4.2026`, `5.2026`** hold America To Go revenue, but
  R365 has **no** 4445 journal for Round Rock in *any* month. Either it is booked
  elsewhere or those figures came from outside R365.
- **BF (In-house)** does not reconcile on either grain. A widened account set
  lands Menchaca Dec 2025 exactly but leaves Cherrywood Oct 2025 off by ~4,400.
  Excluded from writes until someone confirms the definition.

## R365 API notes (each learned the hard way)

- `TransactionDetail` has no business-date column. Filter `createdOn` (posting
  time) and resolve the business date from `Transaction.date`.
- **The posting lag is large and variable**: median 8 days, p95 58, max ~110 in
  the target window. An early version padded the sweep by 12 days and silently
  dropped **64%** of lines. Business dates are now resolved by `transactionId`
  with no date filter — `Transaction` accepts an id-only `$filter` — so nothing
  is lost to a guessed pad.
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
