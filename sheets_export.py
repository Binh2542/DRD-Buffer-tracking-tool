from pathlib import Path

import gspread

from gsheets_cache import (
    call_with_retry,
    delete_row_by_key,
    delete_row_by_two_keys,
    find_row_by_key,
    get_spreadsheet,
    upsert_row,
)
from webdb_client import parse_gmt7_formatted_to_iso

_HEADER = ["QR", "Board", "Import Time", "Attempt", "Defect"]
_COMPLETED_HEADER = ["QR", "Board", "Import Time", "Attempt", "Defect", "Complete Time", "Debug"]
_COMPLETED_TAB_NAME = "Debug_Completed"
_ONLINE_REPAIR_HEADER = ["QR", "Board", "Repair Time", "Debug", "Defect"]
_ONLINE_REPAIR_TAB_NAME = "Debug_Online"
_MRB_HEADER = ["QR", "Board", "MRB Time", "Debug", "Defect"]
_MRB_TAB_NAME = "MRB"
# Shared across Debug/Assembly Rework (see firestore_client.QR_CHANGES_COLLECTION) -
# one tab, not a debug/AR pair like the others above.
_QR_CHANGE_HEADER = ["PCB QR", "Previous QR", "Current QR", "Model", "Time"]
_QR_CHANGE_TAB_NAME = "QR Change"
_USER_TAB_NAME = "User"

# Assembly Rework mode's own tabs - same column layout as Debug mode's,
# just kept on separate sheets so the two workflows' data never mixes.
_AR_TAB_NAME = "AR_Buffer"
_AR_COMPLETED_TAB_NAME = "AR_Completed"
_AR_ONLINE_REPAIR_TAB_NAME = "AR_Online"
_AR_MRB_TAB_NAME = "Assembly Rework MRB"


_CURRENT_BUFFER_TAB_NAME = "Debug_Buffer"


def init_sheet(key_path: Path, sheet_id: str):
    """Connect to the "Current Buffer" tab (Debug mode's live buffer
    mirror), creating it if missing.

    This used to be looked up by POSITION (get_worksheet(0), "whichever
    tab is first"), on the assumption that tabs might get renamed but
    never reordered - confirmed wrong live: someone reordered the tabs so
    "Online Repair" ended up first, and every Debug Buffer export
    (export_devices) silently started overwriting the Online Repair tab
    instead, racing with online repair's own (correctly name-based)
    export onto the same physical tab. Every other sheet in this file
    already looks up by name for exactly this reason - this was the one
    exception."""
    spreadsheet = get_spreadsheet(key_path, sheet_id)
    try:
        return spreadsheet.worksheet(_CURRENT_BUFFER_TAB_NAME)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=_CURRENT_BUFFER_TAB_NAME, rows=1000, cols=len(_HEADER))


def init_completed_sheet(key_path: Path, sheet_id: str):
    """Connect to the "Completed from Buffer" tab, creating it if missing."""
    spreadsheet = get_spreadsheet(key_path, sheet_id)
    try:
        return spreadsheet.worksheet(_COMPLETED_TAB_NAME)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=_COMPLETED_TAB_NAME, rows=1000, cols=len(_COMPLETED_HEADER))


def init_online_repair_sheet(key_path: Path, sheet_id: str):
    """Connect to the "Online Repair" tab, creating it if missing."""
    spreadsheet = get_spreadsheet(key_path, sheet_id)
    try:
        return spreadsheet.worksheet(_ONLINE_REPAIR_TAB_NAME)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(
            title=_ONLINE_REPAIR_TAB_NAME, rows=1000, cols=len(_ONLINE_REPAIR_HEADER)
        )


def init_ar_sheet(key_path: Path, sheet_id: str):
    """Connect to the "Assembly Rework" tab (that mode's own current-buffer
    list), creating it if missing - unlike init_sheet's Debug buffer tab,
    this one has no natural "first position" to claim, so it's always
    looked up by name."""
    spreadsheet = get_spreadsheet(key_path, sheet_id)
    try:
        return spreadsheet.worksheet(_AR_TAB_NAME)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=_AR_TAB_NAME, rows=1000, cols=len(_HEADER))


def init_ar_completed_sheet(key_path: Path, sheet_id: str):
    """Connect to the "Assembly Rework Completed" tab, creating it if missing."""
    spreadsheet = get_spreadsheet(key_path, sheet_id)
    try:
        return spreadsheet.worksheet(_AR_COMPLETED_TAB_NAME)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(
            title=_AR_COMPLETED_TAB_NAME, rows=1000, cols=len(_COMPLETED_HEADER)
        )


def init_ar_online_repair_sheet(key_path: Path, sheet_id: str):
    """Connect to the "Assembly Rework Online Repair" tab, creating it if missing."""
    spreadsheet = get_spreadsheet(key_path, sheet_id)
    try:
        return spreadsheet.worksheet(_AR_ONLINE_REPAIR_TAB_NAME)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(
            title=_AR_ONLINE_REPAIR_TAB_NAME, rows=1000, cols=len(_ONLINE_REPAIR_HEADER)
        )


def init_mrb_sheet(key_path: Path, sheet_id: str):
    """Connect to the "MRB" tab, creating it if missing."""
    spreadsheet = get_spreadsheet(key_path, sheet_id)
    try:
        return spreadsheet.worksheet(_MRB_TAB_NAME)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=_MRB_TAB_NAME, rows=1000, cols=len(_MRB_HEADER))


def init_ar_mrb_sheet(key_path: Path, sheet_id: str):
    """Connect to the "Assembly Rework MRB" tab, creating it if missing."""
    spreadsheet = get_spreadsheet(key_path, sheet_id)
    try:
        return spreadsheet.worksheet(_AR_MRB_TAB_NAME)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=_AR_MRB_TAB_NAME, rows=1000, cols=len(_MRB_HEADER))


def init_qr_change_sheet(key_path: Path, sheet_id: str):
    """Connect to the "QR Change" tab, creating it if missing."""
    spreadsheet = get_spreadsheet(key_path, sheet_id)
    try:
        return spreadsheet.worksheet(_QR_CHANGE_TAB_NAME)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=_QR_CHANGE_TAB_NAME, rows=1000, cols=len(_QR_CHANGE_HEADER))


def _fetch_user_rows(key_path: Path, sheet_id: str):
    worksheet = get_spreadsheet(key_path, sheet_id).worksheet(_USER_TAB_NAME)
    return worksheet.get_all_values()


def load_operator_names(key_path: Path, sheet_id: str) -> dict:
    """Load the "User" tab's In-app username -> In-sheet display name
    mapping - Fact-D&R's Operator column expects real names (e.g. "Anh Tran
    Van"), not the short app login (e.g. "anhtran")."""
    rows = call_with_retry(_fetch_user_rows, key_path, sheet_id)[1:]  # skip header
    return {row[0].strip(): row[1].strip() for row in rows if len(row) >= 2 and row[0].strip()}


def export_devices(worksheet, devices: list[dict]) -> None:
    """Overwrite the sheet with the current full device list (report mirror)."""
    rows = [_HEADER]
    for device in devices:
        rows.append([
            device.get("qr", ""),
            device.get("board", ""),
            device.get("import_time", ""),
            device.get("attempt_count", ""),
            device.get("defect", ""),
        ])
    worksheet.clear()
    worksheet.update(range_name="A1", values=rows)


def _device_row(device: dict) -> list:
    return [
        device.get("qr", ""),
        device.get("board", ""),
        device.get("import_time", ""),
        device.get("attempt_count", ""),
        device.get("defect", ""),
    ]


def sync_device_upserted(worksheet, device: dict) -> None:
    """Add or update this one device's row in a Buffer sheet - used at the
    point of the scan itself instead of export_devices' clear()+rewrite-
    everything on every Firestore snapshot. A new device lands as a new row
    at the bottom (the order it actually happened), and touching one row
    costs the same whether the sheet has 10 rows or 10,000."""
    call_with_retry(upsert_row, worksheet, device.get("qr", ""), _device_row(device))


def sync_device_removed(worksheet, qr: str) -> None:
    """Remove one device's row from a Buffer sheet - used when it leaves
    Buffer (completed, sent to MRB, or manually deleted)."""
    call_with_retry(delete_row_by_key, worksheet, qr)


def read_devices_from_sheet(worksheet) -> list[dict]:
    """Read the current Buffer sheet back into the same shape export_devices
    writes it in - used by the "Load" button (see app_gui.py) to refresh
    the on-screen list from the sheet instead of a permanent Firestore
    listener. Rows a human has hand-edited into something unparseable
    (missing a QR, say) are skipped rather than raising - one bad row
    shouldn't make the whole Load fail."""
    devices = []
    for row in worksheet.get_all_values()[1:]:
        row = (row + [""] * 5)[:5]
        qr = row[0].strip()
        if not qr:
            continue
        devices.append({
            "qr": qr,
            "board": row[1],
            "import_time": row[2],
            "import_time_iso": parse_gmt7_formatted_to_iso(row[2]),
            "attempt_count": row[3],
            "defect": row[4],
        })
    return devices


def export_completed_devices(worksheet, devices: list[dict]) -> None:
    """Overwrite the "Completed from Buffer" tab with devices that passed
    Prog Main and were moved out of the active buffer."""
    rows = [_COMPLETED_HEADER]
    for device in devices:
        rows.append([
            device.get("qr", ""),
            device.get("board", ""),
            device.get("import_time", ""),
            device.get("attempt_count", ""),
            device.get("defect", ""),
            device.get("complete_time", ""),
            device.get("debug_operator") or "No info",
        ])
    worksheet.clear()
    worksheet.update(range_name="A1", values=rows)


def _completed_row(device: dict) -> list:
    return [
        device.get("qr", ""),
        device.get("board", ""),
        device.get("import_time", ""),
        device.get("attempt_count", ""),
        device.get("defect", ""),
        device.get("complete_time", ""),
        device.get("debug_operator") or "No info",
    ]


def sync_completed_upserted(worksheet, device: dict) -> None:
    """Add or update one device's row in a Completed sheet - keyed by QR,
    same reasoning as sync_device_upserted for Buffer."""
    call_with_retry(upsert_row, worksheet, device.get("qr", ""), _completed_row(device))


def sync_completed_removed(worksheet, qr: str) -> None:
    call_with_retry(delete_row_by_key, worksheet, qr)


def read_completed_from_sheet(worksheet) -> list[dict]:
    """Read the Completed sheet back into records shaped like
    export_completed_devices writes them - used for the on-demand fetch
    (see app_gui.py's _fetch_on_demand) instead of a Firestore
    list_devices() read."""
    devices = []
    for row in worksheet.get_all_values()[1:]:
        row = (row + [""] * 7)[:7]
        qr = row[0].strip()
        if not qr:
            continue
        devices.append({
            "qr": qr,
            "board": row[1],
            "import_time": row[2],
            "import_time_iso": parse_gmt7_formatted_to_iso(row[2]),
            "attempt_count": row[3],
            "defect": row[4],
            "complete_time": row[5],
            "complete_time_iso": parse_gmt7_formatted_to_iso(row[5]),
            "debug_operator": row[6],
        })
    return devices


def export_online_repairs(worksheet, records: list[dict]) -> None:
    """Overwrite the "Online Repair" tab with the full online-repair log."""
    rows = [_ONLINE_REPAIR_HEADER]
    for record in records:
        rows.append([
            record.get("qr", ""),
            record.get("board", ""),
            record.get("repair_time", ""),
            record.get("debug_operator") or "No info",
            record.get("defect", ""),
        ])
    worksheet.clear()
    worksheet.update(range_name="A1", values=rows)


def sync_online_repair_added(worksheet, record: dict) -> None:
    """Append one online-repair event to its sheet - this is an append-only
    log (the same QR can legitimately repeat, repaired again later), so
    unlike Buffer's upsert this is always a plain append, never an update."""
    call_with_retry(
        worksheet.append_row,
        [
            record.get("qr", ""),
            record.get("board", ""),
            record.get("repair_time", ""),
            record.get("debug_operator") or "No info",
            record.get("defect", ""),
        ],
    )


def sync_online_repair_removed(worksheet, qr: str, repair_time: str) -> None:
    """Remove one online-repair row, matched by QR + Repair Time together
    (column 1 + column 3) - QR alone isn't unique here (repeat repairs),
    and this sheet has no separate id column of its own to key on instead."""
    call_with_retry(delete_row_by_two_keys, worksheet, qr, 1, repair_time, 3)


def read_online_repairs_from_sheet(worksheet) -> list[dict]:
    """Read the Online Repair sheet back into records shaped like
    export_online_repairs writes them - used by "Load" (see app_gui.py).
    "_doc_id" here is a synthetic, session-local id (the sheet row number
    at load time) - NOT a Firestore document id - just enough for the UI's
    existing checkbox-select-by-id machinery to have something stable to
    key each displayed row on."""
    records = []
    for i, row in enumerate(worksheet.get_all_values()[1:], start=2):
        row = (row + [""] * 5)[:5]
        qr = row[0].strip()
        if not qr:
            continue
        records.append({
            "_doc_id": f"row{i}",
            "qr": qr,
            "board": row[1],
            "repair_time": row[2],
            "repair_time_iso": parse_gmt7_formatted_to_iso(row[2]),
            "debug_operator": row[3],
            "defect": row[4],
        })
    return records


def export_mrb_devices(worksheet, devices: list[dict]) -> None:
    """Overwrite the "MRB" tab with the full MRB (scrap) log."""
    rows = [_MRB_HEADER]
    for device in devices:
        rows.append([
            device.get("qr", ""),
            device.get("board", ""),
            device.get("mrb_time", ""),
            device.get("debug_operator") or "No info",
            device.get("defect", ""),
        ])
    worksheet.clear()
    worksheet.update(range_name="A1", values=rows)


def sync_mrb_upserted(worksheet, device: dict) -> None:
    """Add or update one MRB row - keyed by QR, like Buffer/Completed (a
    given QR is only ever scrapped once, see firestore_client.add_mrb_device)."""
    call_with_retry(upsert_row, worksheet, device.get("qr", ""), [
        device.get("qr", ""),
        device.get("board", ""),
        device.get("mrb_time", ""),
        device.get("debug_operator") or "No info",
        device.get("defect", ""),
    ])


def sync_mrb_removed(worksheet, qr: str) -> None:
    call_with_retry(delete_row_by_key, worksheet, qr)


def read_mrb_from_sheet(worksheet) -> list[dict]:
    """Read the MRB sheet back into records shaped like export_mrb_devices
    writes them - used for the on-demand fetch (see app_gui.py's
    _fetch_on_demand) instead of a Firestore list_devices() read."""
    devices = []
    for row in worksheet.get_all_values()[1:]:
        row = (row + [""] * 5)[:5]
        qr = row[0].strip()
        if not qr:
            continue
        devices.append({
            "qr": qr,
            "board": row[1],
            "mrb_time": row[2],
            "mrb_time_iso": parse_gmt7_formatted_to_iso(row[2]),
            "debug_operator": row[3],
            "defect": row[4],
        })
    return devices


def export_qr_changes(worksheet, records: list[dict]) -> None:
    """Overwrite the "QR Change" tab with the full paper-QR-reassignment
    tracking log - shared across Debug/Assembly Rework, so this is the
    only export* function here not called once per mode."""
    rows = [_QR_CHANGE_HEADER]
    for record in records:
        rows.append([
            record.get("pcb_qr", ""),
            record.get("previous_qr", ""),
            record.get("current_qr", ""),
            record.get("model", ""),
            record.get("scan_time", ""),
        ])
    worksheet.clear()
    worksheet.update(range_name="A1", values=rows)


def sync_qr_change_upserted(worksheet, record: dict) -> None:
    """Add or update one QR Change row - keyed by pcb_qr (see
    firestore_client.add_qr_change: the permanent identifier, not the
    reassignable paper QR)."""
    call_with_retry(upsert_row, worksheet, record.get("pcb_qr", ""), [
        record.get("pcb_qr", ""),
        record.get("previous_qr", ""),
        record.get("current_qr", ""),
        record.get("model", ""),
        record.get("scan_time", ""),
    ])


def sync_qr_change_current_updated(worksheet, pcb_qr: str, current_qr: str) -> None:
    """Update just the "Current QR" cell (column C) of an already-tracked
    PCB's row - the sheet equivalent of firestore_client.
    update_qr_change_current, which only ever touches that one field too."""
    row_number = call_with_retry(find_row_by_key, worksheet, pcb_qr, 1)
    if row_number is not None:
        call_with_retry(worksheet.update, range_name=f"C{row_number}", values=[[current_qr]])


def sync_qr_change_removed(worksheet, pcb_qr: str) -> None:
    call_with_retry(delete_row_by_key, worksheet, pcb_qr)


def read_qr_changes_from_sheet(worksheet) -> list[dict]:
    """Read the QR Change sheet back into records shaped like
    export_qr_changes writes them - used for the on-demand fetch."""
    records = []
    for row in worksheet.get_all_values()[1:]:
        row = (row + [""] * 5)[:5]
        pcb_qr = row[0].strip()
        if not pcb_qr:
            continue
        records.append({
            "pcb_qr": pcb_qr,
            "previous_qr": row[1],
            "current_qr": row[2],
            "model": row[3],
            "scan_time": row[4],
            "scan_time_iso": parse_gmt7_formatted_to_iso(row[4]),
        })
    return records
