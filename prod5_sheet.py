import datetime as dt

import gspread

_FROM_TAB_NAME = "FROM"
_GMT7 = dt.timezone(dt.timedelta(hours=7))


def _connect(key_path, sheet_id):
    client = gspread.service_account(filename=str(key_path))
    return client.open_by_key(sheet_id)


def init_from_sheet(key_path, sheet_id):
    """Connect to the "FROM" tab of the "Buffer Debug PROD5 VTP" spreadsheet."""
    return _connect(key_path, sheet_id).worksheet(_FROM_TAB_NAME)


def current_shift(now_gmt7: dt.datetime) -> str:
    """Day shift is 08:00-20:00 GMT+7, Night shift is the rest."""
    return "Day" if 8 <= now_gmt7.hour < 20 else "Night"


def board_colour_for(color) -> str:
    if not color:
        return "No Colour"
    color = color.strip().lower()
    if color == "white":
        return "W QR"
    if color == "black":
        return "B QR"
    return "No Colour"


def record_from_scan(worksheet, board: str, color) -> None:
    """Record one "new QR added to buffer" event in the FROM sheet.

    The first scan of a given day+shift+type (Board+BoardColour) creates a
    new line with Qty 1. Any further matching scan in that same day+shift
    bumps that same line's Qty instead of creating a duplicate line - rows
    are appended chronologically, so today's entries (if any) sit at the
    tail and scanning stops as soon as an older Period is reached.
    """
    now_gmt7 = dt.datetime.now(dt.timezone.utc).astimezone(_GMT7)
    period = now_gmt7.strftime("%d.%m.%Y")
    shift = current_shift(now_gmt7)
    board_colour = board_colour_for(color)

    all_values = worksheet.get_all_values()
    for row_index in range(len(all_values), 1, -1):  # 1-based rows, skip header
        row = all_values[row_index - 1]
        row_period, row_shift, _from, _operator, row_board, row_colour, row_qty = (row + [""] * 7)[:7]
        if row_period != period:
            break
        if row_shift == shift and row_board == board and row_colour == board_colour:
            try:
                new_qty = int(row_qty) + 1
            except ValueError:
                new_qty = 1
            worksheet.update_cell(row_index, 7, str(new_qty))  # column G = Qty
            return

    worksheet.append_row(
        [period, shift, "from Test", "", board, board_colour, "1", "", "", "", "Check", ""],
        value_input_option="USER_ENTERED",
    )
