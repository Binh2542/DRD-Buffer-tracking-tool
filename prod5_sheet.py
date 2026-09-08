import datetime as dt
import threading
import time

from gsheets_cache import get_spreadsheet

_FROM_TAB_NAME = "FROM"
_FACT_OK_TAB_NAME = "Fact-D&R_ok"
_FACT_DR_TAB_NAME = "Fact-D&R"
_GMT7 = dt.timezone(dt.timedelta(hours=7))


def _write_with_retry(write_fn):
    """Run `write_fn` (a zero-arg callable doing exactly one gspread write -
    append_row/update_cell/update), retrying on failure like
    webdb_client._lookup_board does for WebDB (1s, then 2s between attempts,
    3 attempts total). gspread's default client never retries a transient
    error (a rate limit, a network blip) on its own, and a burst of many
    back-to-back scans (e.g. repairing a whole pallet then scanning it all
    at once) is exactly the kind of burst that can trip one - without this,
    that single write is silently gone forever, indistinguishable from a
    normal success to anything calling this. Only wraps the write itself,
    not the read-then-decide step before it, so a retry doesn't also re-pay
    for another full-sheet read. Re-raises the last exception if every
    attempt fails, so the caller (see app_gui.py's _write_prod5_row) can
    finally notice and log/surface it instead of it vanishing into a dead
    background thread.
    """
    last_exc = None
    for attempt, pause in enumerate((0, 1, 2)):
        if pause:
            time.sleep(pause)
        try:
            return write_fn()
        except Exception as exc:  # noqa: BLE001 - retried below, re-raised if truly exhausted
            last_exc = exc
    raise last_exc

# Boards with no matching dropdown option in this spreadsheet's Board
# column (see _record_row's ONE_OF_RANGE validation) - writing them would
# just recreate the "unvalidated free text in a dropdown cell" bug fixed
# above. These boards still work normally everywhere else in the app
# (Buffer/Completed/Online Repair/Refresh) - they're just never logged here.
_PROD5_EXCLUDED_BOARD_MARKERS = ("MCO.001.OVB.001v8",)


def should_skip_prod5_sheet(raw_board_name) -> bool:
    """True if `raw_board_name` (the untranslated board_name WebDB itself
    returns, not the resolved display option) has no home in this
    spreadsheet and record_from_scan/record_completed/record_line_activity
    should be skipped entirely for it."""
    if not raw_board_name:
        return False
    return any(marker in raw_board_name for marker in _PROD5_EXCLUDED_BOARD_MARKERS)

# app_gui.py fires one background thread per device when several devices
# scan/complete in the same batch (e.g. a Refresh that completes 5 at once) -
# _record_row does a read-then-decide-then-write against the same
# worksheet, so without this lock, concurrent calls can each read "no
# matching row yet" and both append a duplicate line, or race on the same
# target row and clobber each other's write.
_lock = threading.Lock()


def init_from_sheet(key_path, sheet_id):
    """Connect to the "FROM" tab of the "Buffer Debug PROD5 VTP" spreadsheet."""
    return get_spreadsheet(key_path, sheet_id).worksheet(_FROM_TAB_NAME)


def init_fact_ok_sheet(key_path, sheet_id):
    """Connect to the "Fact-D&R_ok" tab of the "Buffer Debug PROD5 VTP" spreadsheet."""
    return get_spreadsheet(key_path, sheet_id).worksheet(_FACT_OK_TAB_NAME)


def init_fact_dr_sheet(key_path, sheet_id):
    """Connect to the "Fact-D&R" tab of the "Buffer Debug PROD5 VTP"
    spreadsheet. Unlike the other tabs here, this one already has 1500+
    rows of ongoing data from an unrelated factory workflow (Line/Assembly
    output tracking) - so it's never auto-created, only ever connected to."""
    return get_spreadsheet(key_path, sheet_id).worksheet(_FACT_DR_TAB_NAME)


def shift_and_period(now_gmt7: dt.datetime) -> tuple[str, str]:
    """Day shift is 08:00-20:00 GMT+7. Night shift spans midnight - 20:00
    through 08:00 the *next* calendar day - but both halves belong to the
    Period the night shift started on, so e.g. a device scanned at 03:00
    still logs under yesterday's date as the tail end of last night's
    Night shift, not a new row for today. Public since app_gui.py also
    needs this to decide whether a Buffer/Online Repair item being
    deleted is still from "today"'s day+shift before undoing its Qty
    (see the undo_* functions below)."""
    if 8 <= now_gmt7.hour < 20:
        return "Day", now_gmt7.strftime("%d.%m.%Y")
    period_date = now_gmt7.date() if now_gmt7.hour >= 20 else now_gmt7.date() - dt.timedelta(days=1)
    return "Night", period_date.strftime("%d.%m.%Y")


def _row_range(sheet_id: int, row: int, end_column_index: int = 7) -> dict:
    """A-G by default (Period/Shift/Process/Operator/Board/Colour/Qty) - the
    columns _record_row ever writes. Never extends into H+ so this can't
    collide with the protected columns further right on Fact-D&R. Callers
    with a different column layout (e.g. the Assembly Rework FROM sheet,
    which also validates column K) pass a wider end_column_index."""
    return {
        "sheetId": sheet_id, "startRowIndex": row - 1, "endRowIndex": row,
        "startColumnIndex": 0, "endColumnIndex": end_column_index,
    }


def _copy_validation_and_format(worksheet, source_row: int, dest_row: int, end_column_index: int = 7) -> None:
    """append_row() only writes cell values - it doesn't extend the
    sheet's dropdown data validation or cell formatting to the new row the
    way manually inserting a row in the Sheets UI does. Past whatever row
    the sheet owner last set validation up to, appended rows silently
    become plain unvalidated text cells, so the "choose an option" columns
    (Shift/Process/Board/Colour/Operator) stop rendering as dropdowns.
    Explicitly copying validation + format from the row directly above
    keeps every appended row consistent with its neighbours - and since
    each new row copies from the row before it (which was already fixed
    the same way), this self-heals going forward even once the sheet's
    original pre-set range has been outgrown. Best-effort/cosmetic only -
    never blocks the actual data write if it fails."""
    request = {
        "requests": [
            {
                "copyPaste": {
                    "source": _row_range(worksheet.id, source_row, end_column_index),
                    "destination": _row_range(worksheet.id, dest_row, end_column_index),
                    "pasteType": "PASTE_DATA_VALIDATION",
                }
            },
            {
                "copyPaste": {
                    "source": _row_range(worksheet.id, source_row, end_column_index),
                    "destination": _row_range(worksheet.id, dest_row, end_column_index),
                    "pasteType": "PASTE_FORMAT",
                }
            },
        ]
    }
    try:
        worksheet.spreadsheet.batch_update(request)
    except Exception:  # noqa: BLE001
        pass


def board_colour_for(color) -> str:
    if not color:
        return "No Colour"
    color = color.strip().lower()
    if color == "white":
        return "W QR"
    if color == "black":
        return "B QR"
    return "No Colour"


def _record_row(
    worksheet, board: str, color, operator: str, label_column_value: str, trailing_values: list,
) -> None:
    """Shared logic for the FROM, Fact-D&R_ok and Fact-D&R sheets: the first
    event of a given day+shift+type (Board+BoardColour+Operator) creates a
    new line with Qty 1. Any further matching event in that same day+shift
    bumps that same line's Qty instead of creating a duplicate line - rows
    are appended chronologically, so today's entries (if any) sit at the
    tail and scanning stops as soon as an older Period is reached.

    `trailing_values` fills whatever columns follow Qty on that particular
    sheet (they don't all have the same trailing column layout).
    """
    now_gmt7 = dt.datetime.now(dt.timezone.utc).astimezone(_GMT7)
    shift, period = shift_and_period(now_gmt7)
    board_colour = board_colour_for(color)

    with _lock:
        all_values = worksheet.get_all_values()
        for row_index in range(len(all_values), 1, -1):  # 1-based rows, skip header
            row = all_values[row_index - 1]
            row_period, row_shift, _label, row_operator, row_board, row_colour, row_qty = (
                row + [""] * 7
            )[:7]
            if row_period != period:
                break
            if (
                row_shift == shift and row_board == board and row_colour == board_colour
                and row_operator == operator
            ):
                try:
                    new_qty = int(row_qty) + 1
                except ValueError:
                    new_qty = 1
                _write_with_retry(lambda: worksheet.update_cell(row_index, 7, str(new_qty)))  # column G = Qty
                return

        _write_with_retry(lambda: worksheet.append_row(
            [period, shift, label_column_value, operator, board, board_colour, "1", *trailing_values],
            value_input_option="USER_ENTERED",
        ))
        new_row_index = len(all_values) + 1
        if new_row_index > 2:  # a real data row exists above to copy validation/format from
            _copy_validation_and_format(worksheet, source_row=new_row_index - 1, dest_row=new_row_index)


def record_from_scan(worksheet, board: str, color) -> None:
    """Record one "new QR added to buffer" event in the FROM sheet."""
    _record_row(
        worksheet, board, color, operator="", label_column_value="from Test",
        trailing_values=["", "", "", "Check", ""],
    )


def record_completed(worksheet, board: str, color) -> None:
    """Record one "device passed and moved to Completed" event in the
    Fact-D&R_ok sheet."""
    _record_row(
        worksheet, board, color, operator="", label_column_value="TEST",
        trailing_values=["", "", "", "", ""],
    )


def record_mrb(worksheet, board: str, color) -> None:
    """Record one "device sent to MRB (scrap)" event in the Fact-D&R_ok
    sheet - same shape as record_completed, but column C ("District to")
    is "MRB" instead of "TEST" (a valid option in that column's existing
    dropdown), since these units don't count as a normal completed pass."""
    _record_row(
        worksheet, board, color, operator="", label_column_value="MRB",
        trailing_values=["", "", "", "", ""],
    )


def record_line_activity(worksheet, board: str, color, operator: str) -> None:
    """Record one "new QR added to buffer" event in the Fact-D&R sheet -
    same buffer-income event as record_from_scan, but this sheet also
    separates lines by Operator (not just day+shift+type), since it's
    shared with an unrelated per-person Line/Assembly tracking workflow.

    Column K ("Check") on this sheet is protected (write-blocked for our
    service account) - trailing_values only goes through REF/SCRAP/Remarks
    (columns H-J) so the row write never touches K at all.
    """
    _record_row(
        worksheet, board, color, operator=operator, label_column_value="Line",
        trailing_values=["", "", ""],
    )


def _decrement_or_remove_row(
    worksheet, all_values, row_index: int, qty_column: int, *, use_delete_rows: bool, blank_column_count: int,
) -> None:
    """Shared tail logic once one of the undo_* functions below has found
    the matching row (by 1-based row_index): decrement its Qty, or remove
    the line entirely if that would take Qty to 0. Uses delete_rows() only
    where confirmed structurally safe (no protected columns share that
    row - Debug's FROM and Assembly Rework's FROM); Debug's Fact-D&R and
    Assembly Rework's Fact-Rework both have a protected column further
    right, so those blank the writable columns in place instead, leaving
    an empty-but-present line rather than risking an APIError from a
    delete_rows() call that touches a protected range.
    """
    row = all_values[row_index - 1]
    raw_qty = row[qty_column - 1] if len(row) >= qty_column else ""
    try:
        qty = int(raw_qty)
    except ValueError:
        qty = 1
    if qty > 1:
        _write_with_retry(lambda: worksheet.update_cell(row_index, qty_column, str(qty - 1)))
        return
    if use_delete_rows:
        _write_with_retry(lambda: worksheet.delete_rows(row_index))
        return
    end_col = chr(ord("A") + blank_column_count - 1)
    _write_with_retry(lambda: worksheet.update(
        range_name=f"A{row_index}:{end_col}{row_index}",
        values=[[""] * blank_column_count],
        value_input_option="USER_ENTERED",
    ))


def undo_from_scan(worksheet, board: str, color, expected_period: str, expected_shift: str) -> bool:
    """Reverse one record_from_scan event - called when an admin/owner
    deletes a Buffer item that was scanned in the current day+shift, so
    the FROM sheet's count doesn't silently drift out of sync with the
    app's own list. No-op (returns False) if no matching row is found,
    e.g. because the item is from an earlier day+shift and was never
    passed in here to begin with, or the row was already cleaned up."""
    board_colour = board_colour_for(color)
    with _lock:
        all_values = worksheet.get_all_values()
        for row_index in range(len(all_values), 1, -1):  # 1-based rows, skip header
            row = all_values[row_index - 1]
            row_period, row_shift, row_label, row_operator, row_board, row_colour, _qty = (
                row + [""] * 7
            )[:7]
            if row_period != expected_period:
                break
            if (
                row_shift == expected_shift and row_label == "from Test" and row_operator == ""
                and row_board == board and row_colour == board_colour
            ):
                _decrement_or_remove_row(worksheet, all_values, row_index, 7, use_delete_rows=True, blank_column_count=7)
                return True
        return False


def undo_line_activity(
    worksheet, board: str, color, operator: str, expected_period: str, expected_shift: str,
) -> bool:
    """Reverse one record_line_activity event (Debug's Fact-D&R sheet) -
    same purpose as undo_from_scan, matched on Operator too since this
    sheet separates lines per person. Column K is protected here, so a
    Qty-1 row is blanked in place (A-J) rather than deleted."""
    board_colour = board_colour_for(color)
    with _lock:
        all_values = worksheet.get_all_values()
        for row_index in range(len(all_values), 1, -1):  # 1-based rows, skip header
            row = all_values[row_index - 1]
            row_period, row_shift, row_label, row_operator, row_board, row_colour, _qty = (
                row + [""] * 7
            )[:7]
            if row_period != expected_period:
                break
            if (
                row_shift == expected_shift and row_label == "Line" and row_operator == operator
                and row_board == board and row_colour == board_colour
            ):
                _decrement_or_remove_row(worksheet, all_values, row_index, 7, use_delete_rows=False, blank_column_count=10)
                return True
        return False


# ---- Assembly Rework's own PROD5-style spreadsheet ("Buffer Assembly
# REWORK PROD5 VTP") - a different, larger workbook with its own column
# layout (Board/BoardColour don't line up 1:1 with Debug's sheet: Board
# text already includes colour/region so BoardColour is always left blank
# here, and the trailing columns are Subtype/Number of tasks/Remarks/Etap
# instead of Debug's). Kept separate from _record_row above rather than
# forcing it into that shape.
_AR_FROM_TAB_NAME = "FROM"


def init_ar_prod5_from_sheet(key_path, sheet_id):
    """Connect to the "FROM" tab of the "Buffer Assembly REWORK PROD5 VTP"
    spreadsheet. Like Debug's Fact-D&R, this already has hundreds of rows
    of ongoing data - never auto-created, only ever connected to."""
    return get_spreadsheet(key_path, sheet_id).worksheet(_AR_FROM_TAB_NAME)


def record_ar_buffer_scan(worksheet, from_label: str, board_text: str) -> None:
    """Record one "device failed LongTest/TestRoom/QC" event in Assembly
    Rework's FROM sheet - board_text is the fully resolved device display
    name (see device_choice.py), from_label is "From Long Test" or
    "From QC" (column C's dropdown only allows those plus "From Assembly").

    Same day+shift+type merge behaviour as _record_row (bump Qty instead
    of duplicating a line), except the merge key here is
    (shift, from_label, board_text) - from_label must be part of it since,
    unlike Debug's FROM sheet where the label is always the same static
    string, it genuinely varies per event here and two different-cause
    events on the same board/day/shift must not collapse into one row
    that then shows the wrong cause.
    """
    now_gmt7 = dt.datetime.now(dt.timezone.utc).astimezone(_GMT7)
    shift, period = shift_and_period(now_gmt7)

    with _lock:
        all_values = worksheet.get_all_values()
        for row_index in range(len(all_values), 1, -1):  # 1-based rows, skip header
            row = all_values[row_index - 1]
            row_period, row_shift, row_from, _op, row_board, _colour, row_qty = (row + [""] * 7)[:7]
            if row_period != period:
                break
            if row_shift == shift and row_from == from_label and row_board == board_text:
                try:
                    new_qty = int(row_qty) + 1
                except ValueError:
                    new_qty = 1
                _write_with_retry(lambda: worksheet.update_cell(row_index, 7, str(new_qty)))  # column G = Qty
                return

        _write_with_retry(lambda: worksheet.append_row(
            [period, shift, from_label, "", board_text, "", "1", "", "", "", ""],
            value_input_option="USER_ENTERED",
        ))
        new_row_index = len(all_values) + 1
        if new_row_index > 2:  # a real data row exists above to copy validation/format from
            # A-E and K all carry dropdown validation on this sheet (unlike
            # Debug's FROM, where only A-G matter) - see _row_range's docstring.
            _copy_validation_and_format(
                worksheet, source_row=new_row_index - 1, dest_row=new_row_index, end_column_index=11
            )


def undo_ar_buffer_scan(
    worksheet, from_label: str, board_text: str, expected_period: str, expected_shift: str,
) -> bool:
    """Reverse one record_ar_buffer_scan event - only header row is
    protected on this sheet, so a Qty-1 row is removed via delete_rows()
    rather than blanked in place."""
    with _lock:
        all_values = worksheet.get_all_values()
        for row_index in range(len(all_values), 1, -1):  # 1-based rows, skip header
            row = all_values[row_index - 1]
            row_period, row_shift, row_from, _op, row_board, _colour, _qty = (row + [""] * 7)[:7]
            if row_period != expected_period:
                break
            if row_shift == expected_shift and row_from == from_label and row_board == board_text:
                _decrement_or_remove_row(worksheet, all_values, row_index, 7, use_delete_rows=True, blank_column_count=7)
                return True
        return False


_AR_FACT_REWORK_TAB_NAME = "Fact-Rework"


def init_ar_fact_rework_sheet(key_path, sheet_id):
    """Connect to the "Fact-Rework" tab of the "Buffer Assembly REWORK
    PROD5 VTP" spreadsheet. Never auto-created, only ever connected to."""
    return get_spreadsheet(key_path, sheet_id).worksheet(_AR_FACT_REWORK_TAB_NAME)


def _last_real_row(all_values) -> int:
    """Fact-Rework has thousands of rows pre-formatted far past the real
    data (a formula in a trailing column keeps them technically
    "non-empty" even though columns A-H are blank), so get_all_values()'s
    length can't be trusted as "where the real data ends" the way every
    other sheet in this module can - naively appending past them would
    silently bury the new row thousands of rows below where anyone would
    ever look for it. This walks back from the end to find the last row
    that actually has a Period (column A) filled in. Returns 1 (the
    header row) if the sheet is otherwise empty."""
    for i in range(len(all_values), 0, -1):
        if all_values[i - 1] and all_values[i - 1][0].strip():
            return i
    return 1


def record_ar_fact_rework_event(
    worksheet, district_to: str, process: str, operator: str, board_text: str,
) -> None:
    """Record one event in Assembly Rework's Fact-Rework sheet - either a
    Refresh moving a device Buffer -> Completed (process="Repair from
    buffer") or an Online Repair scan (process="Online repair"); both use
    this same sheet/column layout, differing only in that Process value.
    `district_to` is "QC" or "LONG" (which of LongTest/TestRoom/QC the
    device failed - originally for Refresh, or its current last-steps
    state for Online Repair). `operator` is the *app user who triggered
    the action* (resolved via the User sheet), not anyone named in WebDB's
    own step history - same convention as Debug's record_line_activity.

    Unlike every other sheet this module writes to, real data here doesn't
    sit at the tail (see _last_real_row) - append_row() would badly
    misplace the new row, so this finds the real last row itself and
    writes there via an explicit range update. Those pre-formatted rows
    already carry the sheet's data validation this far ahead of the real
    data, so (unlike FROM) there's no need to copy validation across too.
    """
    now_gmt7 = dt.datetime.now(dt.timezone.utc).astimezone(_GMT7)
    shift, period = shift_and_period(now_gmt7)

    with _lock:
        all_values = worksheet.get_all_values()
        last_real_row = _last_real_row(all_values)

        for row_index in range(last_real_row, 1, -1):  # 1-based rows, skip header
            row = all_values[row_index - 1]
            row_period, row_shift, row_district, row_process, row_operator, row_board = (
                row + [""] * 6
            )[:6]
            row_qty = row[7] if len(row) > 7 else ""
            if row_period != period:
                break
            if (
                row_shift == shift and row_district == district_to and row_process == process
                and row_operator == operator and row_board == board_text
            ):
                try:
                    new_qty = int(row_qty) + 1
                except ValueError:
                    new_qty = 1
                _write_with_retry(lambda: worksheet.update_cell(row_index, 8, str(new_qty)))  # column H = Qty
                return

        new_row = last_real_row + 1
        _write_with_retry(lambda: worksheet.update(
            range_name=f"A{new_row}:H{new_row}",
            values=[[period, shift, district_to, process, operator, board_text, "", "1"]],
            value_input_option="USER_ENTERED",
        ))


def undo_ar_online_repair(
    worksheet, district_to: str, operator: str, board_text: str, expected_period: str, expected_shift: str,
) -> bool:
    """Reverse one record_ar_fact_rework_event(process="Online repair")
    event. Columns L-M are protected here, so a Qty-1 row is blanked in
    place (A-H) rather than deleted, and - like record_ar_fact_rework_event
    itself - real data isn't guaranteed to sit at the tail, so this scans
    back from _last_real_row rather than len(all_values)."""
    with _lock:
        all_values = worksheet.get_all_values()
        last_real_row = _last_real_row(all_values)
        for row_index in range(last_real_row, 1, -1):  # 1-based rows, skip header
            row = all_values[row_index - 1]
            row_period, row_shift, row_district, row_process, row_operator, row_board = (
                row + [""] * 6
            )[:6]
            if row_period != expected_period:
                break
            if (
                row_shift == expected_shift and row_district == district_to and row_process == "Online repair"
                and row_operator == operator and row_board == board_text
            ):
                _decrement_or_remove_row(worksheet, all_values, row_index, 8, use_delete_rows=False, blank_column_count=8)
                return True
        return False
