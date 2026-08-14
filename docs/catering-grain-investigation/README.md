# Catering grain investigation — 2026-08-14

Why `scripts/ingest-catering-pl.py` is gated off, and how to unblock it.

## The question

The catering ingester extracted 26 cells whose P&L value disagreed with the
hand-keyed history in Sales & Trends. Bobby asked which side was right.

## The answer

**The sheet is right.** The P&L is the wrong *grain* for these columns.

The Sales & Trends catering columns (BF–BM) hold **calendar-month** revenue.
The TRIS P&L only reports **28-day fiscal periods**. Those windows are ~27%
different in size, so the P&L figure is systematically lower.

## Evidence

R365 is the system the P&L is generated from, which makes it the tiebreaker.
Summing the same journal lines two different ways settles it:

| Store | Sheet `3.2026` | P&L fiscal P3 | R365 fiscal P3 | R365 calendar March |
|---|---|---|---|---|
| Cherrywood | 6,732.00 | 4,888.10 | 4,888.10 | **6,731.85** |
| Arboretum | 4,685.00 | 3,767.35 | 3,767.35 | **4,684.55** |
| South Congress | 5,951.00 | 4,755.80 | 4,755.80 | **5,950.60** |
| Round Rock | 5,183.00 | 4,207.85 | 4,207.85 | **5,182.60** |
| Menchaca | 5,130.00 | 3,891.20 | 3,891.20 | **5,130.15** |

R365-over-fiscal-period reproduces the P&L exactly. R365-over-calendar-month
reproduces the sheet to the cent. The sheet was keyed from calendar months.

Corroborating signal: the sheet's own **Days in Month** column reads `31` for
row `3.2026` and `28` for `2.2026` — calendar lengths.

Across all populated catering cells, R365-by-calendar-month matches the sheet on
**120 of 154** comparable cells (84 exact, 36 within a dollar — history was keyed
to whole dollars).

## The red herring

Early on, `sheet ≈ P&L revenue + vendor commission` fit the Lunchdrop cluster to
within 0.1–0.5% for three of five stores, which looked like a gross-vs-net story.
It was a coincidence of magnitude: Lunchdrop commission runs ~25% and the fiscal
window is ~27% short of a 31-day month. R365 disambiguated what curve-fitting on
the P&L alone could not — a reminder to reconcile against the source system
rather than the artifact.

## Why one early cell looked like it matched

Cherrywood `11.2025` Lunchdrop = 3,893.45 equals the P&L Period 11 2025 figure
exactly, which suggested the sheet had once been keyed from fiscal periods. It is
one cell out of many; November 2025 happens to be a month where the two windows
nearly coincide. The broad comparison overrules it.

## Unresolved

The **In-house column (BF)** does not reconcile on either grain. The heading says
"Square, FlexCater" and `4130 - Square Catering Sales` exists in R365, but a
widened account set still only lands some stores (Menchaca Dec 2025 exact,
Cherrywood Oct 2025 off by ~4,400). BF needs its account definition confirmed
with whoever maintains the sheet before automating it.

Four EZCater cells remain blocked separately because history was hand-split
across BJ and BK, so writing a combined total would double-count.

## To unblock

Re-source these columns from R365 OData by business date over the calendar month
and delete the gate in `main()`. API gotchas worth keeping:

- `$top` is capped at **5000** — paginate with `$skip`.
- `Transaction.date` requires a **full datetime literal**
  (`date ge 2026-03-01T00:00:00Z`); a bare date returns HTTP 400.
- `TransactionDetail` filters on `createdOn`, which is the *posting* time, not
  the business date — join to `Transaction.date` for the real one. Post dates lag
  business dates, so widen the sweep on both ends.
- Auth needs one literal backslash in the domain prefix; build it with `chr(92)`
  in Python rather than a shell heredoc, or it silently 401s.

## Scripts

| File | What it does |
|---|---|
| `settle.py` | Reads the Days-in-Month column — calendar vs fiscal |
| `r365_months.py` | Pulls R365 catering lines, aggregated by business month |
| `verdict.py` | Per-cell verdict for the 26 disagreements |
| `confirm_grain.py` | Sheet vs R365-by-calendar-month across all cells |
| `verdict.json` | The verdict table as data |
