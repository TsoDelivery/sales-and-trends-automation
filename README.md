# Sales & Trends Automation

Automates the monthly sales data flow from **Restaurant365 (R365)**, with Tray
retained only where R365 does not yet expose a reliable equivalent, into the
**Sales and Trends** Google Spreadsheet.

## What it does

For each of the 5 Tso Chinese Delivery stores, every month the script:

1. **Pulls** supported channel revenue from R365 SalesDetail for the given month
2. **Uses R365's channel buckets where available:**
   - **1P Sales/Tix** — first-party sales (kiosk, takeout, delivery, phone orders)
   - **3P Sales/Tix** — third-party delivery platforms (DoorDash, Uber Eats, Grubhub, etc.)
   - **1P Sales** — Carryout + Delivery + Kiosk + Phone AI
   - **Phone AI** — phone AI ordering companies (UrbanPiper, AIAssistant.co, Voicify)
3. **Writes** supported R365 values into empty monthly cells in Google Sheets
4. **Skips** cells that already have values (never overwrites)

## Stores

| Store | Tray Site ID | Spreadsheet Tab |
|-------|:------------:|-----------------|
| Cherrywood | 589 | Cherrywood Monthly Sales |
| Arbor | 590 | Arbor Monthly Sales |
| TsoCo | 586 | TsoCo Monthly Sales |
| Round Rock | 591 | Round Rock Monthly Sales |
| Menchaca | 514 | Menchaca Monthly Sales |

## Phone AI Data

Phone AI orders are extracted from **Tray Revenue Centers**, mapped per store:

| Store (Site ID) | Provider | Tray RC Name |
|---|---|---|
| Cherrywood (589) | UrbanPiper | `UrbanPiper` |
| Arbor (590) | AIAssistant.co | `AIAssistant.co` |
| TsoCo (586) | Voicify | `Voicify` |
| Round Rock (591) | AIAssistant.co | `AIAssistant.co` |
| Menchaca (514) | AIAssistant.co | `AIAssistant.co` |

The script extracts phone AI data from Tray per-day, aggregates monthly, and writes only to **empty cells** (never overwrites existing data). The naming conventions weren't set up in Tray until mid-2026, so Jan–Apr shows $0 via Tray — existing data for those months came from direct portal access.

## Column mapping

| Column | Value | Auto? |
|--------|-------|:-----:|
| AB | 1P - Phone Order (Ai) Sales | Written |
| AC | % of Total | Formula — skipped |
| AD | Phone Order (Ai) Tix | Written |
| AE | AOV | Formula — skipped |

The script only writes **AB** and **AD** (white columns). AC and AE are gray formulas that auto-update.

## Usage

```bash
# Dry run (see what would be written)
node scripts/write-sales-trends.mjs --month 2026-08

# Write to the spreadsheet
node scripts/write-sales-trends.mjs --month 2026-08 --write

# Specific stores only
node scripts/write-sales-trends.mjs --month 2026-08 --stores arbor,tsoco
```

## Scheduling

### R365 validation

The server-side workflow `.github/workflows/r365-validation-monthly.yml` runs on
the 3rd of each month and validates the previous closed month against the live
Sales & Trends sheet, then fills only empty supported R365 cells. It flags
supported R365 channels when the difference is outside the applicable rule.
Existing non-empty cells are never overwritten.

```bash
python3 scripts/r365-sales-trends.py --month 2026-07 --validate
```

R365 can validate and fill Kiosk, Uber Eats, DoorDash, Favor, and Grubhub. R365 does not
contain real channel-level Carryout or Delivery revenue; those remain covered by
the existing Tray/UrbanPiper/Grafana flow. Uber Eats and DoorDash use calibrated
promo-adjusted net/gross bands, so the validator flags unusual promo behavior
rather than the normal gross-vs-net difference.

The separate **website sales** concept has been retired. Carryout and Delivery
remain part of **1P Sales**, alongside Kiosk and Phone AI; percentage columns now
refer to Total Sales rather than Website Sales.

DoorDash is treated as **gross revenue**, regardless of the legacy "Net Sales"
label in Tray. Catering remains an explicit formula placeholder — currently a
YTD-average placeholder pending complete R365 financials — and is not populated
with an invented amount.

### Daily Days in Month updater

The workflow `.github/workflows/days-in-month-daily.yml` runs daily at 8:00 AM
Central and updates only column C (`Days in Month`) on the current month row in
all five store tabs. It uses completed Central-time calendar days: on August 13,
for example, it writes `12`. It never touches sales, ticket, formula, or manual
Grafana cells. Scheduled runs write automatically; manual runs default to a dry
run unless `dry_run` is turned off.

The updater is deliberately separate from the weekly WOW writer. The WOW job
continues to process completed Sunday–Saturday weeks, while this job maintains
the month-to-date day-count denominator until the month closes.

See [CRON.md](CRON.md) for the existing Tray schedule.

## Catering revenue from the TRIS P&L email

> **STATUS: BLOCKED — this script does not write. Read this before touching it.**
>
> The extraction works and is unit-tested. The problem is the **grain**: the
> TRIS P&L reports 28-day fiscal periods, but the Sales & Trends catering
> columns hold **calendar-month** figures. Feeding the P&L into those rows
> understates catering revenue by roughly **20–27%**. A gate in `main()` stops
> the run before any cell is written.

### How we know (verified against R365, 2026-08-14)

Three independent checks agree:

1. The sheet's own **Days in Month** column reads `31` for row `3.2026` and
   `28` for `2.2026` — calendar lengths, not 28-day periods.
2. R365 journal lines summed over **calendar March 2026** reproduce the sheet
   to the cent — Cherrywood Lunchdrop `6,731.85` against a sheet value of
   `6,732`. Same for all five stores.
3. The **same** lines summed over **fiscal P3** (2/22–3/21) reproduce the P&L
   figure, `4,888.10` — about 27% lower.

So the sheet is right and the P&L is the wrong shape for these columns. Across
all populated catering cells, R365-by-calendar-month matches the sheet on 120 of
154 comparable cells (84 exact, 36 within a dollar of the whole-dollar keying).

Note that fiscal periods still govern **elsewhere** in this repo — the row label
`8.2026` means fiscal period 8 for the vendor profitability work. The catering
columns are the exception, which is precisely why this was easy to get wrong.

### The fix, when someone picks this up

Source these columns from **R365 OData aggregated by business date** over the
calendar month, not from the P&L:

- `TransactionDetail` filtered on `createdOn`, joined to `Transaction.date` for
  the true business date. `$top` is capped at 5000, so paginate with `$skip`.
- `Transaction.date` needs a **full datetime literal**
  (`date ge 2026-03-01T00:00:00Z`). A bare `2026-03-01` returns HTTP 400.
- Auth uses a single literal backslash in the domain prefix; build it with
  `chr(92)` rather than in a shell heredoc, or it silently 401s.

The In-house column (BF) needs its account set resolved first — the heading says
"Square, FlexCater" and `4130 - Square Catering Sales` is real, but even a widened
set only reconciles some stores (Menchaca Dec 2025 lands exactly, Cherrywood does
not). Treat BF as unresolved.

```bash
# Runs the extraction and then refuses to write, explaining why
python3 scripts/ingest-catering-pl.py --period 3 --year 2026
```

### Fiscal calendar

`scripts/fiscal_calendar.py` implements the 13×28-day calendar, anchored on the
`P03 2026` statement header ("12 Periods Ending 03/21/2026"). Rolling forward
gives P08 2026 = Jul 12 – Aug 8, matching the vendor profitability automation.
Still correct and still useful — just not the right key for catering rows.

### Column mapping (extraction, verified)

| Column | Sheet heading | P&L GL account |
|---|---|---|
| BF | In-house Catering (Square, FlexCater) | `Total 4300 - Direct Catering Sales` |
| BH | Lunchdrop | `4420 - Lunchdrop Catering Sales` |
| BJ | EZCater | `Total 4440 - EZ Cater Sales` |
| BM | America To Go | `4445 - America To Go Catering Sales` |

Accounts are matched by **exact GL label**, never by row number: the catering
block sits at a different row on every store sheet and moves between periods.

### Accounts deliberately not written

- `4313 - Flex In-house Catering Sales (Tax Exempt)` — already inside
  `Total 4300`, so writing it to BG would double-count. Verified against nine
  historical cells where BF equals the total *including* tax-exempt.
- `4441 - EZ Cater Sales (Tax Exempt)` — already inside `Total 4440` (BJ).
- `4130 - Square Catering Sales` (Round Rock) — sits in Direct Sales, outside
  `Total 4300`, with no verified Sales & Trends column.

These are reported as notes on every run so a person can decide.

### Write safety

Default is a dry run. Per cell:

| Situation | Behaviour |
|---|---|
| Sheet cell blank | Filled |
| Agrees within $0.50 | No-op (history was hand-keyed to whole dollars) |
| Real disagreement | **Skipped** and reported; needs `--allow-overwrite` |
| EZCater BK populated | **Blocked** — BJ is the total, writing it would double-count |
| No sheet row for the period | Reported, never invented |

Writes are read back and verified cent-for-cent; a mismatch exits non-zero.

### Known state as of P03 2026

Across 179 comparable historical cells: 104 agree cent-for-cent, 29 differ only
by hand-keyed rounding, 26 genuinely disagree, and 4 are blocked on the EZCater
tax-exempt split. The disagreements are concentrated in Lunchdrop (BH) and
EZCater (BJ) and are **not** auto-resolved — the sheet's hand-keyed values are
not treated as truth, and neither is the P&L, until someone reconciles them.

## Credentials

- `.env` — Tray API credentials
- `.secrets/google-service-account.json` — Google Sheets service account
- GitHub Actions secrets: `GOOGLE_SERVICE_ACCOUNT_JSON`, `SALES_TRENDS_SPREADSHEET_ID`,
  `R365_USERNAME`, and `R365_PASSWORD`

---

*This README was auto-generated by the automation setup process.*