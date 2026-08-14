#!/usr/bin/env python3
"""Ingest catering revenue from the TRIS Preliminary Financial Statement email.

Pipeline: Gmail (subject match) -> .xlsx attachment -> GL-label extraction ->
compare to the live Sales & Trends sheet -> write ONLY blank cells.

Default is a DRY RUN. Nothing is written without --write.

Safety rules, all enforced in catering_pl.plan_updates and covered by tests:
  * Blank target cell   -> fill.
  * Agrees (<$0.50)     -> no-op.
  * Real disagreement   -> SKIPPED and reported. Requires --allow-overwrite.
  * EZCater BK populated-> BLOCKED, never auto-resolved (double-count risk).
  * Tax-exempt accounts -> never written; they are inside the Total rows.

Usage:
  python3 scripts/ingest-catering-pl.py                      # newest closed period, dry run
  python3 scripts/ingest-catering-pl.py --period 8 --year 2026
  python3 scripts/ingest-catering-pl.py --file pkg.xlsx      # skip Gmail
  python3 scripts/ingest-catering-pl.py --write
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import catering_pl as cp
import fiscal_calendar as fc

REPO = Path(__file__).resolve().parent.parent
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# ---------------------------------------------------------------- environment

def load_env():
    env_path = REPO / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def spreadsheet_id():
    value = os.environ.get("SALES_TRENDS_SPREADSHEET_ID", "")
    if not value:
        raise SystemExit("SALES_TRENDS_SPREADSHEET_ID is not set (.env or environment)")
    return value


# ---------------------------------------------------------------------- gmail

def run(cmd, timeout=180):
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({' '.join(cmd[:3])}...): {proc.stderr.strip()[:300]}")
    return proc.stdout


def find_statement_email(year, period):
    """Newest message whose subject matches the TRIS package for this period."""
    subject = fc.subject_for(year, period)
    query = f'in:anywhere subject:("{subject}")'
    out = run(["gog", "gmail", "search", query, "--plain"])
    lines = [l for l in out.splitlines()[1:] if l.strip()]
    if not lines:
        # Fall back to a looser match: TRIS occasionally reformats the subject.
        loose = f'in:anywhere subject:("Preliminary Financial Statement") "P{period:02d}"'
        out = run(["gog", "gmail", "search", loose, "--plain"])
        lines = [l for l in out.splitlines()[1:] if l.strip()]
    if not lines:
        return None
    fields = lines[0].split("\t")
    return {"id": fields[0], "date": fields[1] if len(fields) > 1 else "",
            "sender": fields[2] if len(fields) > 2 else "",
            "subject": fields[3] if len(fields) > 3 else ""}


def statement_attachment(message_id, year, period):
    """Attachment id of the '<Pnn> <year> - TSO Preliminary Financial Statements.xlsx'."""
    payload = json.loads(run(["gog", "gmail", "read", message_id, "--json"]))
    parts, stack = [], [payload["thread"]["messages"][0]["payload"]]
    while stack:
        part = stack.pop()
        parts.append(part)
        stack.extend(part.get("parts") or [])

    candidates = []
    for part in parts:
        name = part.get("filename") or ""
        att_id = (part.get("body") or {}).get("attachmentId")
        if not att_id or not name.lower().endswith((".xlsx", ".xls")):
            continue
        # Reject the fixed-assets workbook; we want the statements package.
        if "fixed asset" in name.lower():
            continue
        score = 0
        if "preliminary financial statement" in name.lower():
            score += 2
        if f"p{period:02d}" in name.lower().replace(" ", ""):
            score += 2
        if str(year) in name:
            score += 1
        candidates.append((score, name, att_id))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    _, name, att_id = candidates[0]
    return {"filename": name, "attachment_id": att_id}


def download_attachment(message_id, attachment_id, dest):
    run(["gog", "gmail", "attachment", message_id, attachment_id, "--out", str(dest)], timeout=300)
    if not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError(f"attachment download produced no data at {dest}")
    return dest


# --------------------------------------------------------------------- sheets

def sheets_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds_path = os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS", str(REPO / ".secrets" / "google-service-account.json")
    )
    if not Path(creds_path).exists():
        raise SystemExit(f"Google credentials not found: {creds_path}")
    creds = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_tabs(service, sheet_id, tabs):
    out = {}
    for tab in tabs:
        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"'{tab}'!A1:BZ60",
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute()
        out[tab] = result.get("values", [])
    return out


def write_updates(service, sheet_id, updates):
    data = [{"range": f"'{u['tab']}'!{u['column']}{u['row']}", "values": [[u["value"]]]}
            for u in updates]
    result = service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()
    return result.get("totalUpdatedCells", 0)


# ----------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="Ingest catering revenue from the TRIS P&L email.")
    ap.add_argument("--period", type=int, help="fiscal period 1-13 (default: newest closed)")
    ap.add_argument("--year", type=int, help="fiscal year (default: newest closed)")
    ap.add_argument("--file", help="use a local .xlsx instead of searching Gmail")
    ap.add_argument("--all-periods", action="store_true",
                    help="ingest every period in the workbook, not just the target")
    ap.add_argument("--write", action="store_true", help="apply changes (default: dry run)")
    ap.add_argument("--allow-overwrite", action="store_true",
                    help="also overwrite cells that disagree with the P&L")
    ap.add_argument("--as-of", help="treat this YYYY-MM-DD as today (for testing)")
    args = ap.parse_args(argv)

    load_env()

    today = dt.date.fromisoformat(args.as_of) if args.as_of else dt.date.today()
    if args.period and args.year:
        year, period = args.year, args.period
    elif args.period or args.year:
        raise SystemExit("--period and --year must be given together")
    else:
        year, period = fc.most_recent_closed_period(today)

    start, end = fc.period_range(year, period)
    target_label = fc.label_for(year, period)
    print(f"Target: P{period:02d} {year}  ({start} to {end})  sheet row '{target_label}'")

    tmpdir = None
    if args.file:
        workbook_path = Path(args.file).expanduser()
        if not workbook_path.exists():
            raise SystemExit(f"file not found: {workbook_path}")
        print(f"Source: local file {workbook_path.name}")
    else:
        print(f'Searching Gmail for: "{fc.subject_for(year, period)}"')
        email = find_statement_email(year, period)
        if not email:
            print(f"\nNOT FOUND: no email yet for P{period:02d} {year}. Nothing written.")
            return 0
        print(f"Found: {email['date']}  from {email['sender']}")
        print(f"       {email['subject']}")
        attachment = statement_attachment(email["id"], year, period)
        if not attachment:
            print("\nERROR: email found but it has no statements .xlsx attachment.")
            return 1
        print(f"Attachment: {attachment['filename']}")
        tmpdir = tempfile.TemporaryDirectory()
        workbook_path = download_attachment(
            email["id"], attachment["attachment_id"], Path(tmpdir.name) / "statements.xlsx"
        )

    try:
        import openpyxl
        wb = openpyxl.load_workbook(workbook_path, data_only=True)
        extracted, findings = cp.extract_workbook(wb)

        if not extracted:
            print("\nERROR: no catering data extracted -- P&L layout may have changed.")
            for f in findings:
                print(f"  ! {f}")
            return 1

        service = sheets_service()
        sheet_id = spreadsheet_id()
        sheet_values = read_tabs(service, sheet_id, sorted(extracted.keys()))

        only = None if args.all_periods else target_label
        updates, report = cp.plan_updates(
            extracted, sheet_values, allow_overwrite=args.allow_overwrite, only_period=only
        )

        counts = cp.summarize(report)
        print(f"\nPlan: {counts.get('fill', 0)} to fill, {counts.get('match', 0)} already correct, "
              f"{counts.get('diff', 0)} disagree, {counts.get('blocked', 0)} blocked, "
              f"{counts.get('no_row', 0)} missing row")

        by_status = {}
        for item in report:
            by_status.setdefault(item["status"], []).append(item)

        for status, header in [
            ("fill", "WILL WRITE (sheet is blank)"),
            ("diff", "DISAGREES -- skipped unless --allow-overwrite"),
            ("blocked", "BLOCKED -- needs a human decision"),
            ("no_row", "NO SHEET ROW for that period"),
        ]:
            items = by_status.get(status)
            if not items:
                continue
            print(f"\n{header}:")
            for it in items:
                pl = f"{it['pl']:,.2f}" if isinstance(it["pl"], (int, float)) else "-"
                sh = it["sheet"]
                sh = f"{sh:,.2f}" if isinstance(sh, (int, float)) else (str(sh) or "<blank>")
                line = f"  {it['tab'][:26]:28} {it['period']:9} {it['column']:3} P&L={pl:>12} sheet={sh:>12}"
                if it.get("delta") is not None:
                    line += f"  delta={it['delta']:>10,.2f}"
                if it.get("reason"):
                    line += f"  [{it['reason']}]"
                print(line)

        if findings:
            print("\nNotes:")
            for f in findings:
                print(f"  ! {f}")

        if not updates:
            print("\nNothing to write.")
            return 0

        if not args.write:
            print(f"\nDRY RUN: {len(updates)} cell(s) would be written. Re-run with --write.")
            return 0

        written = write_updates(service, sheet_id, updates)
        print(f"\nWrote {written} cell(s). Verifying...")

        # Read back: the write is not trusted until the sheet confirms it.
        verify_values = read_tabs(service, sheet_id, sorted({u["tab"] for u in updates}))
        bad = []
        for u in updates:
            rows = verify_values.get(u["tab"], [])
            idx = u["row"] - 1
            row = rows[idx] if len(rows) > idx else []
            ci = cp.column_index(u["column"])
            actual = row[ci] if len(row) > ci else ""
            try:
                ok = abs(float(actual) - u["value"]) < 0.005
            except (TypeError, ValueError):
                ok = False
            if not ok:
                bad.append((u, actual))

        if bad:
            print(f"VERIFY FAILED for {len(bad)} cell(s):")
            for u, actual in bad:
                print(f"  {u['tab']} {u['column']}{u['row']}: expected {u['value']}, found {actual!r}")
            return 1

        print(f"Verified {len(updates)} cell(s) match the P&L cent-for-cent.")
        return 0
    finally:
        if tmpdir:
            tmpdir.cleanup()


if __name__ == "__main__":
    sys.exit(main())
