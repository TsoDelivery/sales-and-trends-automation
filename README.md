# Sales and Trends Automation

Automates updates to the **Sales and Trends** spreadsheet for all five Tso Chinese stores, sourcing financial data primarily from **Restaurant365 (R365)**.

**Long-term goal:** Move repeatable updates from manual/AI-assisted edits into reviewed, version-controlled, server-side automation run via GitHub Actions.

---

## Target Spreadsheet

- **Name:** Sales and Trends
- **Spreadsheet ID:** `1XDkS81q0CrZNyX6rxqFdeWpRJZQpBrgSbGjFSC7nHsM`
- **Service account:** `marketing-automation-sheets@tso-chinese-delivery.iam.gserviceaccount.com`
- **Permission required:** Editor access (owner by requester)

---

## Known Tabs

| Tab | Purpose |
|---|---|
| `Month End Summary` | AI-generated summary; needs automation support for periodic updates |
| `YOY Revenue` | System-wide monthly revenue by year (2018–2026) |
| `PRIME + EBITDA` | COGS / Labor / PRIME / EBITDA per store per year — **nice-to-have** |
| `YOY Tickets` | Ticket counts by store by year/month |
| `Sheet17` | 1P Delivery/Takeout/Kiosk breakdown by store with % shares |
| `Arbor Monthly Sales` | Store-level monthly sales and ticket breakdowns |
| `Cherrywood Monthly Sales ` | *(trailing space in tab name — watch for in API calls)* |
| `Menchaca Monthly Sales` | Store-level monthly sales and ticket breakdowns |
| `Round Rock Monthly Sales` | Store-level monthly sales and ticket breakdowns |
| `TsoCo Monthly Sales` | Store-level monthly sales and ticket breakdowns |

### Store Monthly Sales — column layout (all 5 stores share the same template)

| Group | Headers |
|---|---|
| Basics | DATE · Days in Month · Total Tix · Avg Tix/Day · Total AOV |
| 1P Sales | 1P Sales (Carryout+Delivery+Kiosk+Phone AI) · % of Total Sales |
| TsoGiving | Donation · Sales |
| Carryout | Sales · % of Total · Tickets · AOV |
| Delivery | Sales · % of Total · Tickets · AOV |
| Kiosk / Walk-In | Kiosk Take Out · % of Total Website Sales · Walk-In Tix · AOV |

**Note:** "Total Website Sales" references should be simplified to just "1P Sales" going forward — website sales are redundant since Carryout and Delivery already cover the same information.

---

## Data Sources & Philosophy

| Data | Source | Notes |
|---|---|---|
| 1P Carryout + Delivery sales | **R365** (preferred) over Grafana | Grafana can vary from R365; R365 is the system of record |
| 3P delivery vendor data | **R365** (preferred) over Tray | DoorDash "Net sales" in Tray is actually gross sales (unreliable unless fixed) |
| Catering | YTD average formula; backfill from R365 when complete | Start with a placeholder, update once monthly R365 runs are finalized |
| PRIME + EBITDA | R365 P&L data | Nice-to-have to populate this tab once store sales automation is stable |

**Color coding convention:** Yellow = Pending, Gray = Formula Calculations.

---

## Traps — read before changing anything

1. **Cherrywood tab has a trailing space in its name.** The metadata/properties API returns `"Cherrywood Monthly Sales "` (with a trailing space). `encodeURIComponent` does NOT encode ASCII spaces — you must manually encode as `%20` or the API call will 400.

2. **The `getValues` function uses `encodeURIComponent` on the range string.** This preserves `!` characters, which can cause range parsing errors for tab names containing `!`. If a tab with `!` ever reappears, wrap the sheet name in single quotes and build the URL path manually.

3. **Metadata endpoint requires OAuth, service-account-JWT-based calls use values endpoint.** The `GET /v4/spreadsheets/{id}?fields=sheets.properties` endpoint returns 403/401 with service-account token even when values read works fine. Always fall back to reading known tab names directly when metadata fails.

4. **Never verify a writer using the writer's own config.** An audit that imports the writer's column map risks agreeing with itself. Keep audit/verification scopes independent of the write scripts.

---

## Project Structure

```
sales-and-trends-automation/
├── .github/
│   └── workflows/
│       └── sales-trends-weekly.yml    # scheduled GitHub Action
├── scripts/
│   ├── lib/
│   │   ├── google-sheets.mjs           # shared Google Sheets API client
│   │   └── env.mjs                     # env loading helper
│   ├── write-sales-trends.mjs          # main data-write script
│   └── audit-scope.mjs                 # independent verification
├── .env                                # local dev environment (never committed)
├── .env.example                        # template with placeholders
├── .gitignore
├── package.json
└── README.md
```

---

## Local Development

```bash
# Setup
cp .env.example .env
# Fill in .env with real values (ask a teammate with access)

# Dry-run a single month
node scripts/write-sales-trends.mjs --month 2026-08

# Write
node scripts/write-sales-trends.mjs --month 2026-08 --write

# Audit
node scripts/audit-scope.mjs --month 2026-08
```

## Deploy (server-side)

Once core logic is stable and the SOP is written:

1. Add `GOOGLE_SERVICE_ACCOUNT_JSON` as a GitHub Actions secret.
2. Add `SALES_TRENDS_SPREADSHEET_ID` as a GitHub Actions secret.
3. Configure the cron schedule in `.github/workflows/sales-trends-weekly.yml`.
4. Merge to `main`.