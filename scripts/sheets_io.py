"""Shared Google Sheets access for the Sales & Trends workbook.

Factored out of ingest-catering-pl.py so the R365 catering source and the
validator use exactly the same read/write path -- one auth story, one place to
fix a bug.
"""

from __future__ import annotations

import os
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


def sheets_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds_path = os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS",
        str(REPO / ".secrets" / "google-service-account.json"),
    )
    if not Path(creds_path).exists():
        raise SystemExit(f"Google credentials not found: {creds_path}")
    creds = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_tabs(service, sheet_id, tabs, attempts=4):
    """{tab: [[cell, ...], ...]} for each tab, unformatted values.

    Retries on transient failures: the Sheets API intermittently returns 403
    "The caller does not have permission" for a service account that genuinely
    has access, and a one-off 403 should not look like a misconfiguration.
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
                if exc.resp.status not in (403, 429, 500, 502, 503) or attempt == attempts - 1:
                    raise
                time.sleep(2 ** attempt)
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
