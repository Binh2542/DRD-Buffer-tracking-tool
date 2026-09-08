"""One-off script: pull the FULL Online Repair history (not just the live
3-day window app_gui.py normally syncs - see ONLINE_REPAIR_LIVE_WINDOW_DAYS
there) out of Firestore, Defect column included, into two new sheet tabs
that the live sync never touches:
    - "Online Repair Archive"              (Debug mode)
    - "Assembly Rework Online Repair Archive"  (Assembly Rework mode)

Safe to re-run any time you want the archive brought up to date - each run
fully rebuilds both tabs from Firestore, it never reads or depends on
anything already in the sheet.

Usage: python backfill_online_repair_archive.py
"""
import os
from pathlib import Path

import gspread
from dotenv import load_dotenv

from firestore_client import (
    AR_ONLINE_REPAIR_COLLECTION,
    ONLINE_REPAIR_COLLECTION,
    fetch_online_repairs_since,
    init_firestore,
)
from gsheets_cache import get_spreadsheet

BASE_DIR = Path(__file__).resolve().parent
_EPOCH_ISO = "1970-01-01T00:00:00+00:00"  # "since" the beginning of time - i.e. no lower bound
_HEADER = ["QR", "Board", "Repair Time", "Debug", "Defect"]


def _write_archive_tab(spreadsheet, title, records):
    records = sorted(records, key=lambda r: r.get("repair_time_iso") or "", reverse=True)
    rows = [_HEADER]
    for record in records:
        rows.append([
            record.get("qr", ""),
            record.get("board", ""),
            record.get("repair_time", ""),
            record.get("debug_operator") or "No info",
            record.get("defect", ""),
        ])
    try:
        worksheet = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=title, rows=max(1000, len(rows) + 10), cols=len(_HEADER))
    if worksheet.row_count < len(rows):
        worksheet.resize(rows=len(rows) + 10)
    worksheet.clear()
    worksheet.update(range_name="A1", values=rows)
    print(f'  "{title}": {len(records)} record(s) written.')


def main():
    load_dotenv(BASE_DIR / ".env")
    key_path = BASE_DIR / "firebase_key.json"
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    if not sheet_id:
        raise SystemExit("GOOGLE_SHEET_ID is not set in .env")

    print("Connecting to Firestore...")
    db = init_firestore(key_path)
    spreadsheet = get_spreadsheet(key_path, sheet_id)

    print("Fetching full Debug Online Repair history...")
    debug_records = fetch_online_repairs_since(db, _EPOCH_ISO, collection=ONLINE_REPAIR_COLLECTION)
    print("Fetching full Assembly Rework Online Repair history...")
    ar_records = fetch_online_repairs_since(db, _EPOCH_ISO, collection=AR_ONLINE_REPAIR_COLLECTION)

    print("Writing archive tabs...")
    _write_archive_tab(spreadsheet, "Online Repair Archive", debug_records)
    _write_archive_tab(spreadsheet, "Assembly Rework Online Repair Archive", ar_records)
    print("Done.")


if __name__ == "__main__":
    main()
