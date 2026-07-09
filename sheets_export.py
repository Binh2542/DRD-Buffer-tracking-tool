from pathlib import Path

import gspread

_HEADER = ["QR", "Board", "Import Time", "Attempt", "Defect"]
_COMPLETED_HEADER = ["QR", "Board", "Import Time", "Attempt", "Defect", "Complete Time", "Debug"]
_COMPLETED_TAB_NAME = "Completed from Buffer"


def _connect(key_path: Path, sheet_id: str):
    client = gspread.service_account(filename=str(key_path))
    return client.open_by_key(sheet_id)


def init_sheet(key_path: Path, sheet_id: str):
    """Connect to the first tab of a spreadsheet (the "current buffer" tab,
    whatever it's named - referenced by position so renaming it doesn't
    break anything) using the same service account as Firestore. Only this
    tab is ever touched - other tabs (e.g. for manual reports/pivot tables)
    are left alone."""
    return _connect(key_path, sheet_id).get_worksheet(0)


def init_completed_sheet(key_path: Path, sheet_id: str):
    """Connect to the "Completed from Buffer" tab, creating it if missing."""
    spreadsheet = _connect(key_path, sheet_id)
    try:
        return spreadsheet.worksheet(_COMPLETED_TAB_NAME)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=_COMPLETED_TAB_NAME, rows=1000, cols=len(_COMPLETED_HEADER))


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
