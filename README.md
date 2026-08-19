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
the existing Tray/UrbanPiper flow and the separate server-side Grafana WOW 1P
automation. The Grafana job uses a dedicated read-only service credential and
independent write-back verification; it is not part of this monthly R365 writer. Uber Eats and DoorDash use calibrated
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

## Catering revenue

**Use `scripts/ingest-catering-r365.py`.** It reads Restaurant365 and fills the
catering columns (BH, BJ, BM) on the five store tabs.

```bash
# Cache R365 credentials once per session (never commit these):
export OP_SERVICE_ACCOUNT_TOKEN=$(cat ~/.op_service_account_token)
op item get "Tsora Restaurant365" --vault "Administrative Assistants" \
  --fields label=username --reveal > /tmp/.r365u
op item get "Tsora Restaurant365" --vault "Administrative Assistants" \
  --fields label=NewPassword --reveal > /tmp/.r365p
chmod 600 /tmp/.r365u /tmp/.r365p

# Dry run (default -- writes nothing):
.venv/bin/python scripts/ingest-catering-r365.py --month 2026-06

# Apply:
.venv/bin/python scripts/ingest-catering-r365.py --month 2026-06 --commit
```

### Why R365 and not the emailed P&L

These columns hold **calendar-month** revenue. The TRIS P&L only reports
**28-day fiscal periods**, so its numbers are the wrong shape -- writing them in
understates catering by roughly 20-27% every period. R365 is the system the P&L
is generated *from*, so aggregating its journals by business date gives the
right figure at any grain.

Full evidence, including the near-miss that made a wrong answer look right:
`docs/catering-grain-investigation/`.

### Validated against history

`scripts/validate-r365-catering.py` replays R365 against hand-keyed history it
did not author. Over Jun 2025 - Jul 2026:

| Column | Agrees | Notes |
|---|---|---|
| BH Lunchdrop | 64/65 (98%) | |
| BJ EZCater | 31/37 (84%) | 5 of 6 misses are stale sheet values, see below |
| BM America To Go | 5/10 (50%) | 3 Round Rock cells have no R365 journal at all |

History was keyed to whole dollars, so cent-level differences are expected and
counted as agreement.

### Columns deliberately NOT written

- **BF (In-house / Square / FlexCater)** — excluded. Its account set does not
  reconcile on either grain: a widened set lands Menchaca Dec 2025 exactly but
  leaves Cherrywood Oct 2025 off by ~4,400. Needs confirming with whoever
  maintains the sheet.

### Known bad history — needs a decision, not a silent fix

The validator found existing cells that are wrong. The writer **skips** these by
default rather than quietly correcting them; pass `--overwrite` to fix them.

- **Six stale partial-month cells.** Each matches R365 exactly through an
  early weekly journal and then stops — they were keyed before the month's last
  journal posted. Example: TsoCo `6.2026` BJ reads 4,630.00 and matches R365
  through the third of four weekly journals; the true total is 6,648.73.
- **Arbor `10.2025` BJ = 838.45** captures only account 4440 and omits 4441
  (EZCater tax-exempt). True total 4,648.45.
- **Cherrywood `6.2026` BH = 4,160.00** — unexplained. R365 gives 3,539.95 by
  business date and 3,768.85 by posting date; neither is 4,160.
- **Round Rock BM `3.2026`, `4.2026`, `5.2026`** hold America To Go revenue but
  R365 has **no** 4445 journal for Round Rock in any month. Either the revenue
  is booked elsewhere or the sheet figures came from outside R365.

### Safety rules the writer enforces

1. Dry run by default; writing needs `--commit`.
2. Whole, closed calendar months only, and not until `--settle-days` (default
   45) have passed — journals post a median of 8 and up to ~110 days late, so a
   month written too early is silently short. This is the exact mechanism that
   produced the six stale cells.
3. Existing differing values are skipped and reported unless `--overwrite`.
4. Any completeness or coverage warning blocks a commit unless `--force`.
5. Every write is read back and verified.

### The P&L script

`scripts/ingest-catering-pl.py` is the earlier, wrong-grain approach: it reads
the emailed TRIS P&L, which reports 28-day fiscal periods rather than calendar
months. A gate in `main()` stops it before any cell is written. Kept for
reference only — the extraction itself is sound and unit-tested, it is only
pointed at the wrong source.

Note that fiscal periods still govern **elsewhere** in this repo — the row label
`8.2026` means fiscal period 8 for the vendor profitability work. The catering
columns are the exception, which is precisely why this was easy to get wrong.

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