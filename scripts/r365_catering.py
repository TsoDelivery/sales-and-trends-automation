"""R365 OData -> catering revenue by store and CALENDAR MONTH.

WHY THIS EXISTS
---------------
The Sales & Trends catering columns (BF-BM) hold CALENDAR-MONTH revenue. The
TRIS P&L only reports 28-day fiscal periods, so it is the wrong shape for these
columns -- feeding it in understates catering by ~20-27%. Verified against R365
on 2026-08-14; see docs/catering-grain-investigation/.

R365 is the system the P&L is generated FROM, so aggregating its journal lines
by business date is the authoritative source at any grain we like.

API CONTRACT (every line here was learned the hard way -- do not "simplify")
---------------------------------------------------------------------------
* `TransactionDetail` has NO business-date column. Filter it on `createdOn`
  (the POSTING time) and resolve the real business date from `Transaction.date`.
* THE POSTING LAG IS LARGE AND VARIABLE. Measured on Lunchdrop journals posted
  in March 2026: minimum 3 days, MEDIAN 31, maximum 58. Journals posted in one
  month routinely carry business dates two months earlier. So a `createdOn`
  sweep padded by a week or two silently truncates whole months -- an early
  version of this file padded by 12 days and dropped ~64% of the lines.
* Therefore business dates are resolved by **transactionId, in chunks, with no
  date filter at all** -- `Transaction` accepts an id-only `$filter`, unlike
  `TransactionDetail`. That is exact, so nothing is lost to a guessed pad.
* `POST_LAG_PAD` still governs how wide the `createdOn` sweep runs. It must
  comfortably exceed the real posting lag, and `verify_completeness` reports
  when observed lag approaches the pad so truncation cannot pass unnoticed.
* Every `$filter` must carry a date range of <=31 days -- EXCEPT an id-only
  filter on `Transaction`. We use 25-day windows for date-filtered sweeps.
* Datetime literals must be full: `date ge 2026-03-01T00:00:00Z`. A bare
  `2026-03-01` returns HTTP 400.
* GUID literals are bare: `glAccountId eq 2ce8cf8e-...`, never `guid'...'`.
* `$top` is capped server-side at 5000; follow `@odata.nextLink` instead of
  hand-rolling `$skip`.
* Basic auth needs ONE literal backslash in `tsochinese\\<user>`. Build it with
  chr(92) in a real .py file -- a shell heredoc mangles it into a silent 401.
* Sales accounts are CREDIT-side: value = credit - debit. Refunds post as
  debits, so always net them rather than summing credits.
* Google Sheets reads can return a transient 403 "caller does not have
  permission" on a service account that genuinely has access. Retry before
  concluding the permissions are wrong.
"""

from __future__ import annotations

import base64
import collections
import datetime as dt
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://odata.restaurant365.net/api/v2/views/"
WINDOW_DAYS = 25          # <=31 hard API limit, 25 leaves headroom
# Journals post LONG after the sale. Ordinary weekly journals land within ~2
# weeks, but correction batches are far worse: a 5-line adjustment for business
# date 2025-07-31 was posted 2026-01-21, a lag of 174 days. A small pad silently
# truncates whole months, so this is deliberately generous -- the cost is a few
# extra queries, and the cost of getting it wrong is quiet bad data.
POST_LAG_PAD = 240
TIMEOUT = 300

# R365 location name -> Sales & Trends tab name.
STORE_TABS = {
    "Tso Chinese Cherrywood": "Cherrywood Monthly Sales ",
    "Tso Chinese Arboretum Crossing": "Arbor Monthly Sales",
    "TsoCo South Congress": "TsoCo Monthly Sales",
    "Tso Chinese Round Rock": "Round Rock Monthly Sales",
    "Tso Chinese Menchaca": "Menchaca Monthly Sales",
}

NON_STORE = {
    "Corporate Office",
    "Prep/Commissary",
    "ZZZ Inactive (Cedar Park)",
    "ZZZ Inactive (Pflugerville)",
}

# Sales & Trends column -> GL account NUMBERS that sum to it.
#
# Matched by account NUMBER (stable) and resolved to GUIDs at runtime. Numbers,
# not names: TRIS renames accounts between periods.
#
# BH/BJ/BM verified cent-exact against hand-keyed history -- see
# docs/catering-grain-investigation/. BF is NOT verified and is excluded by
# default; see UNVERIFIED_COLUMNS.
# Map a sheet HEADER LABEL to the GL accounts that feed it.
#
# DO NOT map by column letter. The catering columns are NOT in the same order on
# every tab: BM is "America To Go" on Cherrywood and Arbor, "Try Hungry" on
# Round Rock, and "Event" on TsoCo and Menchaca. An earlier version hardcoded
# Cherrywood's layout and consequently validated Round Rock's Try Hungry cells
# against America To Go revenue -- a meaningless comparison that made real data
# look like a mystery. Resolve columns from row 1 of each tab, per tab.
#
# Each header also gets exactly the accounts it names. "EZCater" and
# "EZCater (non-Tax)" are SEPARATE columns, so 4441 belongs to the latter alone;
# folding it into the former inflates it.
HEADER_ACCOUNTS = {
    "Lunchdrop": ["4420"],
    "EZCater": ["4440", "4442"],          # taxable sales + discounts
    "EZCater (non-Tax)": ["4441"],        # tax-exempt sales, its own column
    "America To Go": ["4445"],
    "Sharebite": ["4430"],
    "My Hot Lunchbox": ["4410", "4411"],
    "Try Hungry": ["4446"],
}

# Headers we deliberately never write, with the reason.
HEADER_SKIP = {
    "In-house Catering (Square, FlexCater)": "account set unconfirmed, reconciles on neither grain",
    "In-house Catering (Square, Spoonfed)(Non-Taxable)": "account set unconfirmed",
    "Event": "no GL account identified",
    "Forkable (None)": "marked None on the sheet",
    "Cater2Me (report emailed)": "sourced from an emailed report, not R365",
    "Cater2Me (non-taxable)": "sourced from an emailed report, not R365",
    "Platterz (report emailed)": "sourced from an emailed report, not R365",
    "Foodee after fees (wholesale)": "net of fees; R365 books gross",
}

# Kept only so older callers fail loudly rather than silently using a wrong map.
COLUMN_ACCOUNTS = None


# Deliberately NOT written. The script reports these rather than guessing.
UNVERIFIED_COLUMNS = {
    "BF": (
        "In-house Catering (Square, FlexCater). Does not reconcile on either "
        "grain: a widened account set lands Menchaca Dec 2025 exactly but "
        "leaves Cherrywood Oct 2025 off by ~4,400. The account definition "
        "needs confirming with whoever maintains the sheet."
    ),
}


def resolve_columns(header_row, first_col="BE", last_col="BR"):
    """Map header label -> column letter for ONE tab, read from its row 1.

    The catering block is not in the same order on every tab, so this must be
    called per tab. Returns (writable, skipped, unknown):

        writable  {header: (column_letter, [gl_accounts])}
        skipped   {header: column_letter}   -- known, deliberately not written
        unknown   {header: column_letter}   -- unrecognised, reported not guessed
    """
    lo, hi = column_index(first_col), column_index(last_col)
    writable, skipped, unknown = {}, {}, {}
    for idx in range(lo, hi + 1):
        label = str(header_row[idx]).strip() if len(header_row) > idx else ""
        if not label:
            continue
        letter = column_letter(idx)
        if label in HEADER_ACCOUNTS:
            writable[label] = (letter, HEADER_ACCOUNTS[label])
        elif label in HEADER_SKIP:
            skipped[label] = letter
        else:
            unknown[label] = letter
    return writable, skipped, unknown


def column_letter(index):
    """0-based index -> spreadsheet column letters (0 -> A, 57 -> BF)."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def column_index(letters):
    """Spreadsheet column letters -> 0-based index (A -> 0, BF -> 57)."""
    total = 0
    for char in letters.strip().upper():
        total = total * 26 + (ord(char) - ord("A") + 1)
    return total - 1


# Every catering-ish account, for --audit. Lets a person see what is being left
# out of the mapped columns instead of trusting the mapping blindly.
AUDIT_ACCOUNTS = [
    "4130", "4133", "4134", "4136", "4137", "4142", "4144", "4210.1", "4213",
    "4300", "4310", "4311", "4312", "4313", "4400", "4410", "4411", "4420",
    "4430", "4440", "4441", "4442", "4445", "4446",
]


# ------------------------------------------------------------------ transport

def auth_headers(user_path="/tmp/.r365u", pass_path="/tmp/.r365p"):
    """Basic auth for R365. The domain prefix needs one literal backslash."""
    try:
        user = open(user_path).read().strip()
        password = open(pass_path).read().strip()
    except FileNotFoundError as exc:
        raise SystemExit(
            f"R365 credentials not cached ({exc.filename}). Run:\n"
            "  export OP_SERVICE_ACCOUNT_TOKEN=$(cat ~/.op_service_account_token)\n"
            '  op item get "Tsora Restaurant365" --vault "Administrative Assistants" '
            "--fields label=username --reveal > /tmp/.r365u\n"
            '  op item get "Tsora Restaurant365" --vault "Administrative Assistants" '
            "--fields label=NewPassword --reveal > /tmp/.r365p\n"
            "  chmod 600 /tmp/.r365u /tmp/.r365p"
        ) from exc
    if not user or not password:
        raise SystemExit("R365 credential files are empty")
    principal = "tsochinese" + chr(92) + user
    token = base64.b64encode(f"{principal}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Accept": "application/json"}


def get(entity, params, headers):
    """GET with @odata.nextLink pagination. Surfaces the real OData error text."""
    query = urllib.parse.urlencode(params, safe="$'(), -:/")
    url = f"{BASE}{entity}?{query}"
    rows = []
    while url:
        request = urllib.request.Request(url, headers=headers)
        try:
            payload = json.load(urllib.request.urlopen(request, timeout=TIMEOUT))
        except urllib.error.HTTPError as exc:
            body = exc.read()[:400].decode("utf-8", "replace")
            raise SystemExit(f"{entity} HTTP {exc.code}: {body}")
        rows.extend(payload.get("value", []))
        url = payload.get("@odata.nextLink")
    return rows


def windows(start, end, span=WINDOW_DAYS):
    """Half-open [lo, hi) windows of at most `span` days covering start..end."""
    cursor = start
    while cursor <= end:
        stop = min(cursor + dt.timedelta(days=span), end + dt.timedelta(days=1))
        yield cursor, stop
        cursor = stop


# -------------------------------------------------------------------- fetching

def resolve_accounts(numbers, headers):
    """Account numbers -> {number: (guid, name)}. Fails loudly on a bad number."""
    rows = get("GlAccount", {"$select": "glAccountId,glAccountNumber,name", "$top": "5000"},
               headers)
    by_number = {str(r.get("glAccountNumber") or "").strip(): r for r in rows}
    out, missing = {}, []
    for number in numbers:
        row = by_number.get(str(number))
        if row is None:
            missing.append(number)
            continue
        out[str(number)] = (row["glAccountId"], row["name"])
    if missing:
        raise SystemExit(f"GL account number(s) not found in R365: {missing}")
    return out


def resolve_business_dates(transaction_ids, headers, chunk=20, verbose=True):
    """transactionId -> {date, isApproved, locationName}, resolved EXACTLY.

    `Transaction` accepts an id-only `$filter` (no date range needed), so we
    never have to guess how far back a journal's business date might sit. This
    is the fix for the posting-lag truncation described in the module docstring.
    """
    out = {}
    ids = list(dict.fromkeys(transaction_ids))
    for i in range(0, len(ids), chunk):
        batch = ids[i:i + chunk]
        id_filter = " or ".join(f"transactionId eq {tid}" for tid in batch)
        rows = get("Transaction", {
            "$select": "transactionId,locationId,locationName,date,isApproved,isTemplate",
            "$filter": id_filter,
            "$top": "500",
        }, headers)
        for row in rows:
            out[row["transactionId"]] = row
        if verbose and (i // chunk) % 10 == 0:
            print(f"  resolved {min(i + chunk, len(ids))}/{len(ids)} transactions",
                  file=sys.stderr, flush=True)
    return out


def fetch_lines(numbers, start, end, headers, verbose=True):
    """Journal lines for these GL accounts, keyed to BUSINESS date.

    Returns (records, warnings). Each record:
        {date, month, store, tab, account, net, approved, posted, lag_days}
    """
    accounts = resolve_accounts(numbers, headers)
    guid_to_number = {guid: num for num, (guid, _) in accounts.items()}
    name_of = {num: name for num, (_, name) in accounts.items()}

    locations = get("Location", {"$select": "locationId,name", "$top": "1000"}, headers)
    location_name = {loc["locationId"]: loc["name"] for loc in locations}

    def log(msg):
        if verbose:
            print(msg, file=sys.stderr, flush=True)

    # --- detail: filter createdOn (posting time), padded generously ----------
    # The pad must exceed the real posting lag (median ~31 days, max ~58 seen).
    id_filter = " or ".join(f"glAccountId eq {guid}" for guid, _ in accounts.values())
    detail = {}
    lo_pad = start - dt.timedelta(days=POST_LAG_PAD)
    hi_pad = end + dt.timedelta(days=POST_LAG_PAD)
    for lo, hi in windows(lo_pad, hi_pad):
        rows = get("TransactionDetail", {
            "$select": "transactionDetailId,transactionId,locationId,glAccountId,"
                       "credit,debit,createdOn",
            "$filter": (f"({id_filter}) "
                        f"and createdOn ge {lo.isoformat()}T00:00:00Z "
                        f"and createdOn lt {hi.isoformat()}T00:00:00Z"),
            "$top": "5000",
        }, headers)
        log(f"  detail createdOn {lo}..{hi}: {len(rows)}")
        for row in rows:
            detail[row["transactionDetailId"]] = row

    # --- business dates: resolved by id, exactly, no date guessing -----------
    log(f"  resolving business dates for {len(detail)} detail lines...")
    transactions = resolve_business_dates(
        [r["transactionId"] for r in detail.values()], headers, verbose=verbose)

    records, warnings = [], []
    unmatched = 0
    for row in detail.values():
        tx = transactions.get(row["transactionId"])
        if tx is None:
            unmatched += 1
            continue
        if tx.get("isTemplate"):
            continue
        business_date = tx["date"][:10]
        posted = (row.get("createdOn") or "")[:10]
        lag = None
        if posted:
            lag = (dt.date.fromisoformat(posted) - dt.date.fromisoformat(business_date)).days
        loc_id = row.get("locationId") or tx.get("locationId")
        store = location_name.get(loc_id, tx.get("locationName") or str(loc_id))
        if store in NON_STORE:
            continue
        number = guid_to_number[row["glAccountId"]]
        records.append({
            "date": business_date,
            "month": business_date[:7],
            "store": store,
            "tab": STORE_TABS.get(store),
            "account": number,
            "account_name": name_of[number],
            "net": round(float(row.get("credit") or 0) - float(row.get("debit") or 0), 2),
            "approved": bool(tx.get("isApproved")),
            "posted": posted,
            "lag_days": lag,
        })

    if unmatched:
        warnings.append(
            f"{unmatched} detail line(s) could not be resolved to a Transaction at all. "
            f"That should not happen with id-based resolution -- investigate, do not ignore."
        )
    unknown = sorted({r["store"] for r in records if r["tab"] is None})
    if unknown:
        warnings.append(f"R365 location(s) with no Sales & Trends tab mapping: {unknown}")
    return records, warnings


def verify_completeness(records, months, warnings=None):
    """Warn when the data looks truncated rather than merely sparse.

    Extreme lags come in BULK-IMPORT BATCHES, not from ordinary weekly journals:
    Tso's history was loaded on 2025-08-25 (163 lines, lags to 230 days) and
    again on 2026-01-21. Those days are one-off backfills, and letting them drive
    the check made it warn on every single run -- an alarm that always fires is
    an alarm nobody reads. So ignore any posting DAY that carries a big batch of
    old lines, and judge the pad on routine journals only.
    """
    warnings = list(warnings or [])

    per_posted_day = collections.Counter(
        r["posted"] for r in records
        if r.get("lag_days") is not None and r["lag_days"] > 120)
    bulk_days = {day for day, n in per_posted_day.items() if n >= 20}

    routine = [r for r in records
               if r.get("lag_days") is not None and r["posted"] not in bulk_days]
    if routine:
        worst = max(r["lag_days"] for r in routine)
        if worst > POST_LAG_PAD:
            warnings.append(
                f"routine posting lag reaches {worst} days against "
                f"POST_LAG_PAD={POST_LAG_PAD}. Raise POST_LAG_PAD -- months may "
                f"be truncated.")
    for day in sorted(bulk_days):
        warnings.append(
            f"note: {per_posted_day[day]} back-dated lines were bulk-posted on "
            f"{day}; excluded from the posting-lag check as a one-off import")

    for month in coverage_gaps(records, months):
        warnings.append(f"{month}: no catering lines at all -- verify this is a real zero")
    # Edge months are the ones a too-narrow sweep starves first.
    if months:
        for edge in (months[0], months[-1]):
            if not any(r["month"] == edge for r in records):
                warnings.append(f"edge month {edge} has NO lines at all -- likely truncated")
    return warnings


# ----------------------------------------------------------------- aggregation

def month_label(month_key):
    """'2026-03' -> '3.2026', matching the Sales & Trends row labels.

    NOTE: this label looks like the fiscal-period labels used elsewhere in this
    repo, but for the catering columns it is a CALENDAR month. That collision is
    exactly what made the original bug so easy to miss.
    """
    year, month = month_key.split("-")
    return f"{int(month)}.{int(year)}"


def aggregate(records, header_accounts=None):
    """records -> {tab: {'3.2026': {'Lunchdrop': 6731.85, ...}}}

    Keyed by HEADER LABEL, not column letter, because the same letter means
    different things on different tabs. The caller resolves label -> column per
    tab via resolve_columns(). Only whole, closed calendar months should be
    passed in; the caller decides.
    """
    header_accounts = header_accounts or HEADER_ACCOUNTS
    account_to_header = {}
    for header, numbers in header_accounts.items():
        for number in numbers:
            account_to_header[str(number)] = header

    out = {}
    for record in records:
        header = account_to_header.get(record["account"])
        if header is None or record["tab"] is None:
            continue
        label = month_label(record["month"])
        bucket = out.setdefault(record["tab"], {}).setdefault(label, {})
        bucket[header] = round(bucket.get(header, 0.0) + record["net"], 2)
    return out


def unapproved(records):
    return [r for r in records if not r["approved"]]


def month_range(start, end):
    """Calendar months fully covered by [start, end], as '2026-03' keys."""
    months, cursor = [], dt.date(start.year, start.month, 1)
    while cursor <= end:
        last_day = (dt.date(cursor.year + (cursor.month == 12),
                            cursor.month % 12 + 1, 1) - dt.timedelta(days=1))
        if cursor >= start and last_day <= end:
            months.append(cursor.strftime("%Y-%m"))
        cursor = last_day + dt.timedelta(days=1)
    return months


def month_bounds(month_key):
    """'2026-03' -> (date(2026,3,1), date(2026,3,31))."""
    year, month = (int(x) for x in month_key.split("-"))
    first = dt.date(year, month, 1)
    last = dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
    return first, last


def coverage_gaps(records, months):
    """Store-months with no catering journal at all.

    A missing journal is indistinguishable from a genuinely zero month unless
    you look, so this is reported rather than assumed either way.
    """
    seen = {(r["month"], r["store"]) for r in records}
    stores = sorted(STORE_TABS)
    return [(m, s) for m in months for s in stores if (m, s) not in seen]
