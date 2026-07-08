from pathlib import Path

import gspread

_HEADER = ["QR", "Board", "Import Time", "Attempt", "Defect"]


def init_sheet(key_path: Path, sheet_id: str):
    """Connect to the "Sheet1" tab of a spreadsheet using the same service
    account as Firestore. Only this tab is ever touched - other tabs in the
    same spreadsheet (e.g. for manual reports/pivot tables) are left alone."""
    client = gspread.service_account(filename=str(key_path))
    spreadsheet = client.open_by_key(sheet_id)
    return spreadsheet.worksheet("Sheet1")


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
