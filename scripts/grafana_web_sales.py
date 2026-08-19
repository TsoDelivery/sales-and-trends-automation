"""Carryout and Delivery revenue from the live tsochinese.com ordering DB.

WHY THIS SOURCE, NOT THE P&L
---------------------------
The reconciled P&L collapses both channels into a single "Total Grafana Sales"
line. One line cannot be apportioned across two sheet columns (M carryout,
R delivery), so the P&L can only ever *report* a combined variance. The
ordering DB has the per-order `cart.carryout` boolean, so it can attribute.

Verified 2026-08-14 against July 2026, all five stores: carryout within
0.2-0.7%, delivery within 0.1-1.6% of the hand-maintained sheet values.

NET OF TAX
----------
Sheet columns are net of sales tax. Gross `transaction.amount` runs ~8-10%
high, which is almost exactly the tax component. We subtract `sales_tax`.

THE WRONG-VALUE TRAP
--------------------
`transaction.type` is 'capture' and `transaction.status` is 'SUCCEEDED'
(uppercase). Both are Postgres ENUMs, so a typo like 'charge' or 'succeeded'
raises a hard 500 -- loud, easy to spot. The dangerous case is a *valid but
wrong* enum value: type='refund' returns rows totalling ~$1k instead of ~$325k,
and status='FAILED' returns zero rows. Neither is an error. So every result is
checked against a per-store floor before it is trusted.

Verified by fault injection (2026-08-14): the floor catches both wrong-enum
cases. It does NOT catch dropping the `test_`/`deleted_at` filters or forgetting
to subtract tax -- those shift the total by 0-8%, which is inside the plausible
range. Those are covered by unit tests on the SQL text instead.
"""

import datetime as dt
import json
import os
import subprocess

GRAFANA_URL = "https://grafana.tsochinese.com/api/ds/query"
DATASOURCE_ID = 10          # TSO-PROD
OP_ITEM = "Tsora Grafana Service Account"
OP_VAULT = "Administrative Assistants"
OP_TOKEN_FILE = "~/.op_service_account_token"
KEYCHAIN_ACCOUNT = "tsora-assistant"
KEYCHAIN_SERVICE = "grafana-token"

# location.id -> Sales & Trends tab name.
LOCATIONS = {
    1: "Cherrywood Monthly Sales ",
    2: "Arbor Monthly Sales",
    5: "TsoCo Monthly Sales",
    6: "Round Rock Monthly Sales",
    20: "Menchaca Monthly Sales",
}

# 0-based Sales & Trends column indices.
COL_CARRYOUT = 12   # M
COL_DELIVERY = 17   # R

# A real store-month is far above this. Anything lower means a wrong-but-valid
# enum value (type='refund' yields ~$1k) or an empty result, not zero sales.
SANITY_FLOOR = 1000.0

# Cents-vs-dollars hand keying is not a real disagreement.
TOLERANCE = 1.01


def month_bounds(year, month):
    start = dt.date(year, month, 1)
    end = dt.date(year + 1, 1, 1) if month == 12 else dt.date(year, month + 1, 1)
    return start, end


def build_sql(year, month):
    """Per-location, per-channel net-of-tax revenue for one calendar month."""
    start, end = month_bounds(year, month)
    ids = ",".join(str(i) for i in sorted(LOCATIONS))
    return f"""
select c.location_id,
       c.carryout,
       count(distinct c.id) as tickets,
       round(sum(t.amount - coalesce(t.sales_tax, 0))::numeric, 2) as net_ex_tax
from cart c
join transaction t on t.cart_id = c.id
where c.order_time >= '{start.isoformat()}'
  and c.order_time <  '{end.isoformat()}'
  and c.location_id in ({ids})
  and coalesce(c.test_, false) = false
  and c.deleted_at is null
  and t.type = 'capture'
  and t.status = 'SUCCEEDED'
group by c.location_id, c.carryout
""".strip()


# ------------------------------------------------------------------------ auth

def grafana_token():
    """Keychain first (fast, no network), then 1Password service account.

    Returns (token, error). Never logs or returns the value on the error path.
    """
    try:
        p = subprocess.run(
            ["security", "find-generic-password", "-a", KEYCHAIN_ACCOUNT,
             "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=30)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip(), None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # not a mac, or keychain locked -- fall through to 1Password

    path = os.path.expanduser(OP_TOKEN_FILE)
    if not os.path.exists(path):
        return None, (f"no Grafana token: keychain miss and {OP_TOKEN_FILE} absent")
    with open(path) as f:
        sa_token = f.read().strip()
    if not sa_token:
        return None, f"{OP_TOKEN_FILE} is empty"

    env = dict(os.environ, OP_SERVICE_ACCOUNT_TOKEN=sa_token)
    env.pop("OP_SESSION", None)   # a stale session can trigger a Touch ID prompt
    try:
        p = subprocess.run(
            ["op", "item", "get", OP_ITEM, "--vault", OP_VAULT,
             "--fields", "label=credential", "--reveal"],
            capture_output=True, text=True, timeout=60, env=env)
    except FileNotFoundError:
        return None, "the `op` CLI is not installed"
    except subprocess.TimeoutExpired:
        return None, ("`op` timed out after 60s -- likely a daemon pileup or "
                      "stale socket; retry or restart the op daemon")
    if p.returncode != 0:
        return None, f"`op` failed: {(p.stderr or p.stdout).strip()[:200]}"
    tok = p.stdout.strip()
    return (tok, None) if tok else (None, "1Password returned an empty credential")


# ----------------------------------------------------------------------- query

def parse_frames(payload):
    """Grafana /ds/query response -> {(location_id, carryout): {...}}."""
    result = payload.get("results", {}).get("A", {})
    if "error" in result:
        raise RuntimeError(f"Grafana query error: {str(result['error'])[:300]}")
    frames = result.get("frames") or []
    if not frames:
        return {}
    fields = [f["name"] for f in frames[0]["schema"]["fields"]]
    columns = frames[0]["data"]["values"]
    if not columns or not columns[0]:
        return {}
    out = {}
    for row in zip(*columns):
        rec = dict(zip(fields, row))
        key = (int(rec["location_id"]), bool(rec["carryout"]))
        out[key] = {"tickets": int(rec["tickets"] or 0),
                    "net": float(rec["net_ex_tax"] or 0.0)}
    return out


def fetch(year, month, token, timeout=120):
    """Run the month query. IPv4 is mandatory: IPv6 is blocked by Cloudflare WAF."""
    body = {"queries": [{"refId": "A", "datasourceId": DATASOURCE_ID,
                         "rawSql": build_sql(year, month), "format": "table"}],
            "from": "now-5y", "to": "now"}
    p = subprocess.run(
        ["curl", "-4", "-s", "--max-time", str(timeout), "-X", "POST",
         "-H", f"Authorization: Bearer {token}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(body), GRAFANA_URL],
        capture_output=True, text=True, timeout=timeout + 30)
    if p.returncode != 0:
        raise RuntimeError(f"curl failed (exit {p.returncode}): {p.stderr[:200]}")
    try:
        payload = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"non-JSON from Grafana: {p.stdout[:200]}")
    return parse_frames(payload)


def to_store_revenue(rows):
    """{(loc, carryout): {...}} -> {tab: {"carryout": net, "delivery": net, ...}}."""
    out = {}
    for (loc, is_carryout), rec in rows.items():
        tab = LOCATIONS.get(loc)
        if not tab:
            continue
        slot = out.setdefault(tab, {"carryout": None, "delivery": None,
                                    "carryout_tickets": 0, "delivery_tickets": 0})
        key = "carryout" if is_carryout else "delivery"
        slot[key] = rec["net"]
        slot[f"{key}_tickets"] = rec["tickets"]
    return out


def check_sanity(store_revenue):
    """Return a list of problems. Empty list means the shape looks credible.

    Guards the silent-empty failure mode: a broken query returns 0 rows, which
    would otherwise be written as legitimate zeros.
    """
    problems = []
    if not store_revenue:
        return ["query returned no rows at all -- check transaction type/status "
                "('capture'/'SUCCEEDED') before trusting this as zero sales"]
    missing = sorted(set(LOCATIONS.values()) - set(store_revenue))
    for tab in missing:
        problems.append(f"{tab.strip()}: no rows returned")
    for tab, rec in sorted(store_revenue.items()):
        for key in ("carryout", "delivery"):
            val = rec.get(key)
            if val is None:
                problems.append(f"{tab.strip()}: {key} missing")
            elif val < SANITY_FLOOR:
                problems.append(
                    f"{tab.strip()}: {key} is {val:,.0f}, below the "
                    f"{SANITY_FLOOR:,.0f} floor -- suspect a broken query")
    return problems


# ------------------------------------------------------------------- comparing

def allocate_pl_total(store_revenue, pl_total):
    """Split the P&L's combined "Total Grafana Sales" using Grafana's ratio.

    WHY THIS EXISTS
    ---------------
    Two facts collide:
      * The P&L is the reconciled source of truth for revenue LEVEL
        (Angell 2026-08-14), but it carries carryout+delivery as ONE line.
      * The ordering DB can attribute per order, but its own level runs ~3-5%
        above the P&L for reasons not yet explained (promos and account credits
        account for only ~1.5% of it).

    So we take the LEVEL from the P&L and the RATIO from Grafana. The two
    written cells then sum exactly to the reconciled P&L figure, while the split
    between them reflects actual order data.

    Returns (carryout, delivery) or None if the ratio cannot be computed.
    """
    if pl_total is None:
        return None
    car = store_revenue.get("carryout")
    del_ = store_revenue.get("delivery")
    if car is None or del_ is None:
        return None
    total = car + del_
    if total <= 0:
        return None
    share = car / total
    car_out = round(pl_total * share, 2)
    return car_out, round(pl_total - car_out, 2)


def compare(store_revenue, sheet_row, allow_overwrite=False, pl_total=None):
    """Compare one month-row for one store. Same finding shape as
    store_pl.compare_row, so both sources feed a single report.

    When pl_total is given, the P&L level is allocated across the two columns
    (see allocate_pl_total) and that becomes the target value. Without it, the
    raw ordering-DB figures are used.
    """
    out = []
    allocated = allocate_pl_total(store_revenue, pl_total) if pl_total else None
    basis = "P&L total split by ordering-DB ratio" if allocated else "ordering DB (net of tax)"

    def sheet_val(idx):
        if sheet_row is None or len(sheet_row) <= idx:
            return None
        s = str(sheet_row[idx] or "").replace("$", "").replace(",", "").strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None

    for i, (key, col, name) in enumerate((("carryout", COL_CARRYOUT, "Carryout"),
                                          ("delivery", COL_DELIVERY, "Delivery"))):
        src = allocated[i] if allocated else store_revenue.get(key)
        if src is None:
            continue
        cur = sheet_val(col)
        if cur is None or cur == 0.0:
            action, reason = "fill", "target cell blank"
        elif abs(src - cur) <= TOLERANCE:
            action, reason = "agree", ""
        elif allow_overwrite:
            action, reason = "update", basis
        else:
            action, reason = "report", "differs; --allow-overwrite not set"
        out.append({"col": col, "name": name, "pl": src, "sheet": cur,
                    "action": action, "reason": reason, "basis": basis,
                    "raw_db": store_revenue.get(key),
                    "tickets": store_revenue.get(f"{key}_tickets", 0)})
    return out
