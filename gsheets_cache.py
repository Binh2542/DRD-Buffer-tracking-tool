import threading
import time
from pathlib import Path

import gspread

_client = None
_spreadsheets = {}
_lock = threading.Lock()

# Sheets writes moved off the Firebase service account onto a real Google
# account (OAuth) after the company blocked that service account from
# writing to Sheets org-wide - see sheets_oauth_client.json/
# sheets_oauth_token.json, produced by the one-time oauth_login_once.py
# setup script. Firestore auth is untouched (still the service account in
# firebase_key.json) since only Sheets access was blocked, not Firestore.
#
# Both files live next to firebase_key.json (i.e. next to the exe itself,
# app_gui.py's BASE_DIR) - NOT next to this module's own __file__, which
# in a frozen PyInstaller build resolves inside the temp extraction
# directory (_MEIPASS), not next to the actual exe. key_path (still passed
# in by every caller for this exact reason) is what locates that directory.


def get_spreadsheet(key_path, sheet_id):
    """Cache the authorized gspread client and each opened Spreadsheet by
    ID, process-wide.

    Login used to do a fresh service-account auth AND a fresh open_by_key
    for every tab across sheets_export.py/pcb_choice.py/prod5_sheet.py -
    5 calls for just 2 distinct spreadsheets - which was the dominant cost
    in login time. Each is now only ever created once per run.

    Login also fetches several sheets concurrently (see app_gui.py's
    _do_login), so cache population is locked to avoid two threads racing
    to create the client/open the same spreadsheet at once.
    """
    global _client
    with _lock:
        if _client is None:
            base_dir = Path(key_path).resolve().parent
            _client = gspread.oauth(
                credentials_filename=str(base_dir / "sheets_oauth_client.json"),
                authorized_user_filename=str(base_dir / "sheets_oauth_token.json"),
                # Sheets-only, deliberately excluding the Drive scope -
                # this app only ever calls open_by_key()+reads/writes cells,
                # never anything Drive-specific, and the full Drive scope
                # is on Google's Restricted list, which blocks publishing
                # this OAuth app to production without a verification
                # review. Sheets-only avoids that entirely.
                scopes=("https://www.googleapis.com/auth/spreadsheets",),
            )
        if sheet_id not in _spreadsheets:
            _spreadsheets[sheet_id] = _client.open_by_key(sheet_id)
        return _spreadsheets[sheet_id]


def call_with_retry(fn, *args, **kwargs):
    """Call fn(*args, **kwargs), retrying up to twice more (1s, then 2s
    pause) on any failure.

    Login fires 13+ Google Sheets API calls at once (see app_gui.py's
    _do_login) - load_pcb_choice_rules/load_device_choice_rules/
    load_operator_names among them - so a transient rate-limit or network
    blip on any one of them is a real, observed failure mode, not a
    theoretical one. Their callers all treat "couldn't load this sheet"
    as "empty ruleset" rather than blocking login (so one missing sheet
    doesn't lock an operator out entirely) - which previously made a
    transient hiccup indistinguishable, for that operator's WHOLE session,
    from every device/operator name in the sheet genuinely being unmapped:
    every write silently fell back to the raw WebDB board name / raw
    login username instead of the correct display name, with nothing to
    show for it beyond one easy-to-miss warning dialog at login.
    """
    last_exc = None
    for pause in (0, 1, 2):
        if pause:
            time.sleep(pause)
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - retried below; re-raised if still failing after every attempt
            last_exc = exc
    raise last_exc


def find_row_by_key(worksheet, key_value, key_column=1):
    """Row number (1-indexed, matching gspread/Sheets) of the row whose
    key_column cell equals key_value, or None if no such row exists. Costs
    exactly one Sheets API call regardless of how many rows the sheet has -
    the search itself runs server-side, not by paging through get_all_values.
    """
    try:
        cell = worksheet.find(str(key_value), in_column=key_column)
    except gspread.exceptions.CellNotFound:
        return None
    return cell.row if cell else None


def upsert_row(worksheet, key_value, row_values, key_column=1):
    """Replace the row whose key_column cell equals key_value with
    row_values, or append a new row at the bottom if no such row exists yet.

    Used in place of the old export_*() pattern (clear() the entire sheet,
    then rewrite every single row from scratch on every single change) -
    this touches exactly the one row that actually changed (plus one
    lookup), regardless of how large the sheet has grown. New rows land at
    the bottom in the order they actually happened, instead of wherever
    Firestore's snapshot order happened to put them.
    """
    row_number = find_row_by_key(worksheet, key_value, key_column)
    if row_number is not None:
        worksheet.update(range_name=f"A{row_number}", values=[row_values])
    else:
        worksheet.append_row(row_values)


def find_row_by_two_keys(worksheet, key_value, key_column, second_value, second_column):
    """Row number of the row where key_column == key_value AND
    second_column == second_value, or None - for logs where a single
    column isn't unique enough on its own (e.g. Online Repair's QR, which
    can legitimately repeat if the same unit is repaired more than once).
    One call finds every row matching the first key; each candidate's
    second column is then checked (almost always just one candidate in
    practice, so this rarely costs more than 2 calls total)."""
    try:
        cells = worksheet.findall(str(key_value), in_column=key_column)
    except gspread.exceptions.CellNotFound:
        return None
    for cell in cells:
        row_values = worksheet.row_values(cell.row)
        if len(row_values) >= second_column and row_values[second_column - 1] == str(second_value):
            return cell.row
    return None


def delete_row_by_two_keys(worksheet, key_value, key_column, second_value, second_column):
    """Delete the row matched by find_row_by_two_keys, if any - a no-op
    when no such row exists."""
    row_number = find_row_by_two_keys(worksheet, key_value, key_column, second_value, second_column)
    if row_number is not None:
        worksheet.delete_rows(row_number)


def delete_row_by_key(worksheet, key_value, key_column=1):
    """Delete the row whose key_column cell equals key_value, if any - a
    no-op (not an error) when no such row exists, matching
    firestore_client._delete_from's same tolerance for a key that was
    never actually there."""
    row_number = find_row_by_key(worksheet, key_value, key_column)
    if row_number is not None:
        worksheet.delete_rows(row_number)
