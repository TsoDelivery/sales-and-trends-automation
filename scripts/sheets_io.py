"""Shared Google Sheets access for the Sales & Trends workbook.

Factored out of ingest-catering-pl.py so the R365 catering source and the
validator use exactly the same read/write path -- one auth story, one place to
fix a bug.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def load_env():
    """Populate os.environ from the repo .env, without clobbering real env vars."""
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


def sheets_service(verbose=False):
    """Sheets client authenticated as THIS repo's service account.

    Credential precedence matters more than it looks. The repo's own key wins
    over the ambient GOOGLE_APPLICATION_CREDENTIALS, because that variable is
    commonly exported in a login shell for something else entirely -- on this
    machine it points at a GA4 analytics key that has no access to the Sales &
    Trends workbook. Honouring it produced a 403 "caller does not have
    permission" that looked exactly like rate-limiting, survived a 120-second
    retry ladder, and only ever appeared in background runs (which use a login
    shell) while every interactive run succeeded.

    A wrong-identity 403 is indistinguishable from a throttling 403 unless you
    print which identity you are using. So print it.
    """
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    repo_key = REPO / ".secrets" / "google-service-account.json"
    explicit = os.environ.get("SALES_TRENDS_GOOGLE_CREDENTIALS")
    if explicit:
        creds_path = explicit
    elif repo_key.exists():
        creds_path = str(repo_key)
    else:
        creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")

    if not creds_path or not Path(creds_path).exists():
        raise SystemExit(
            "Google credentials not found. Expected the repo key at "
            f"{repo_key}, or set SALES_TRENDS_GOOGLE_CREDENTIALS.")

    creds = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    if verbose:
        print(f"Auth:   {creds.service_account_email}", file=sys.stderr)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_tabs(service, sheet_id, tabs, attempts=5):
    """{tab: [[cell, ...], ...]} for each tab, unformatted values.

    Retries on transient failures. The Sheets API intermittently returns 403
    "The caller does not have permission" for a service account that genuinely
    has access -- it is quota/rate pressure wearing a permissions mask, and a
    one-off 403 should not look like a misconfiguration.

    Backoff is deliberately long (5s, 15s, 35s, 65s): read quota is enforced per
    60-second window, so a 7-second retry ladder exhausts every attempt inside
    the same window it is being throttled by and fails for no reason. Ask how
    long the wall lasts before choosing how long to wait.
    """
    import time
    from googleapiclient.errors import HttpError

    out = {}
    for tab in tabs:
        last = None
        for attempt in range(attempts):
            try:
                result = service.spreadsheets().values().get(
                    spreadsheetId=sheet_id,
                    range=f"'{tab}'!A1:BZ60",
                    valueRenderOption="UNFORMATTED_VALUE",
                ).execute()
                out[tab] = result.get("values", [])
                break
            except HttpError as exc:
                last = exc
                transient = exc.resp.status in (403, 429, 500, 502, 503)
                if not transient or attempt == attempts - 1:
                    raise
                delay = (5, 15, 35, 65)[min(attempt, 3)]
                print(f"  transient {exc.resp.status} reading {tab!r}; "
                      f"retrying in {delay}s ({attempt + 1}/{attempts - 1})",
                      file=sys.stderr)
                time.sleep(delay)
        else:  # pragma: no cover - defensive, loop always breaks or raises
            raise RuntimeError(f"could not read tab {tab!r}: {last}")
    return out


def write_updates(service, sheet_id, updates):
    """Batch-write [{tab, row, column, value}, ...]. Returns cells updated."""
    data = [{"range": f"'{u['tab']}'!{u['column']}{u['row']}", "values": [[u["value"]]]}
            for u in updates]
    result = service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()
    return result.get("totalUpdatedCells", 0)


def row_index_by_label(rows):
    """First row number (1-based) for each column-A label."""
    out = {}
    for i, row in enumerate(rows):
        key = str(row[0]).strip() if row else ""
        if key and key not in out:
            out[key] = i + 1
    return out
