import datetime as dt
import json
import os
import queue
import re
import sys
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import messagebox, ttk

import matplotlib.dates as mdates
from dotenv import load_dotenv
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator

from buffer_stats import (
    compute_daily_buffer_counts,
    compute_daily_online_repair_counts,
)
from firestore_client import (
    AR_COMPLETED_COLLECTION,
    AR_DEVICES_COLLECTION,
    AR_MRB_COLLECTION,
    AR_ONLINE_REPAIR_COLLECTION,
    COMPLETED_COLLECTION,
    DEVICES_COLLECTION,
    IO_QUEUE_COLLECTION,
    MRB_COLLECTION,
    ONLINE_REPAIR_COLLECTION,
    QR_CHANGES_COLLECTION,
    add_completed_device,
    add_device,
    add_mrb_device,
    add_online_repair,
    add_qr_change,
    delete_app_account,
    delete_completed_devices,
    delete_devices,
    delete_io_queue_entries,
    delete_mrb_devices,
    delete_online_repairs,
    delete_qr_changes,
    delete_queue_account,
    fetch_completed_since,
    fetch_online_repairs_since,
    get_app_account,
    log_lookup_failure,
    log_sheet_write_failure,
    hash_password,
    init_firestore,
    list_devices,
    listen_app_accounts,
    listen_devices,
    listen_io_queue,
    listen_online_repairs,
    listen_queue_accounts,
    remove_io_queue_entries,
    set_account_mode,
    set_account_role,
    set_app_account,
    set_queue_account,
    set_queue_account_department,
    update_qr_change_current,
)
from device_choice import load_device_choice_rules, resolve_device_choice
from pcb_choice import load_pcb_choice_rules, resolve_board_name
from prod5_sheet import (
    init_ar_fact_rework_sheet,
    init_ar_prod5_from_sheet,
    init_fact_dr_sheet,
    init_fact_ok_sheet,
    init_from_sheet,
    record_ar_buffer_scan,
    record_ar_fact_rework_event,
    record_completed,
    record_from_scan,
    record_line_activity,
    record_mrb,
    shift_and_period,
    should_skip_prod5_sheet,
    undo_ar_buffer_scan,
    undo_ar_online_repair,
    undo_from_scan,
    undo_line_activity,
)
from auto_update import apply_update_and_restart, check_for_update, download_asset
from serial_scanner import SerialScanner, list_serial_ports
from sheets_export import (
    export_completed_devices,
    export_devices,
    export_mrb_devices,
    export_online_repairs,
    export_qr_changes,
    init_ar_completed_sheet,
    init_ar_mrb_sheet,
    init_ar_online_repair_sheet,
    init_ar_sheet,
    init_completed_sheet,
    init_mrb_sheet,
    init_online_repair_sheet,
    init_qr_change_sheet,
    init_sheet,
    load_operator_names,
    read_completed_from_sheet,
    read_devices_from_sheet,
    read_mrb_from_sheet,
    read_online_repairs_from_sheet,
    read_qr_changes_from_sheet,
    sync_completed_removed,
    sync_completed_upserted,
    sync_device_removed,
    sync_device_upserted,
    sync_mrb_removed,
    sync_mrb_upserted,
    sync_online_repair_added,
    sync_online_repair_removed,
    sync_qr_change_current_updated,
    sync_qr_change_removed,
    sync_qr_change_upserted,
)
from webdb_client import (
    attempt_login,
    build_api_session,
    build_driver,
    check_assembly_rework_buffer,
    check_assembly_rework_repaired,
    check_device_prog_main,
    check_device_prog_main_session,
    check_pcb_qr_current,
    elapsed_since,
    fetch_pcb_qr_info,
    format_utc_to_gmt7,
)

def _frozen_base_dir() -> Path:
    """Where firebase_key.json/.env/chrome_profile live when packaged -
    next to the actual executable, not inside PyInstaller's temp
    extraction dir (that's what _resource_path/sys._MEIPASS is for).

    On Windows/Linux, sys.executable already points straight at that
    file, e.g. dist/DRD Accounting Tool.exe - .parent is exactly right.
    On macOS, sys.executable is buried inside the .app bundle's own
    internals (Foo.app/Contents/MacOS/Foo), three levels below the
    folder the user actually places .env/firebase_key.json in
    (alongside "Foo.app" itself, same convention as every other
    platform) - walking up past Contents/MacOS/Foo.app corrects for
    that. Without this, the app can't find its Firebase credentials at
    all and crashes on startup with no visible error (a windowed/
    non-console build has nowhere to print the traceback).
    """
    exe_path = Path(sys.executable).resolve()
    if sys.platform == "darwin" and exe_path.match("*.app/Contents/MacOS/*"):
        return exe_path.parents[2].parent
    return exe_path.parent


# Files the user provides/that persist (firebase_key.json, .env, the
# Chrome profile) live next to the actual executable/app, not inside
# PyInstaller's temp extraction dir - so this must use sys.executable's
# folder when frozen, not __file__ (which resolves inside that temp dir).
BASE_DIR = _frozen_base_dir() if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _resource_path(relative: str) -> Path:
    """Resolve a bundled asset path - works both running from source and
    when packaged by PyInstaller, which extracts data files under
    sys._MEIPASS instead of next to the script."""
    base = Path(getattr(sys, "_MEIPASS", BASE_DIR))
    return base / relative


ICON_PNG = _resource_path("assets/icon.png")
ICON_ICO = _resource_path("assets/icon.ico")
FIREBASE_KEY_FILE = BASE_DIR / "firebase_key.json"
PROFILE_DIR = BASE_DIR / "chrome_profile"
# Remembers the last serial QR scanner port selected (e.g. "/dev/ttyACM0"
# on Linux) across restarts - same lightweight flat-file-cache approach
# webdb_client.py already uses for the chromedriver path, not worth a full
# settings/JSON framework for one string.
SERIAL_PORT_CACHE_FILE = BASE_DIR / ".serial_port"
# Last known-good copies of the PCB choice/Device choice/User sheets,
# saved locally on each machine every time they load successfully from
# Google Sheets - used as a fallback at login if the live load fails (see
# _do_login), so a Google Sheets outage/rate limit degrades to "using
# yesterday's mapping" instead of either silently writing wrong names
# (the original bug) or locking every operator out of the app entirely
# for however long the outage lasts.
PCB_CHOICE_CACHE_FILE = BASE_DIR / ".pcb_choice_cache.json"
DEVICE_CHOICE_CACHE_FILE = BASE_DIR / ".device_choice_cache.json"
OPERATOR_NAMES_CACHE_FILE = BASE_DIR / ".operator_names_cache.json"


def _load_json_cache(path: Path):
    """Returns the cached value, or None if there's no cache yet (first
    run on this machine) or it's unreadable/corrupt - either way, the
    caller treats None as "no fallback available"."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_json_cache(path: Path, value) -> None:
    """Best-effort - a failure to write the cache shouldn't affect the
    login that just succeeded, it only means this specific fallback won't
    be available next time if the next login's live load fails."""
    try:
        path.write_text(json.dumps(value), encoding="utf-8")
    except OSError:
        pass


def _pcb_choice_rules_to_jsonable(rules):
    """pcb_choice_rules is a list of (search_text, regions, option) tuples
    where `regions` is a set or None - neither tuples nor sets are JSON-
    serializable, so convert both to plain lists. resolve_board_name only
    ever does `region in regions`, which works identically on a list, so
    nothing needs converting back on the read side."""
    return [[search_text, list(regions) if regions is not None else None, option]
            for search_text, regions, option in rules]
# Shown in the window title bar (see WebdbApp.__init__) - bump this by
# hand whenever a build is cut, since PyInstaller doesn't derive one on
# its own. Cross-platform: Tkinter's title bar is the same call on every
# OS this ships on.
APP_VERSION = "V1.4.0"
# Checked at every startup (see WebdbApp.__init__/_check_for_update_async) -
# a public repo so this needs no embedded token (see auto_update.py's
# docstring for the release/asset naming convention this expects).
UPDATE_REPO = "Binh2542/DRD-Buffer-tracking-tool"
BASE_URL = "https://main.prod.m11g.ajax.systems/webaut/webdb/"
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
PROD5_SHEET_ID = "1otPfRvWa2zGREGLi_5SzsOi8DyPZyeo24cVwS5bNTos"  # "Buffer Debug PROD5 VTP"
AR_PROD5_SHEET_ID = "1wr7UhVUlMl7_QfXik3nxDw4dMY56kKV1otf3BhU62rk"  # "Buffer Assembly REWORK PROD5 VTP"

# The app always connects to the real WebDB site with this one shared
# account, regardless of which app-level account the operator logs in
# with - app accounts and roles are managed separately, in Settings.
WEBDB_USERNAME = "BinhNguyen"
WEBDB_PASSWORD = "Binh@254"

# The owner account is hardcoded (not stored in Firestore like other app
# accounts) so there's always at least one account that can manage the rest.
OWNER_USERNAME = "BinhNguyen"
OWNER_PASSWORD = "Binh@254"

# ---------------- dark / green theme ----------------
BG = "#14171c"
PANEL_BG = "#1b1f27"
FIELD_BG = "#232833"
BORDER = "#2f3541"
TEXT = "#e6e9ee"
MUTED = "#8b93a1"
ACCENT = "#21c98f"
ACCENT_DARK = "#189c70"
ACCENT_TEXT = "#0b1410"
SELECT_BG = "#1d4f3d"
DANGER = "#e5626a"
CHART_SECONDARY = "#4a9eff"

GMT7 = dt.timezone(dt.timedelta(hours=7))


def _parse_iso_to_gmt7(iso_str):
    """Parse an ISO timestamp (naive treated as UTC) and return it converted
    to GMT+7 local time, or None if `iso_str` is missing/unparseable."""
    if not iso_str:
        return None
    try:
        parsed = dt.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(GMT7)


def _matches_date_range_and_shift(iso_str, start_date, end_date, shift_filter):
    """True if the GMT+7 local timestamp encoded in `iso_str` falls within
    [start_date, end_date] (inclusive) and, if shift_filter is "Day" or
    "Night", within that 8AM-8PM / 8PM-8AM window - "24h" matches any hour."""
    local = _parse_iso_to_gmt7(iso_str)
    if local is None:
        return False
    if not (start_date <= local.date() <= end_date):
        return False
    if shift_filter == "24h":
        return True
    is_day = 8 <= local.hour < 20
    return is_day if shift_filter == "Day" else not is_day

FONT_BASE = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_HEADER = ("Segoe UI", 15, "bold")

# Online Repair is used every day (unlike Completed/MRB/QR Change), so it
# keeps a live listener - but bounded to a rolling window instead of the
# collection's full history, so the per-write fan-out cost this app pays
# to every connected listener stays flat no matter how much history piles
# up. 3 days keeps that flat cost inside Firebase's free quota at this
# project's real scan volume (see the Chart view for anything older).
ONLINE_REPAIR_LIVE_WINDOW_DAYS = 3

# How often the online-repair listeners are torn down and re-subscribed
# with a freshly-computed cutoff (see _refresh_online_repair_window). The
# cutoff passed to listen_online_repairs is only evaluated once, at
# subscribe time - without this periodic refresh, a session left open for
# many days straight would keep growing past the intended window instead
# of it actually rolling forward.
_ONLINE_REPAIR_WINDOW_REFRESH_MS = 6 * 60 * 60 * 1000

# (key, header, width, anchor, stretch, sort_key)
BUFFER_COLUMNS = [
    ("check", "", 42, "center", False, None),
    ("id", "ID", 130, "w", False, None),
    ("board", "Board", 220, "w", False, "board"),
    ("import_time", "Import Time", 190, "center", False, "import_time"),
    ("elapsed", "Elapsed", 110, "center", False, "elapsed"),
    ("attempt", "Attempt", 80, "center", False, "attempt"),
    ("defect", "Defect", 450, "w", True, "defect"),
]
COMPLETED_COLUMNS = [
    ("check", "", 42, "center", False, None),
    ("id", "ID", 115, "w", False, None),
    ("board", "Board", 180, "w", False, "board"),
    ("import_time", "Import Time", 186, "center", False, "import_time"),
    ("attempt", "Attempt", 75, "center", False, "attempt"),
    ("complete_time", "Complete Time", 186, "center", False, "complete_time"),
    ("debug", "Debug", 260, "w", False, "debug"),
    ("defect", "Defect", 320, "w", True, "defect"),
]
ONLINE_REPAIR_COLUMNS = [
    ("check", "", 42, "center", False, None),
    ("id", "ID", 150, "w", False, None),
    ("board", "Board", 260, "w", False, "board"),
    ("repair_time", "Repair Time", 200, "center", False, "repair_time"),
    ("debug", "Debug", 220, "w", False, "debug"),
    ("defect", "Defect", 320, "w", True, "defect"),
]
MRB_COLUMNS = [
    ("check", "", 42, "center", False, None),
    ("id", "ID", 150, "w", False, None),
    ("board", "Board", 260, "w", False, "board"),
    ("mrb_time", "MRB Time", 200, "center", False, "mrb_time"),
    ("debug", "Debug", 220, "w", False, "debug"),
    ("defect", "Defect", 320, "w", True, "defect"),
]
# Shared across Debug/Assembly Rework (see firestore_client.QR_CHANGES_COLLECTION) -
# no _AR_HEADER_OVERRIDES entries needed since none of these keys
# (pcb_qr/previous_qr/current_qr/model/scan_time) are mode-specific vocabulary.
QR_CHANGE_COLUMNS = [
    ("check", "", 42, "center", False, None),
    ("pcb_qr", "PCB QR", 200, "w", False, "pcb_qr"),
    ("previous_qr", "Previous QR", 180, "w", False, "previous_qr"),
    ("current_qr", "Current QR", 180, "w", False, "current_qr"),
    ("model", "Model", 220, "w", True, "model"),
    ("scan_time", "Time", 190, "center", False, "scan_time"),
]
QUEUE_COLUMNS = [
    ("check", "", 42, "center", False, None),
    ("qr", "QR", 260, "w", False, None),
    ("department", "Department", 160, "w", False, "department"),
    ("scanned_by", "Scanned By", 150, "w", False, "scanned_by"),
    ("scanned_time", "Scanned Time", 190, "center", False, "scanned_time"),
]

# Which department(s) the public scan pages (public/*/index.html) feed
# into this app's own Queue view, per app mode - Hard Test's queue is a
# Debug-mode concern, the other four feed Assembly Rework. Keys match the
# DEPARTMENT constant each scan page sets and firestore.rules' allow-list;
# labels match the Cloud Function's own DEPARTMENT_TABS (functions/index.js).
_DEBUG_QUEUE_DEPARTMENTS = ["hard-test"]
_AR_QUEUE_DEPARTMENTS = ["assembly", "long-test", "test-room", "qc"]
_QUEUE_DEPARTMENT_LABELS = {
    "hard-test": "HARD TEST",
    "assembly": "ASSEMBLY",
    "long-test": "LONG TEST",
    "test-room": "TEST ROOM",
    "qc": "QC",
}

# Assembly Rework uses different vocabulary for the same underlying data -
# "board" is really a whole device (see device_choice.py), the person
# named is an Assembler, not a Debug technician, and "defect" is really
# which line (QC/Long Test) the unit came from (see _ask_ar_from_district),
# not a Prog Main failure reason. Only header *text* changes; the dict
# keys/sort_keys stay "board"/"debug"/"defect" everywhere since that's
# what every record (Firestore, sheets) actually keys on.
_AR_HEADER_OVERRIDES = {"board": "Device", "debug": "Assembler", "defect": "From"}

# Restricted from the "user" role (see _can_view_reports) - each is an
# on-demand fetch, not just a UI view, so cutting 80% of accounts off from
# them directly reduces Firestore reads too.
_REPORT_VIEW_MODES = ("completed", "chart", "summary")

# Every real QR/PCB QR in this system is letters, digits, and "-" only.
# Vietnamese input editors (Unikey etc.), if left switched on, can
# silently transform a barcode scanner's fast "keystrokes" into accented
# Vietnamese text instead (e.g. Telex turning "aa" into "a"+diacritic) -
# rejecting anything outside this charset up front means that garbled
# scan never reaches WebDB at all, instead of churning through a slow
# "not found" lookup for a QR that could never match.
_VALID_QR_RE = re.compile(r"^[A-Za-z0-9-]+$")


def _setup_style(root: tk.Tk) -> None:
    root.configure(bg=BG)
    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure(".", background=BG, foreground=TEXT, font=FONT_BASE)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TEXT, font=FONT_BASE)
    style.configure("Header.TLabel", background=BG, foreground=TEXT, font=FONT_HEADER)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=FONT_BASE)
    style.configure("Accent.TLabel", background=BG, foreground=ACCENT, font=FONT_BOLD)
    style.configure("Error.TLabel", background=BG, foreground=DANGER, font=FONT_BASE)
    # Solid-fill "badge" for the Queue counter (see _update_queue_counter_label) -
    # needs to read at a glance from across the room, not blend into the
    # header like a normal label.
    style.configure(
        "QueueBadge.TLabel", background=ACCENT, foreground=ACCENT_TEXT,
        font=("Segoe UI", 13, "bold"), padding=(14, 6),
    )

    style.configure(
        "TEntry",
        fieldbackground=FIELD_BG,
        foreground=TEXT,
        insertcolor=TEXT,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        borderwidth=1,
        padding=6,
    )
    style.map(
        "TEntry",
        bordercolor=[("focus", ACCENT)],
        lightcolor=[("focus", ACCENT)],
        darkcolor=[("focus", ACCENT)],
    )

    style.configure(
        "Accent.TButton",
        background=ACCENT,
        foreground=ACCENT_TEXT,
        font=FONT_BOLD,
        padding=(14, 8),
        borderwidth=0,
        focuscolor=ACCENT,
    )
    style.map(
        "Accent.TButton",
        background=[("active", ACCENT_DARK), ("disabled", "#2a303a")],
        foreground=[("disabled", MUTED)],
    )

    style.configure(
        "Secondary.TButton",
        background=PANEL_BG,
        foreground=TEXT,
        font=FONT_BASE,
        padding=(12, 7),
        borderwidth=1,
        bordercolor=BORDER,
        focuscolor=PANEL_BG,
    )
    style.map(
        "Secondary.TButton",
        background=[("active", FIELD_BG), ("disabled", PANEL_BG)],
        foreground=[("disabled", MUTED)],
        bordercolor=[("active", ACCENT)],
    )

    style.configure(
        "Danger.TButton",
        background=PANEL_BG,
        foreground=DANGER,
        font=FONT_BASE,
        padding=(12, 7),
        borderwidth=1,
        bordercolor=BORDER,
        focuscolor=PANEL_BG,
    )
    style.map(
        "Danger.TButton",
        background=[("active", "#2a1c1f"), ("disabled", PANEL_BG)],
        bordercolor=[("active", DANGER)],
        foreground=[("disabled", MUTED)],
    )

    style.configure(
        "Treeview",
        background=FIELD_BG,
        fieldbackground=FIELD_BG,
        foreground=TEXT,
        rowheight=28,
        borderwidth=0,
        font=FONT_BASE,
    )
    style.configure(
        "Treeview.Heading",
        background=PANEL_BG,
        foreground=ACCENT,
        font=FONT_BOLD,
        relief="flat",
        borderwidth=0,
        padding=(8, 8),
    )
    style.map("Treeview.Heading", background=[("active", FIELD_BG)])
    style.map("Treeview", background=[("selected", SELECT_BG)], foreground=[("selected", TEXT)])

    style.configure(
        "Vertical.TScrollbar",
        background=PANEL_BG,
        troughcolor=BG,
        bordercolor=BORDER,
        arrowcolor=TEXT,
    )

    style.configure(
        "TProgressbar",
        background=ACCENT,
        troughcolor=FIELD_BG,
        bordercolor=BORDER,
        lightcolor=ACCENT,
        darkcolor=ACCENT,
    )


def _apply_window_icon(root: tk.Tk) -> None:
    """Cosmetic - never let an icon/asset problem block the app from
    starting, so failures here are always swallowed silently.

    On Windows this sets the icon two different ways rather than picking
    just one: iconbitmap (native .ico, what actually drives the taskbar
    button on Windows) and iconphoto (Tk's own PhotoImage-based path).
    They're normally redundant, but on a handful of machines iconbitmap
    alone has been observed to silently no-op, leaving the taskbar
    showing Tk's own default "feather" icon instead of ours - iconphoto
    uses a different code path in Tk's implementation and has been
    reliable as a fallback there. Each call is independently guarded so
    one failing doesn't skip the other.
    """
    if sys.platform == "win32" and ICON_ICO.exists():
        try:
            root.iconbitmap(str(ICON_ICO))
        except Exception:  # noqa: BLE001
            pass
    if ICON_PNG.exists():
        try:
            root.iconphoto(True, tk.PhotoImage(file=str(ICON_PNG)))
        except Exception:  # noqa: BLE001
            pass


def _apply_dark_titlebar(root: tk.Tk) -> None:
    """Windows 10 (20H1+)/11 only: make the OS-drawn title bar dark to match
    the app's theme, via the same DWM attribute Windows' own dark-mode apps
    use. Silently does nothing on macOS/Linux or older Windows builds."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        dwmapi = ctypes.windll.dwmapi
        value = ctypes.c_int(1)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE: new / pre-20H1 builds
            if dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:  # noqa: BLE001
        pass


def _force_repaint(root: tk.Tk) -> None:
    """Nudge Windows' compositor (DWM) into actually repainting the window.

    Reordering the icon/dark-titlebar setup, then a bare alpha toggle, were
    both tried and neither was enough on some machines to fix the
    black-screen-until-you-click-the-taskbar-icon issue. What manually
    clicking the taskbar icon actually does is minimize-then-restore the
    window, which forces DWM to build a brand new composited frame from
    the window's current content instead of reusing a stale one - so this
    reproduces that exact action programmatically (iconify/deiconify),
    with the cheaper alpha toggle kept first in case it's sufficient on
    its own on some machines. No-op on platforms without these attributes,
    or if the window isn't in a state that supports iconify (e.g. still
    withdrawn).

    Windows-only: this exists purely for a DWM-specific repaint bug and
    doesn't apply anywhere else - on Linux (GNOME/Mutter, at least),
    iconify() reliably minimizes the window to the taskbar but deiconify()
    doesn't reliably restore focus to it afterward, so this was actually
    causing the exact "window minimizes and needs a manual taskbar click"
    symptom on Ubuntu that clicking the taskbar icon is supposed to fix on
    Windows - the opposite of its intent there.
    """
    if sys.platform != "win32":
        return
    try:
        root.update_idletasks()
        root.attributes("-alpha", 0.999)
        root.after(30, lambda: root.attributes("-alpha", 1.0))
    except tk.TclError:
        pass

    def _restore():
        try:
            root.iconify()
            root.after(80, lambda: (root.deiconify(), root.lift(), root.focus_force(), _apply_window_icon(root)))
        except tk.TclError:
            pass

    root.after(100, _restore)


def _build_and_login_driver():
    """Start a brand new Chrome/Selenium session and log it into WebDB
    with the one shared service account. Raises RuntimeError with a
    user-facing message on any failure (never leaves a half-built driver
    behind - it's always either a working, logged-in driver, or quit).

    Shared by the initial login (_do_login) and _relogin_webdb_locked's
    escalation path - a long-running headless Chrome session can end up
    wedged (crashed renderer, detached debugger, etc.) in a way a plain
    re-login on the SAME driver can't recover from; building a fresh
    driver from scratch is what actually fixes it, same as a full
    logout+login already did before this existed.
    """
    try:
        driver = build_driver(PROFILE_DIR, headless=True)
    except Exception as exc:  # noqa: BLE001 - infra error, not a credentials problem
        raise RuntimeError(f"Failed to start browser: {exc}") from exc
    try:
        ok = attempt_login(driver, BASE_URL, WEBDB_USERNAME, WEBDB_PASSWORD)
    except Exception as exc:  # noqa: BLE001
        driver.quit()
        raise RuntimeError(f"Login error: {exc}") from exc
    if not ok:
        driver.quit()
        raise RuntimeError("Failed to connect to WebDB with the configured service account.")
    return driver


class WebdbApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"DRD Accounting Tool {APP_VERSION}")
        self.root.minsize(700, 500)
        try:
            self.root.state("zoomed")
        except tk.TclError:
            pass
        # Belt-and-suspenders: explicitly size to the screen too, since
        # state("zoomed") can silently no-op depending on DPI/display setup.
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self.root.geometry(f"{screen_w}x{screen_h}+0+0")
        # Icon/dark-titlebar are applied after the window's final size/state
        # is already set, not before - applying them first forces Tk to
        # realize (paint) the window at its tiny default size, and DWM can
        # then leave a stale black frame behind through the resize-to-zoomed
        # transition until something (e.g. clicking the taskbar icon) forces
        # a repaint.
        _apply_window_icon(root)
        _apply_dark_titlebar(root)
        _setup_style(root)
        _force_repaint(root)

        self.driver = None
        self.username = None
        self.role = None  # "owner" | "admin" | "user"
        self.db = None
        self.pcb_choice_rules: list = []
        self.from_sheet = None
        self.fact_ok_sheet = None
        self.fact_dr_sheet = None
        self.ar_prod5_from_sheet = None
        self.ar_fact_rework_sheet = None
        self.device_choice_rules: list = []
        self.operator_names: dict = {}
        self.accounts_watch = None
        self.accounts: list[dict] = []
        self._settings_reload_fn = None
        # Only subscribed while Settings' Queue Registration tab is open
        # (see _open_settings_window) - unlike self.accounts, nothing else
        # in the app needs this collection live for the whole session.
        self.queue_accounts_watch = None
        self.queue_accounts: list[dict] = []
        self._queue_settings_reload_fn = None

        # "debug" (PCB-level component debug) and "assembly_rework" (a
        # separate physical workflow) each get their own Google Sheet tabs,
        # Firestore collections, snapshot watches and in-memory lists, kept
        # entirely apart so scans in one never mix with the other. Both are
        # connected/listened to at login (see _do_login/_handle_result) so
        # switching modes is instant, no reconnect needed. `devices`/
        # `completed_devices`/`online_repairs`/`sheet`/`completed_sheet`/
        # `online_repair_sheet` below are properties resolving to whichever
        # mode is currently active - the rest of the UI code reads/writes
        # those exactly as before and doesn't need to know modes exist.
        self.app_mode = "debug"  # "debug" | "assembly_rework"
        self.mode_permission = "both"  # "debug" | "assembly_rework" | "both" - which mode(s) this account may use
        self.mode_buttons: dict[str, ttk.Button] = {}

        self.debug_sheet = None
        self.debug_completed_sheet = None
        self.debug_online_repair_sheet = None
        self.debug_mrb_sheet = None
        self.debug_devices_watch = None
        self.debug_online_repair_watch = None
        self.debug_io_queue_watch = None
        self.debug_devices: list[dict] = []
        self.debug_completed_devices: list[dict] = []
        self.debug_online_repairs: list[dict] = []
        self.debug_mrb_devices: list[dict] = []
        self.debug_io_queue: list[dict] = []

        self.ar_sheet = None
        self.ar_completed_sheet = None
        self.ar_online_repair_sheet = None
        self.ar_mrb_sheet = None
        self.ar_devices_watch = None
        self.ar_online_repair_watch = None
        self.ar_io_queue_watch = None
        self.ar_devices: list[dict] = []
        self.ar_completed_devices: list[dict] = []
        self.ar_online_repairs: list[dict] = []
        self.ar_mrb_devices: list[dict] = []
        self.ar_io_queue: list[dict] = []

        # Paper-QR-reassignment tracking - deliberately NOT split into
        # debug_*/ar_* like everything above (see
        # firestore_client.QR_CHANGES_COLLECTION's docstring): one shared
        # sheet/collection/list regardless of which app_mode is active.
        self.qr_change_sheet = None
        self.qr_changes: list[dict] = []

        self.view_mode = "online_repair"  # "buffer" | "completed" | "online_repair" | "mrb" | "qr_change" | "chart"
        self.view_buttons: dict[str, ttk.Button] = {}
        self._current_columns = ONLINE_REPAIR_COLUMNS
        self.chart_canvas = None
        self.chart_ax = None
        self.chart_range_days = 90
        self._chart_fetch_token = 0
        self._summary_fetch_token = 0
        # Last-fetched Completed/Online-Repair-history for Chart/Summary
        # (both on-demand - see the on-demand fetch section) - reused by
        # _on_snapshot_update so a live Buffer/Online-Repair write while
        # either tab is just sitting open re-renders from this cache
        # instead of re-fetching the whole history on every single write.
        self._chart_completed_cache: list = []
        self._chart_repairs_cache: list | None = None
        self._summary_completed_cache: list = []
        self._summary_repairs_cache: list | None = None
        self._summary_range_cache: tuple | None = None
        self.chart_range_buttons: dict[int, ttk.Button] = {}
        self._chart_dates: list = []
        self._chart_buffer_values: list = []
        self._chart_repair_values: list = []
        self._chart_annotation = None
        self._chart_vline = None
        self._chart_hover_last_idx = None
        self.result_queue: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.checked_qrs: set[str] = set()
        self.checked_completed_qrs: set[str] = set()
        self.checked_online_repair_ids: set[str] = set()
        self.checked_mrb_qrs: set[str] = set()
        self.checked_qr_change_ids: set[str] = set()
        self.checked_io_queue_ids: set[str] = set()
        # Independent of login/mode state on purpose - a serial QR scanner
        # (e.g. /dev/ttyACM0 on Linux, an alternative to a USB-HID
        # "keyboard wedge" scanner) stays connected across logout/login,
        # same as the physical hardware itself would stay plugged in.
        # Only _on_close (the app actually quitting) tears it down.
        self.serial_scanner: SerialScanner | None = None
        self.serial_port_combo = None
        self.serial_connect_button = None
        self.serial_status_label = None
        self._serial_autoconnect_attempted = False
        self.sort_column = None
        self.sort_reverse = False
        self._reset_sort_to_default()  # self.view_mode already defaults to "online_repair" above

        self.login_frame = None
        self.main_frame = None

        self._build_login_frame()
        self.root.after(150, self._poll_results)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(_ONLINE_REPAIR_WINDOW_REFRESH_MS, self._refresh_online_repair_window)

        # Only meaningful for a packaged build - sys.executable is the
        # Python interpreter when running from source, not this app, so
        # there'd be nothing sensible to self-replace. A failed/unreachable
        # check is silently treated as "no update" (see check_for_update),
        # never blocking normal use - this only ever gates on a CONFIRMED
        # newer release, not on being unable to tell.
        if getattr(sys, "frozen", False):
            threading.Thread(target=self._check_for_update_async, daemon=True).start()

    # ---- mode-resolving properties: everything else in the class reads/
    # writes devices/completed_devices/online_repairs/sheet/completed_sheet/
    # online_repair_sheet as if there were only ever one mode - these pick
    # the right underlying debug_*/ar_* attribute for whichever mode is
    # currently active. Firestore snapshot handlers deliberately bypass
    # these setters and write to self.debug_* / self.ar_* directly (a
    # snapshot for the inactive mode must still land in the right place
    # regardless of which mode is on screen right now).
    @property
    def devices(self):
        return self.ar_devices if self.app_mode == "assembly_rework" else self.debug_devices

    @devices.setter
    def devices(self, value):
        if self.app_mode == "assembly_rework":
            self.ar_devices = value
        else:
            self.debug_devices = value

    @property
    def completed_devices(self):
        return self.ar_completed_devices if self.app_mode == "assembly_rework" else self.debug_completed_devices

    @completed_devices.setter
    def completed_devices(self, value):
        if self.app_mode == "assembly_rework":
            self.ar_completed_devices = value
        else:
            self.debug_completed_devices = value

    @property
    def online_repairs(self):
        return self.ar_online_repairs if self.app_mode == "assembly_rework" else self.debug_online_repairs

    @online_repairs.setter
    def online_repairs(self, value):
        if self.app_mode == "assembly_rework":
            self.ar_online_repairs = value
        else:
            self.debug_online_repairs = value

    @property
    def mrb_devices(self):
        return self.ar_mrb_devices if self.app_mode == "assembly_rework" else self.debug_mrb_devices

    @mrb_devices.setter
    def mrb_devices(self, value):
        if self.app_mode == "assembly_rework":
            self.ar_mrb_devices = value
        else:
            self.debug_mrb_devices = value

    @property
    def io_queue(self):
        return self.ar_io_queue if self.app_mode == "assembly_rework" else self.debug_io_queue

    @io_queue.setter
    def io_queue(self, value):
        if self.app_mode == "assembly_rework":
            self.ar_io_queue = value
        else:
            self.debug_io_queue = value

    @property
    def sheet(self):
        return self.ar_sheet if self.app_mode == "assembly_rework" else self.debug_sheet

    @property
    def completed_sheet(self):
        return self.ar_completed_sheet if self.app_mode == "assembly_rework" else self.debug_completed_sheet

    @property
    def online_repair_sheet(self):
        return self.ar_online_repair_sheet if self.app_mode == "assembly_rework" else self.debug_online_repair_sheet

    @property
    def mrb_sheet(self):
        return self.ar_mrb_sheet if self.app_mode == "assembly_rework" else self.debug_mrb_sheet

    @property
    def _devices_collection(self):
        return AR_DEVICES_COLLECTION if self.app_mode == "assembly_rework" else DEVICES_COLLECTION

    @property
    def _completed_collection(self):
        return AR_COMPLETED_COLLECTION if self.app_mode == "assembly_rework" else COMPLETED_COLLECTION

    @property
    def _online_repair_collection(self):
        return AR_ONLINE_REPAIR_COLLECTION if self.app_mode == "assembly_rework" else ONLINE_REPAIR_COLLECTION

    @property
    def _mrb_collection(self):
        return AR_MRB_COLLECTION if self.app_mode == "assembly_rework" else MRB_COLLECTION

    def _allowed_modes(self) -> set:
        """Which app_mode value(s) this account may use. The owner is
        hardcoded and always unrestricted, matching its existing "always
        has full access" treatment elsewhere; everyone else is governed by
        their account's mode_permission field."""
        if self.username == OWNER_USERNAME:
            return {"debug", "assembly_rework"}
        if self.mode_permission == "both":
            return {"debug", "assembly_rework"}
        return {self.mode_permission}

    # ---------------- mandatory update ----------------
    def _check_for_update_async(self):
        update_info = check_for_update(APP_VERSION, UPDATE_REPO)
        if update_info:
            self.root.after(0, lambda: self._show_mandatory_update_dialog(update_info))

    def _show_mandatory_update_dialog(self, update_info):
        """No "Later"/close option anywhere on this dialog, by design - an
        outdated build is exactly what caused the wrong-name writes this
        whole update mechanism exists to prevent (see gsheets_cache.
        call_with_retry's history), so this blocks any further use of the
        app (grab_set + an ignored WM_DELETE_WINDOW) until the operator
        actually updates, not just until they dismiss a notice."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Update Required")
        dialog.configure(bg=BG)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.protocol("WM_DELETE_WINDOW", lambda: None)

        frame = ttk.Frame(dialog, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Update Required", style="Header.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Label(
            frame,
            text=(
                f"Your version ({APP_VERSION}) is outdated.\n"
                f"Version {update_info['version']} is required to continue.\n\n"
                "The app cannot be used until you update - click Update Now below."
            ),
            style="TLabel", wraplength=380, justify="left",
        ).pack(anchor="w", pady=(0, 16))

        status_label = ttk.Label(frame, text="", style="Muted.TLabel", wraplength=380)
        status_label.pack(anchor="w", fill="x", pady=(0, 8))
        progress = ttk.Progressbar(frame, mode="determinate", length=360)
        progress.pack(fill="x", pady=(0, 16))
        update_button = ttk.Button(frame, text="Update Now", style="Accent.TButton")
        update_button.pack(anchor="e")

        def _on_progress(written, total):
            pct = (written / total) * 100 if total else 0
            self.root.after(0, lambda: (
                progress.config(value=pct),
                status_label.config(
                    text=f"Downloading update... {written // 1024} / {total // 1024} KB", foreground=MUTED,
                ),
            ))

        def _fail(message):
            self.root.after(0, lambda: (
                status_label.config(text=message, foreground=DANGER),
                update_button.config(state="normal", text="Retry"),
            ))

        def _do_download():
            target_exe = Path(sys.executable).resolve()
            dest = target_exe.with_name("update_download" + target_exe.suffix)
            try:
                download_asset(
                    update_info["asset_url"], dest, on_progress=_on_progress,
                    expected_sha256=update_info.get("sha256"),
                )
            except Exception as exc:  # noqa: BLE001 - shown to the operator, dialog stays up to retry
                _fail(f"Download failed: {exc}")
                return
            self.root.after(0, lambda: status_label.config(
                text="Installing update and restarting...", foreground=MUTED,
            ))
            try:
                apply_update_and_restart(dest, target_exe)  # never returns on success
            except Exception as exc:  # noqa: BLE001
                _fail(f"Update failed: {exc}")

        update_button.config(
            command=lambda: (
                update_button.config(state="disabled", text="Updating..."),
                threading.Thread(target=_do_download, daemon=True).start(),
            )
        )

        # update_idletasks before reading winfo_width/height - matches the
        # centering pattern other modals in this app already use (e.g.
        # _ask_ar_from_district); immediately after creation the dialog is
        # still sized 1x1, so centering against that would be meaningless.
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dialog.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dialog.winfo_height()) // 3
        dialog.geometry(f"+{x}+{y}")
        dialog.grab_set()
        dialog.focus_set()

    # ---------------- login screen ----------------
    def _build_login_frame(self):
        self.login_frame = ttk.Frame(self.root)
        self.login_frame.pack(fill="both", expand=True)

        # Centered regardless of window size: an inner form frame placed at
        # the middle of the (fullscreen) outer frame, instead of grid/pack
        # which would otherwise anchor it to the top-left corner.
        form = ttk.Frame(self.login_frame, padding=40)
        form.place(relx=0.5, rely=0.5, anchor="center")

        ttk.Label(form, text=" DRD Accounting Tool", style="Header.TLabel").grid(
            row=0, column=0, columnspan=2, pady=(0, 4), sticky="w"
        )
        ttk.Label(form, text="Login", style="Muted.TLabel").grid(
            row=1, column=0, columnspan=2, pady=(0, 20), sticky="w"
        )

        ttk.Label(form, text="Username").grid(row=2, column=0, sticky="e", padx=(0, 10), pady=6)
        self.username_entry = ttk.Entry(form, width=30)
        self.username_entry.grid(row=2, column=1, pady=6)

        ttk.Label(form, text="Password").grid(row=3, column=0, sticky="e", padx=(0, 10), pady=6)
        self.password_entry = ttk.Entry(form, width=30, show="*")
        self.password_entry.grid(row=3, column=1, pady=6)
        self.password_entry.bind("<Return>", lambda _e: self._on_login_click())

        self.login_status_label = ttk.Label(form, text="", style="Error.TLabel", wraplength=320)
        self.login_status_label.grid(row=4, column=0, columnspan=2, pady=8)

        self.login_button = ttk.Button(
            form, text="LOGIN", style="Accent.TButton", command=self._on_login_click
        )
        self.login_button.grid(row=5, column=0, columnspan=2, pady=12, sticky="we")

    def _on_login_click(self):
        username = self.username_entry.get().strip()
        password = self.password_entry.get()
        if not username or not password:
            self.login_status_label.config(
                text="Please enter both username and password.", foreground=DANGER
            )
            return
        self.login_button.config(state="disabled")
        self.login_status_label.config(text="Logging in...", foreground=ACCENT)
        threading.Thread(target=self._do_login, args=(username, password), daemon=True).start()

    def _do_login(self, username, password):
        def fail(error):
            self.result_queue.put((
                "login_result",
                {"ok": False, "error": error, "username": username, "password": password},
            ))

        try:
            db = init_firestore(FIREBASE_KEY_FILE)
        except Exception as exc:  # noqa: BLE001
            fail(f"Failed to connect to the shared database: {exc}")
            return

        # App-level accounts are separate from the real WebDB credentials:
        # the owner account is hardcoded, everyone else is an account the
        # owner/an admin created in Settings (stored in Firestore, with a
        # hashed password and a role of "admin" or "user").
        if username == OWNER_USERNAME and password == OWNER_PASSWORD:
            role = "owner"
            mode_permission = "both"
        else:
            account = get_app_account(db, username)
            if account and account.get("password_hash") == hash_password(password):
                role = account.get("role") or "user"
                # Accounts created before mode permissions existed have no
                # such field - treat that as "both" so access doesn't
                # silently narrow for them.
                mode_permission = account.get("mode_permission") or "both"
            else:
                role = None
                mode_permission = None
        if role is None:
            fail("Incorrect username or password.")
            return

        # The WebDB login (Selenium) and the various Google Sheets
        # connections are all independent of each other - run them
        # concurrently instead of one after another, which was the
        # dominant cost in login time.
        with ThreadPoolExecutor(max_workers=16) as executor:
            webdb_future = executor.submit(_build_and_login_driver)
            sheet_future = (
                executor.submit(init_sheet, FIREBASE_KEY_FILE, GOOGLE_SHEET_ID) if GOOGLE_SHEET_ID else None
            )
            completed_sheet_future = (
                executor.submit(init_completed_sheet, FIREBASE_KEY_FILE, GOOGLE_SHEET_ID)
                if GOOGLE_SHEET_ID else None
            )
            online_repair_sheet_future = (
                executor.submit(init_online_repair_sheet, FIREBASE_KEY_FILE, GOOGLE_SHEET_ID)
                if GOOGLE_SHEET_ID else None
            )
            ar_sheet_future = (
                executor.submit(init_ar_sheet, FIREBASE_KEY_FILE, GOOGLE_SHEET_ID) if GOOGLE_SHEET_ID else None
            )
            ar_completed_sheet_future = (
                executor.submit(init_ar_completed_sheet, FIREBASE_KEY_FILE, GOOGLE_SHEET_ID)
                if GOOGLE_SHEET_ID else None
            )
            ar_online_repair_sheet_future = (
                executor.submit(init_ar_online_repair_sheet, FIREBASE_KEY_FILE, GOOGLE_SHEET_ID)
                if GOOGLE_SHEET_ID else None
            )
            mrb_sheet_future = (
                executor.submit(init_mrb_sheet, FIREBASE_KEY_FILE, GOOGLE_SHEET_ID)
                if GOOGLE_SHEET_ID else None
            )
            ar_mrb_sheet_future = (
                executor.submit(init_ar_mrb_sheet, FIREBASE_KEY_FILE, GOOGLE_SHEET_ID)
                if GOOGLE_SHEET_ID else None
            )
            qr_change_sheet_future = (
                executor.submit(init_qr_change_sheet, FIREBASE_KEY_FILE, GOOGLE_SHEET_ID)
                if GOOGLE_SHEET_ID else None
            )
            pcb_rules_future = (
                executor.submit(load_pcb_choice_rules, FIREBASE_KEY_FILE, GOOGLE_SHEET_ID)
                if GOOGLE_SHEET_ID else None
            )
            operator_names_future = (
                executor.submit(load_operator_names, FIREBASE_KEY_FILE, GOOGLE_SHEET_ID)
                if GOOGLE_SHEET_ID else None
            )
            from_sheet_future = executor.submit(init_from_sheet, FIREBASE_KEY_FILE, PROD5_SHEET_ID)
            fact_ok_sheet_future = executor.submit(init_fact_ok_sheet, FIREBASE_KEY_FILE, PROD5_SHEET_ID)
            fact_dr_sheet_future = executor.submit(init_fact_dr_sheet, FIREBASE_KEY_FILE, PROD5_SHEET_ID)
            ar_prod5_from_sheet_future = executor.submit(
                init_ar_prod5_from_sheet, FIREBASE_KEY_FILE, AR_PROD5_SHEET_ID
            )
            ar_fact_rework_sheet_future = executor.submit(
                init_ar_fact_rework_sheet, FIREBASE_KEY_FILE, AR_PROD5_SHEET_ID
            )
            device_choice_rules_future = (
                executor.submit(load_device_choice_rules, FIREBASE_KEY_FILE, GOOGLE_SHEET_ID)
                if GOOGLE_SHEET_ID else None
            )

            try:
                driver = webdb_future.result()
            except Exception as exc:  # noqa: BLE001
                fail(str(exc))
                return

            # pcb_choice_rules/device_choice_rules/operator_names feed
            # directly into what gets written to the Buffer sheet on every
            # scan (Board/Device name, operator display name) - unlike the
            # other sheet connections below, silently continuing with an
            # empty ruleset here doesn't just disable a feature, it makes
            # every write for this WHOLE SESSION silently wrong (raw WebDB
            # text instead of the mapped name, raw login username instead
            # of the real name) with nothing to show for it. Each already
            # retries 3x against Google Sheets (see gsheets_cache.
            # call_with_retry). If it's STILL failing past that - a longer
            # Google Sheets outage/rate limit, not a few-second blip - fall
            # back to the last known-good copy saved locally on this
            # machine (see _load_json_cache/_save_json_cache) rather than
            # locking every operator out of the app for however long that
            # lasts. Only block login outright when there's no local cache
            # to fall back on either (a brand new machine's very first
            # login during an outage) - that's the one case with truly no
            # safe data to proceed with.
            require_warnings = []

            def _require(future, label, cache_file, empty_default, to_jsonable=lambda v: v):
                if future is None:
                    return empty_default
                try:
                    value = future.result()
                except Exception as exc:  # noqa: BLE001
                    cached = _load_json_cache(cache_file)
                    if cached is not None:
                        require_warnings.append(
                            f"Could not load {label} from Google Sheets ({exc}) - using the last "
                            f"known-good copy saved on this machine instead. Ask an admin to check "
                            f"Google Sheets connectivity if this keeps happening."
                        )
                        return cached
                    fail(
                        f"Could not load {label}: {exc}\n"
                        "This is usually a temporary Google Sheets rate limit - "
                        "please wait about 10 seconds, then press Login again."
                    )
                    raise
                _save_json_cache(cache_file, to_jsonable(value))
                return value

            try:
                pcb_choice_rules = _require(
                    pcb_rules_future, "the PCB choice sheet", PCB_CHOICE_CACHE_FILE, [],
                    to_jsonable=_pcb_choice_rules_to_jsonable,
                )
                device_choice_rules = _require(
                    device_choice_rules_future, "the Device choice sheet", DEVICE_CHOICE_CACHE_FILE, [],
                )
                operator_names = _require(
                    operator_names_future, "the User sheet", OPERATOR_NAMES_CACHE_FILE, {},
                )
            except Exception:  # noqa: BLE001 - fail() already queued the login_result above
                return

            sheet_warnings = list(require_warnings)

            def _resolve(future, label):
                if future is None:
                    return None
                try:
                    return future.result()
                except Exception as exc:  # noqa: BLE001 - reporting is best-effort, don't block login
                    sheet_warnings.append(f"Could not connect to {label}: {exc}")
                    return None

            sheet = _resolve(sheet_future, "the report Google Sheet")
            completed_sheet = _resolve(completed_sheet_future, "the report Google Sheet")
            online_repair_sheet = _resolve(online_repair_sheet_future, "the Online Repair sheet")
            ar_sheet = _resolve(ar_sheet_future, "the Assembly Rework sheet")
            ar_completed_sheet = _resolve(ar_completed_sheet_future, "the Assembly Rework Completed sheet")
            ar_online_repair_sheet = _resolve(
                ar_online_repair_sheet_future, "the Assembly Rework Online Repair sheet"
            )
            mrb_sheet = _resolve(mrb_sheet_future, "the MRB sheet")
            ar_mrb_sheet = _resolve(ar_mrb_sheet_future, "the Assembly Rework MRB sheet")
            qr_change_sheet = _resolve(qr_change_sheet_future, "the QR Change sheet")
            from_sheet = _resolve(from_sheet_future, "the PROD5 VTP FROM sheet")
            fact_ok_sheet = _resolve(fact_ok_sheet_future, "the PROD5 VTP Fact-D&R_ok sheet")
            fact_dr_sheet = _resolve(fact_dr_sheet_future, "the PROD5 VTP Fact-D&R sheet")
            ar_prod5_from_sheet = _resolve(
                ar_prod5_from_sheet_future, "the Assembly Rework PROD5 VTP FROM sheet"
            )
            ar_fact_rework_sheet = _resolve(
                ar_fact_rework_sheet_future, "the Assembly Rework PROD5 VTP Fact-Rework sheet"
            )

        self.result_queue.put((
            "login_result",
            {"ok": True, "driver": driver, "db": db, "sheet": sheet, "completed_sheet": completed_sheet,
             "online_repair_sheet": online_repair_sheet,
             "ar_sheet": ar_sheet, "ar_completed_sheet": ar_completed_sheet,
             "ar_online_repair_sheet": ar_online_repair_sheet,
             "mrb_sheet": mrb_sheet, "ar_mrb_sheet": ar_mrb_sheet, "qr_change_sheet": qr_change_sheet,
             "pcb_choice_rules": pcb_choice_rules, "operator_names": operator_names,
             "from_sheet": from_sheet, "fact_ok_sheet": fact_ok_sheet, "fact_dr_sheet": fact_dr_sheet,
             "ar_prod5_from_sheet": ar_prod5_from_sheet, "ar_fact_rework_sheet": ar_fact_rework_sheet,
             "device_choice_rules": device_choice_rules,
             "sheet_warning": "\n".join(sheet_warnings) if sheet_warnings else None,
             "username": username, "password": password, "role": role,
             "mode_permission": mode_permission},
        ))

    # ---------------- main screen ----------------
    def _build_main_frame(self):
        self.main_frame = ttk.Frame(self.root, padding=16)
        self.main_frame.pack(fill="both", expand=True)

        top = ttk.Frame(self.main_frame)
        top.pack(fill="x", pady=(0, 14))
        header_box = ttk.Frame(top)
        header_box.pack(side="left")
        ttk.Label(header_box, text="DRD Accounting Tool", style="Header.TLabel").pack(anchor="w")
        user_row = ttk.Frame(header_box)
        user_row.pack(anchor="w")
        ttk.Label(user_row, text="Logged in: ", style="Muted.TLabel").pack(side="left")
        ttk.Label(user_row, text=self.username, style="Accent.TLabel").pack(side="left")
        # Always visible regardless of which view_mode tab is showing (see
        # _update_queue_counter_label) - unlike the Queue view itself
        # (click the "Queue" button for the full list), this needs to be
        # glanceable from anywhere in the app, not buried in the small
        # muted username row - a solid badge in the main header bar
        # itself, same visual weight as the Logout/Settings buttons.
        self.queue_counter_label = ttk.Label(top, text="", style="QueueBadge.TLabel")
        self.queue_counter_label.pack(side="left", padx=(24, 0))
        ttk.Button(top, text="Logout", style="Danger.TButton", command=self._on_logout).pack(side="right")
        if self.role in ("owner", "admin"):
            ttk.Button(
                top, text="Settings", style="Secondary.TButton", command=self._open_settings_window
            ).pack(side="right", padx=(0, 8))

        mode_row = ttk.Frame(top)
        mode_row.pack(side="right", padx=(0, 8))
        self.mode_buttons = {}
        allowed_modes = self._allowed_modes()
        for mode, label in (("debug", "DEBUG"), ("assembly_rework", "ASSEMBLY REWORK")):
            btn = ttk.Button(
                mode_row, text=label,
                style="Accent.TButton" if mode == self.app_mode else "Secondary.TButton",
                command=lambda m=mode: self._set_app_mode(m),
                state="normal" if mode in allowed_modes else "disabled",
            )
            btn.pack(side="left", padx=(0, 4))
            self.mode_buttons[mode] = btn

        scan_frame = ttk.Frame(self.main_frame)
        scan_frame.pack(fill="x", pady=(0, 10))
        ttk.Label(scan_frame, text="Scan QR:").pack(side="left")
        self.scan_entry = ttk.Entry(scan_frame, width=40)
        self.scan_entry.pack(side="left", padx=8, fill="x", expand=True)
        self.scan_entry.bind("<Return>", self._on_scan_submit)
        self.reconnect_button = ttk.Button(
            scan_frame, text="🔄 Reconnect WebDB", style="Secondary.TButton",
            command=self._on_manual_reconnect,
        )
        self.reconnect_button.pack(side="left", padx=(8, 0))

        serial_frame = ttk.Frame(self.main_frame)
        serial_frame.pack(fill="x", pady=(0, 10))
        ttk.Label(serial_frame, text="Serial Scanner Port:", style="Muted.TLabel").pack(side="left")
        self.serial_port_combo = ttk.Combobox(serial_frame, width=22)
        self.serial_port_combo.pack(side="left", padx=8)
        ttk.Button(
            serial_frame, text="⟳", width=3, style="Secondary.TButton", command=self._refresh_serial_ports
        ).pack(side="left")
        self.serial_connect_button = ttk.Button(
            serial_frame, text="Connect", style="Secondary.TButton",
            command=self._on_serial_scanner_toggle,
        )
        self.serial_connect_button.pack(side="left", padx=(8, 0))
        self.serial_status_label = ttk.Label(serial_frame, text="Not connected", style="Muted.TLabel")
        self.serial_status_label.pack(side="left", padx=(12, 0))
        self._refresh_serial_ports()
        self._restore_serial_scanner_state()

        self.status_label = ttk.Label(self.main_frame, text="Ready.", style="Muted.TLabel")
        self.status_label.pack(fill="x", pady=(0, 8))

        view_row = ttk.Frame(self.main_frame)
        view_row.pack(fill="x", pady=(0, 10))
        for mode, label in (
            ("queue", "Queue"), ("online_repair", "Online Repair"), ("buffer", "Buffer"),
            ("completed", "Completed List"), ("mrb", "MRB"), ("qr_change", "QR Change"),
            ("chart", "Chart"), ("summary", "Summary"),
        ):
            btn = ttk.Button(
                view_row, text=label,
                style="Accent.TButton" if mode == self.view_mode else "Secondary.TButton",
                command=lambda m=mode: self._set_view_mode(m),
                state="normal" if mode not in _REPORT_VIEW_MODES or self._can_view_reports() else "disabled",
            )
            btn.pack(side="left", padx=(0, 8))
            self.view_buttons[mode] = btn

        # buffer/completed view: table + summary side by side
        self.content_frame = ttk.Frame(self.main_frame)
        self.content_frame.pack(fill="both", expand=True)

        table_frame = ttk.Frame(self.content_frame)
        table_frame.pack(side="left", fill="both", expand=True)

        self.tree = ttk.Treeview(
            table_frame, columns=[c[0] for c in BUFFER_COLUMNS], show="headings", height=12
        )
        self.tree.tag_configure("evenrow", background=FIELD_BG)
        self.tree.tag_configure("oddrow", background=PANEL_BG)
        self.tree.tag_configure("highlight", background=ACCENT, foreground=ACCENT_TEXT)

        tree_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")
        self.tree.bind("<Delete>", self._on_delete_selected)
        self.tree.bind("<Button-1>", self._on_tree_click)

        summary_frame = ttk.Frame(self.content_frame, padding=(16, 0, 0, 0))
        summary_frame.pack(side="right", fill="y")
        ttk.Label(summary_frame, text="Summary", style="Header.TLabel").pack(anchor="w", pady=(0, 8))
        self.summary_tree = ttk.Treeview(
            summary_frame, columns=("model", "qty"), show="headings", height=12
        )
        self.summary_tree.heading("model", text="Model")
        self.summary_tree.heading("qty", text="Qty")
        self.summary_tree.column("model", width=170, anchor="w")
        self.summary_tree.column("qty", width=60, anchor="center")
        self.summary_tree.tag_configure("evenrow", background=FIELD_BG)
        self.summary_tree.tag_configure("oddrow", background=PANEL_BG)
        self.summary_tree.pack(fill="y")
        self.summary_total_label = ttk.Label(summary_frame, text="", style="Accent.TLabel")
        self.summary_total_label.pack(anchor="w", pady=(10, 0))

        # chart view: built once, shown/hidden in place of content_frame
        self.chart_frame = ttk.Frame(self.main_frame)
        chart_range_row = ttk.Frame(self.chart_frame)
        chart_range_row.pack(fill="x", pady=(0, 10))
        ttk.Label(chart_range_row, text="Range:", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        for days, label in ((7, "Week"), (30, "Month"), (90, "3 Months")):
            btn = ttk.Button(
                chart_range_row, text=label,
                style="Accent.TButton" if days == self.chart_range_days else "Secondary.TButton",
                command=lambda d=days: self._set_chart_range(d),
            )
            btn.pack(side="left", padx=(0, 8))
            self.chart_range_buttons[days] = btn
        fig = Figure(figsize=(9, 5), dpi=100, facecolor=BG)
        self.chart_ax = fig.add_subplot(111)
        self.chart_canvas = FigureCanvasTkAgg(fig, master=self.chart_frame)
        self.chart_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.chart_canvas.mpl_connect("motion_notify_event", self._on_chart_hover)

        # summary report view: built once, shown/hidden in place of content_frame
        self.summary_frame = ttk.Frame(self.main_frame)
        self._build_summary_view(self.summary_frame)

        button_row = ttk.Frame(self.main_frame)
        button_row.pack(fill="x", pady=(12, 0))
        self.refresh_button = ttk.Button(
            button_row, text="Refresh", style="Accent.TButton", command=self._on_refresh_click
        )
        self.refresh_button.pack(side="left", padx=(0, 8))
        self.load_button = ttk.Button(
            button_row, text="⟳ Load", style="Secondary.TButton", command=self._on_load_buffer_click
        )
        self.load_button.pack(side="left", padx=(0, 8))
        self.delete_button = ttk.Button(
            button_row, text="Delete", style="Danger.TButton", command=self._on_delete_selected
        )
        self.delete_button.pack(side="left")

        initial_columns = self._columns_for_mode({
            "buffer": BUFFER_COLUMNS, "completed": COMPLETED_COLUMNS, "queue": QUEUE_COLUMNS,
            "online_repair": ONLINE_REPAIR_COLUMNS, "mrb": MRB_COLUMNS, "qr_change": QR_CHANGE_COLUMNS,
        }[self.view_mode])
        self._apply_tree_columns(initial_columns)
        self._refresh_tree()
        self.refresh_button.config(
            state="normal" if self.view_mode in ("buffer", "completed", "mrb", "qr_change") else "disabled"
        )
        if self.view_mode in ("completed", "mrb", "qr_change"):
            self._fetch_on_demand(self.view_mode)
        self._update_delete_button_state()
        self._update_queue_counter_label()
        self.scan_entry.focus_set()
        self._tick_elapsed()

    def _columns_for_mode(self, columns_spec):
        """Swap header text for Assembly Rework's vocabulary (see
        _AR_HEADER_OVERRIDES) - a no-op in Debug mode, and never touches
        the dict key/sort_key, only the label shown in the header."""
        if self.app_mode != "assembly_rework":
            return columns_spec
        return [
            (key, _AR_HEADER_OVERRIDES.get(key, header), width, anchor, stretch, sort_key)
            for key, header, width, anchor, stretch, sort_key in columns_spec
        ]

    def _apply_tree_columns(self, columns_spec):
        self._current_columns = columns_spec
        self.tree["columns"] = [c[0] for c in columns_spec]
        for key, header, width, anchor, stretch, sort_key in columns_spec:
            text = header
            if sort_key and sort_key == self.sort_column:
                text = f"{header} {'▼' if self.sort_reverse else '▲'}"
            if sort_key:
                self.tree.heading(key, text=text, command=lambda k=sort_key: self._sort_by(k))
            else:
                self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor=anchor, stretch=stretch)

    def _view_data(self, mode=None):
        """Returns (items, checked_set, id_key) for the given view mode (or
        the current one). id_key is the field used as each row's unique
        Treeview iid - qr for buffer/completed, but the Firestore doc id for
        online_repair, since the same qr can legitimately appear more than
        once there (a device can be online-repaired multiple times)."""
        mode = mode or self.view_mode
        if mode == "buffer":
            return self.devices, self.checked_qrs, "qr"
        if mode == "completed":
            return self.completed_devices, self.checked_completed_qrs, "qr"
        if mode == "online_repair":
            return self.online_repairs, self.checked_online_repair_ids, "_doc_id"
        if mode == "mrb":
            return self.mrb_devices, self.checked_mrb_qrs, "qr"
        if mode == "qr_change":
            return self.qr_changes, self.checked_qr_change_ids, "pcb_qr"
        if mode == "queue":
            return self.io_queue, self.checked_io_queue_ids, "_doc_id"
        return [], set(), "qr"

    def _set_app_mode(self, mode):
        """Switch between Debug and Assembly Rework - each has its own
        devices/completed_devices/online_repairs, Firestore collections and
        Google Sheet tabs (see the mode-resolving properties near
        __init__), so this just repoints those properties and refreshes
        whatever's currently on screen - the view_mode (Buffer/Completed/
        Online Repair/Chart/Summary) and its columns/layout stay as-is."""
        if mode == self.app_mode:
            return
        if mode not in self._allowed_modes():
            # The button for this mode is already disabled, so this only
            # matters as a safety net against some other code path calling
            # this directly.
            self._show_toast("You don't have permission for that mode.", kind="error")
            return
        self.app_mode = mode
        for m, btn in self.mode_buttons.items():
            btn.config(style="Accent.TButton" if m == mode else "Secondary.TButton")
        # Checked/selected rows belong to the list that was on screen a
        # moment ago in the other mode - carrying them over makes no sense
        # once the displayed data changes out from under them.
        self.checked_qrs = set()
        self.checked_completed_qrs = set()
        self.checked_online_repair_ids = set()
        self.checked_mrb_qrs = set()
        self.checked_io_queue_ids = set()
        self._reset_sort_to_default()
        self._update_queue_counter_label()
        if self.view_mode == "chart":
            self._render_chart()
        elif self.view_mode == "summary":
            self._render_summary_report()
        else:
            # Header text (Board/Device, Debug/Assembler) depends on
            # app_mode, so this needs re-applying even though view_mode
            # itself didn't change.
            columns = self._columns_for_mode({
                "buffer": BUFFER_COLUMNS, "completed": COMPLETED_COLUMNS, "queue": QUEUE_COLUMNS,
                "online_repair": ONLINE_REPAIR_COLUMNS, "mrb": MRB_COLUMNS, "qr_change": QR_CHANGE_COLUMNS,
            }[self.view_mode])
            self._apply_tree_columns(columns)
            self._refresh_tree()
            self.refresh_button.config(
                state="normal" if self.view_mode in ("buffer", "completed", "mrb", "qr_change") else "disabled"
            )
            if self.view_mode in ("completed", "mrb"):
                # Mode-split (unlike qr_change, a single shared collection) -
                # the list just repointed to a different underlying
                # collection, so whatever was fetched under the other mode
                # doesn't belong here.
                self._fetch_on_demand(self.view_mode)
        self._update_delete_button_state()

    def _set_view_mode(self, mode):
        if mode == self.view_mode:
            return
        if mode in _REPORT_VIEW_MODES and not self._can_view_reports():
            # The button is already disabled for this role - this is a
            # safety net against some other code path calling this directly.
            self._show_toast("You don't have permission for that view.", kind="error")
            return
        self.view_mode = mode
        self._reset_sort_to_default()
        for m, btn in self.view_buttons.items():
            btn.config(style="Accent.TButton" if m == mode else "Secondary.TButton")

        self.content_frame.pack_forget()
        self.chart_frame.pack_forget()
        self.summary_frame.pack_forget()
        if mode == "chart":
            self._render_chart()
            self.chart_frame.pack(fill="both", expand=True)
            self.refresh_button.config(state="disabled")
        elif mode == "summary":
            self._render_summary_report()
            self.summary_frame.pack(fill="both", expand=True)
            self.refresh_button.config(state="disabled")
        else:
            self.content_frame.pack(fill="both", expand=True)
            columns = self._columns_for_mode({
                "buffer": BUFFER_COLUMNS, "completed": COMPLETED_COLUMNS, "queue": QUEUE_COLUMNS,
                "online_repair": ONLINE_REPAIR_COLUMNS, "mrb": MRB_COLUMNS, "qr_change": QR_CHANGE_COLUMNS,
            }[mode])
            self._apply_tree_columns(columns)
            self._refresh_tree()
            self.refresh_button.config(state="normal" if mode in ("buffer", "completed", "mrb", "qr_change") else "disabled")
            if mode in ("completed", "mrb", "qr_change"):
                self._fetch_on_demand(mode)
        self._update_delete_button_state()

    def _refresh_tree(self):
        if self.view_mode in ("chart", "summary"):
            return
        for row in self.tree.get_children():
            self.tree.delete(row)
        items, checked, id_key = self._view_data()
        for i, device in enumerate(items):
            tag = "evenrow" if i % 2 == 0 else "oddrow"
            row_id = device[id_key]
            check_mark = "☑" if row_id in checked else "☐"
            if self.view_mode == "buffer":
                values = (
                    check_mark,
                    device["qr"],
                    device.get("board", ""),
                    device.get("import_time", ""),
                    elapsed_since(device.get("import_time_iso")),
                    device.get("attempt_count", ""),
                    device.get("defect", ""),
                )
            elif self.view_mode == "completed":
                values = (
                    check_mark,
                    device.get("qr", ""),
                    device.get("board", ""),
                    device.get("import_time", ""),
                    device.get("attempt_count", ""),
                    device.get("complete_time", ""),
                    device.get("debug_operator", "") or "No info",
                    device.get("defect", ""),
                )
            elif self.view_mode == "mrb":
                values = (
                    check_mark,
                    device.get("qr", ""),
                    device.get("board", ""),
                    device.get("mrb_time", ""),
                    device.get("debug_operator", "") or "No info",
                    device.get("defect", "") or "No info",
                )
            elif self.view_mode == "qr_change":
                values = (
                    check_mark,
                    device.get("pcb_qr", ""),
                    device.get("previous_qr", ""),
                    device.get("current_qr", ""),
                    device.get("model", "") or "No info",
                    device.get("scan_time", ""),
                )
            elif self.view_mode == "queue":
                values = (
                    check_mark,
                    device.get("qr", ""),
                    _QUEUE_DEPARTMENT_LABELS.get(device.get("department"), device.get("department", "")),
                    device.get("scanned_by", "") or "Unknown",
                    device.get("scanned_time", ""),
                )
            else:  # online_repair
                values = (
                    check_mark,
                    device.get("qr", ""),
                    device.get("board", ""),
                    device.get("repair_time", ""),
                    device.get("debug_operator", "") or "No info",
                    device.get("defect", "") or "No info",
                )
            self.tree.insert("", "end", iid=row_id, tags=(tag,), values=values)
        self._refresh_summary()

    def _refresh_summary(self):
        for row in self.summary_tree.get_children():
            self.summary_tree.delete(row)
        if self.view_mode == "online_repair":
            # Matches the factory's actual Day (8AM-8PM)/Night shift concept
            # (see prod5_sheet.shift_and_period, already used the same way
            # by this file's own undo-on-delete logic) rather than a plain
            # UTC calendar date - the previous UTC-date check silently
            # miscounted anything scanned 12:00-6:59 AM GMT+7 into
            # "yesterday", since GMT+7 midnight is still 5PM UTC the day before.
            now_gmt7 = dt.datetime.now(dt.timezone.utc).astimezone(GMT7)
            expected_shift, expected_period = shift_and_period(now_gmt7)
            items = [
                r for r in self.online_repairs
                if _parse_iso_to_gmt7(r.get("repair_time_iso")) is not None
                and shift_and_period(_parse_iso_to_gmt7(r.get("repair_time_iso")))
                == (expected_shift, expected_period)
            ]
            label = f"Online Repair - {expected_shift} shift"
        elif self.view_mode == "queue":
            # No "board" concept for a queue entry (see public/*/index.html -
            # scan pages only ever send qr+department, deliberately no
            # lookup, for speed) - broken down by department instead.
            items = self.io_queue
            label = "Queue"
            counts: dict[str, int] = {}
            for entry in items:
                dep = _QUEUE_DEPARTMENT_LABELS.get(entry.get("department"), entry.get("department") or "Unknown")
                counts[dep] = counts.get(dep, 0) + 1
            for i, (dep, qty) in enumerate(sorted(counts.items())):
                self.summary_tree.insert(
                    "", "end",
                    tags=("evenrow" if i % 2 == 0 else "oddrow",),
                    values=(dep, qty),
                )
            self.summary_total_label.config(text=f"{label}: {len(items)}")
            return
        else:
            items = self.devices
            label = "Total PCB"
        counts: dict[str, int] = {}
        for device in items:
            model = device.get("board") or "Unknown"
            counts[model] = counts.get(model, 0) + 1
        for i, (model, qty) in enumerate(sorted(counts.items())):
            self.summary_tree.insert(
                "", "end",
                tags=("evenrow" if i % 2 == 0 else "oddrow",),
                values=(model, qty),
            )
        self.summary_total_label.config(text=f"{label}: {len(items)}")

    def _update_queue_counter_label(self):
        """Glanceable count for the department(s) this app_mode consumes
        (Hard Test for Debug, the other four for Assembly Rework - see
        _DEBUG_QUEUE_DEPARTMENTS/_AR_QUEUE_DEPARTMENTS) - always visible in
        the header regardless of which view_mode tab is showing. Click
        "Queue" to see the full list."""
        if not hasattr(self, "queue_counter_label"):
            return
        self.queue_counter_label.config(text=f"\U0001f4cb Queue: {len(self.io_queue)}")

    def _on_snapshot_update(self):
        """Called whenever a new Firestore snapshot (Buffer or the live
        Online Repair window) arrives, regardless of which view is
        currently showing. Deliberately does NOT call _render_chart()/
        _render_summary_report() directly for the chart/summary case -
        those re-fetch Completed (and, for summary, historical Online
        Repair) from Firestore, and re-triggering that on every single
        Buffer/Online-Repair write would multiply reads right back up for
        anyone who just leaves either tab open during a busy shift.
        Re-rendering from the last-fetched cache instead keeps the Buffer
        line current (it's computed fresh from self.devices every call)
        without paying for another fetch."""
        if self.view_mode == "chart":
            self._render_chart_with(
                self.devices, self._chart_completed_cache,
                self._chart_repairs_cache if self._chart_repairs_cache is not None else self.online_repairs,
                self.chart_range_days,
            )
        elif self.view_mode == "summary":
            if self._summary_range_cache is not None:
                start_date, end_date, shift_filter = self._summary_range_cache
                self._render_summary_report_with(
                    self.devices, self._summary_completed_cache,
                    self._summary_repairs_cache if self._summary_repairs_cache is not None else self.online_repairs,
                    start_date, end_date, shift_filter,
                )
        else:
            self._apply_current_sort()
            self._refresh_tree()

    # ---- chart view ----
    def _set_chart_range(self, days):
        if days == self.chart_range_days:
            return
        self.chart_range_days = days
        for d, btn in self.chart_range_buttons.items():
            btn.config(style="Accent.TButton" if d == days else "Secondary.TButton")
        self._render_chart()

    def _render_chart(self):
        """Draw immediately with whatever's already in memory (Buffer is
        always current since it stays on a live listener), then kick off
        a background fetch for whatever this range needs that isn't:
        Completed (on-demand only, see the on-demand fetch section - never
        kept in memory just for Chart's sake) and, for a range wider than
        the live Online Repair window, that history too (see
        ONLINE_REPAIR_LIVE_WINDOW_DAYS). Redraws again once that lands."""
        days = self.chart_range_days
        self._render_chart_with(self.devices, self.completed_devices, self.online_repairs, days)
        self._chart_fetch_token += 1
        token = self._chart_fetch_token
        db = self.db
        if db is None:
            return
        completed_collection = self._completed_collection
        online_repair_collection = self._online_repair_collection
        app_mode_at_request = self.app_mode
        need_repair_history = days > ONLINE_REPAIR_LIVE_WINDOW_DAYS
        cutoff_iso = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()

        def _fetch():
            try:
                # Filtered server-side to the selected range (see
                # fetch_completed_since) instead of list_devices' full,
                # ever-growing collection scan - Completed has no live
                # listener at all (on-demand only), so this ran on every
                # single chart open/range change with no bound whatsoever.
                completed = fetch_completed_since(db, cutoff_iso, collection=completed_collection)
                repairs = (
                    fetch_online_repairs_since(db, cutoff_iso, collection=online_repair_collection)
                    if need_repair_history else None
                )
            except Exception:
                return
            self.result_queue.put((
                "chart_history_result",
                {
                    "token": token, "days": days, "app_mode": app_mode_at_request,
                    "completed": completed, "repairs": repairs,
                },
            ))

        threading.Thread(target=_fetch, daemon=True).start()

    def _render_chart_with(self, devices, completed_devices, online_repairs, days):
        buffer_counts = compute_daily_buffer_counts(devices, completed_devices, days=days)
        repair_counts = compute_daily_online_repair_counts(online_repairs, days=days)
        dates = [c[0] for c in buffer_counts]
        buffer_values = [c[1] for c in buffer_counts]
        repair_values = [c[1] for c in repair_counts]
        # Kept for _on_chart_hover, which runs on every mouse-move over the
        # canvas and needs the exact plotted series without recomputing
        # compute_daily_*_counts on every event.
        self._chart_dates = dates
        self._chart_buffer_values = buffer_values
        self._chart_repair_values = repair_values

        range_label = {7: "last week", 30: "last month", 90: "last 3 months"}.get(days, f"last {days} days")

        ax = self.chart_ax
        ax.clear()
        ax.set_facecolor(BG)
        if dates:
            marker = "o" if days <= 30 else None
            ax.plot(
                dates, buffer_values, color=ACCENT, linewidth=2, marker=marker, markersize=4,
                label="Buffer quantity",
            )
            ax.fill_between(dates, buffer_values, color=ACCENT, alpha=0.15)
            ax.plot(
                dates, repair_values, color=CHART_SECONDARY, linewidth=2, marker=marker, markersize=4,
                label="Online Repair (per day)",
            )
        ax.set_title(f"Buffer & Online Repair by Day ({range_label})", color=TEXT, fontsize=12)
        ax.tick_params(colors=TEXT, labelsize=8)
        for spine in ax.spines.values():
            spine.set_color(BORDER)
        ax.grid(True, color=BORDER, linewidth=0.5, alpha=0.5)
        if dates:
            legend = ax.legend(facecolor=PANEL_BG, edgecolor=BORDER, labelcolor=TEXT, fontsize=8)
            legend.get_frame().set_alpha(0.9)
        if days <= 7:
            ax.xaxis.set_major_locator(mdates.DayLocator())
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
        elif days <= 30:
            ax.xaxis.set_major_locator(mdates.DayLocator(interval=3))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
        else:
            ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))

        # The hover annotation/guide line are created here, AFTER the real
        # data is plotted and the axis limits are settled - not before.
        # axvline()/annotate() default to (0, 0) until first positioned by
        # a hover event, and 0 in matplotlib's date-number axis means the
        # year 0001 - creating them earlier let that default position leak
        # into the axes' autoscale range alongside the real ~2026 dates,
        # producing an axis spanning hundreds of thousands of days. That's
        # what caused the "Locator attempting to generate N ticks, which
        # exceeds Locator.MAXTICKS" slowdown (the chart taking minutes to
        # render): matplotlib trying to lay out tens of thousands of tick
        # marks across that bogus range. Explicitly saving and restoring
        # the just-computed xlim/ylim around their creation guarantees
        # these invisible helper artists can never affect the visible
        # axis range, regardless of matplotlib's autoscale behavior.
        xlim, ylim = ax.get_xlim(), ax.get_ylim()
        start_x = dates[0] if dates else 0
        self._chart_vline = ax.axvline(x=start_x, color=MUTED, linewidth=1, linestyle="--", visible=False)
        self._chart_annotation = ax.annotate(
            "", xy=(start_x, 0), xytext=(12, 12), textcoords="offset points",
            bbox=dict(boxstyle="round,pad=0.4", fc=PANEL_BG, ec=ACCENT, alpha=0.95),
            color=TEXT, fontsize=8, visible=False, zorder=10,
        )
        ax.set_xlim(xlim)
        ax.set_ylim(ylim)

        self.chart_canvas.figure.autofmt_xdate()
        self.chart_canvas.draw()
        self._chart_hover_last_idx = None

    def _on_chart_hover(self, event):
        """Show the exact Buffer/Online Repair numbers for whichever day's
        point the mouse is nearest to, via a small annotation box plus a
        dashed guide line - matplotlib has no built-in hover tooltip, so
        this reimplements one: find the closest x (date) to the cursor,
        move the annotation/line there, and hide both again once the
        cursor leaves the axes.

        Tkinter delivers a motion event for every pixel the mouse crosses
        (dozens per second) - redrawing on every single one made the chart
        laggy. The one thing that actually matters for cost here is
        skipping the redraw when the nearest day hasn't changed since the
        last event (most pixel-level moves land on the same day), which
        cuts the number of redraws by an order of magnitude on its own.
        (A blitting-based partial-redraw was tried on top of this but
        caused visible corruption whenever the chart canvas had resized
        since the last full render - not worth the risk for the marginal
        gain over plain draw_idle() once redraws are already this rare.)
        """
        if self.view_mode != "chart" or self._chart_annotation is None or not self._chart_dates:
            return
        ax = self.chart_ax
        if event.inaxes != ax or event.xdata is None:
            if self._chart_annotation.get_visible():
                self._chart_annotation.set_visible(False)
                self._chart_vline.set_visible(False)
                self._chart_hover_last_idx = None
                self.chart_canvas.draw_idle()
            return

        x_nums = mdates.date2num(self._chart_dates)
        idx = min(range(len(x_nums)), key=lambda i: abs(x_nums[i] - event.xdata))
        if idx == self._chart_hover_last_idx:
            return
        self._chart_hover_last_idx = idx

        date = self._chart_dates[idx]
        buffer_v = self._chart_buffer_values[idx]
        repair_v = self._chart_repair_values[idx]

        self._chart_annotation.xy = (x_nums[idx], buffer_v)
        self._chart_annotation.set_text(
            f"{date:%Y-%m-%d}\nBuffer: {buffer_v}\nOnline Repair: {repair_v}"
        )
        self._chart_annotation.set_visible(True)
        self._chart_vline.set_xdata([x_nums[idx], x_nums[idx]])
        self._chart_vline.set_visible(True)
        self.chart_canvas.draw_idle()

    # ---- summary report view: flexible date range + shift breakdown ----
    def _build_summary_view(self, parent):
        controls = ttk.Frame(parent)
        controls.pack(fill="x")
        ttk.Label(controls, text="From:").grid(row=0, column=0, sticky="w")
        self.summary_from_entry = ttk.Entry(controls, width=12)
        self.summary_from_entry.grid(row=0, column=1, padx=(4, 12))
        ttk.Label(controls, text="To:").grid(row=0, column=2, sticky="w")
        self.summary_to_entry = ttk.Entry(controls, width=12)
        self.summary_to_entry.grid(row=0, column=3, padx=(4, 12))
        today_str = dt.datetime.now(dt.timezone.utc).astimezone(GMT7).date().strftime("%d.%m.%Y")
        self.summary_from_entry.insert(0, today_str)
        self.summary_to_entry.insert(0, today_str)
        ttk.Button(
            controls, text="Apply", style="Accent.TButton", command=self._render_summary_report
        ).grid(row=0, column=4)
        ttk.Label(controls, text="(DD.MM.YYYY)", style="Muted.TLabel").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(2, 0)
        )

        shift_row = ttk.Frame(parent)
        shift_row.pack(fill="x", pady=(10, 4))
        ttk.Label(shift_row, text="Shift:", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        self.summary_shift = "24h"
        self.summary_shift_buttons = {}
        for value, label in (("24h", "24h"), ("Day", "Day (8AM-8PM)"), ("Night", "Night (8PM-8AM)")):
            btn = ttk.Button(
                shift_row, text=label,
                style="Accent.TButton" if value == "24h" else "Secondary.TButton",
                command=lambda v=value: self._set_summary_shift(v),
            )
            btn.pack(side="left", padx=(0, 8))
            self.summary_shift_buttons[value] = btn

        self.summary_error_label = ttk.Label(parent, text="", style="Muted.TLabel")
        self.summary_error_label.pack(fill="x", pady=(4, 0))

        canvas = tk.Canvas(parent, bg=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True, pady=(8, 0))
        scroll.pack(side="right", fill="y", pady=(8, 0))
        results_frame = ttk.Frame(canvas)
        results_window = canvas.create_window((0, 0), window=results_frame, anchor="nw")
        results_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(results_window, width=e.width))

        def _make_section(title, name_header):
            section = ttk.Frame(results_frame, padding=(0, 0, 0, 16))
            section.pack(fill="x", anchor="w")
            total_label = ttk.Label(section, text=f"{title}: 0", style="Header.TLabel")
            total_label.pack(anchor="w", pady=(0, 6))
            tree = ttk.Treeview(section, columns=("name", "qty"), show="headings", height=6)
            tree.heading("name", text=name_header)
            tree.heading("qty", text="Qty")
            tree.column("name", width=260, anchor="w")
            tree.column("qty", width=80, anchor="center")
            tree.tag_configure("evenrow", background=FIELD_BG)
            tree.tag_configure("oddrow", background=PANEL_BG)
            tree.pack(fill="x")
            return tree, total_label

        self.summary_repair_model_tree, self.summary_repair_model_total = _make_section(
            "Online Repair - by Model", "Model"
        )
        self.summary_repair_user_tree, self.summary_repair_user_total = _make_section(
            "Online Repair - by User", "User"
        )
        self.summary_buffer_model_tree, self.summary_buffer_model_total = _make_section(
            "Buffer - by Model", "Model"
        )
        self.summary_completed_model_tree, self.summary_completed_model_total = _make_section(
            "Completed from Buffer - by Model", "Model"
        )

    @staticmethod
    def _fill_summary_tree(tree, total_label, title, counts: dict):
        for row in tree.get_children():
            tree.delete(row)
        for i, (name, qty) in enumerate(sorted(counts.items())):
            tree.insert("", "end", tags=("evenrow" if i % 2 == 0 else "oddrow",), values=(name, qty))
        total_label.config(text=f"{title}: {sum(counts.values())}")

    def _set_summary_shift(self, value):
        self.summary_shift = value
        for v, btn in self.summary_shift_buttons.items():
            btn.config(style="Accent.TButton" if v == value else "Secondary.TButton")
        self._render_summary_report()

    def _render_summary_report(self):
        """Draw immediately with whatever's already in memory (Buffer is
        always current), then kick off a background fetch for Completed
        (on-demand only - never kept in memory just for Summary's sake)
        and Online Repair history back to the selected start date (the
        live listener only covers the last ONLINE_REPAIR_LIVE_WINDOW_DAYS).
        Redraws again once that lands."""
        try:
            start_date = dt.datetime.strptime(self.summary_from_entry.get().strip(), "%d.%m.%Y").date()
            end_date = dt.datetime.strptime(self.summary_to_entry.get().strip(), "%d.%m.%Y").date()
        except ValueError:
            self.summary_error_label.config(text="Invalid date - use DD.MM.YYYY", foreground=DANGER)
            return
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        self.summary_error_label.config(text="", foreground=MUTED)
        shift_filter = self.summary_shift
        self._render_summary_report_with(
            self.devices, self.completed_devices, self.online_repairs, start_date, end_date, shift_filter
        )

        self._summary_fetch_token += 1
        token = self._summary_fetch_token
        db = self.db
        if db is None:
            return
        completed_collection = self._completed_collection
        online_repair_collection = self._online_repair_collection
        app_mode_at_request = self.app_mode
        # A day of slack behind start_date, since GMT+7 local dates are
        # UTC+7 - covers the local-midnight boundary without needing exact
        # timezone math here; the precise per-record filtering already
        # happens in _matches_date_range_and_shift below either way.
        since_iso = dt.datetime.combine(
            start_date - dt.timedelta(days=1), dt.time.min, tzinfo=dt.timezone.utc
        ).isoformat()

        def _fetch():
            try:
                # See the matching comment in _set_chart_range's _fetch -
                # filtered server-side instead of a full collection scan.
                completed = fetch_completed_since(db, since_iso, collection=completed_collection)
                repairs = fetch_online_repairs_since(db, since_iso, collection=online_repair_collection)
            except Exception:
                return
            self.result_queue.put((
                "summary_history_result",
                {
                    "token": token, "start_date": start_date, "end_date": end_date, "shift": shift_filter,
                    "app_mode": app_mode_at_request, "completed": completed, "repairs": repairs,
                },
            ))

        threading.Thread(target=_fetch, daemon=True).start()

    def _render_summary_report_with(self, devices, completed_devices, online_repairs, start_date, end_date, shift_filter):
        repairs = [
            r for r in online_repairs
            if _matches_date_range_and_shift(r.get("repair_time_iso"), start_date, end_date, shift_filter)
        ]
        repair_by_model: dict[str, int] = {}
        repair_by_user: dict[str, int] = {}
        for r in repairs:
            model = r.get("board") or "Unknown"
            repair_by_model[model] = repair_by_model.get(model, 0) + 1
            username = r.get("debug_operator") or "Unknown"
            display = self.operator_names.get(username, username)
            repair_by_user[display] = repair_by_user.get(display, 0) + 1
        self._fill_summary_tree(
            self.summary_repair_model_tree, self.summary_repair_model_total,
            "Online Repair - by Model", repair_by_model,
        )
        self._fill_summary_tree(
            self.summary_repair_user_tree, self.summary_repair_user_total,
            "Online Repair - by User", repair_by_user,
        )

        # Buffer is a live inventory, not an activity log - "total buffer"
        # for a range means whatever was still sitting in Buffer at the
        # end of `end_date` (imported by then, not yet completed by
        # then), same day-end-snapshot logic the Chart uses, anchored on
        # the selected end date rather than every day in between. The
        # shift filter doesn't apply here since this isn't an event count.
        buffer_by_model: dict[str, int] = {}
        for d in devices:
            imported = _parse_iso_to_gmt7(d.get("import_time_iso"))
            if imported and imported.date() <= end_date:
                model = d.get("board") or "Unknown"
                buffer_by_model[model] = buffer_by_model.get(model, 0) + 1
        for d in completed_devices:
            imported = _parse_iso_to_gmt7(d.get("import_time_iso"))
            completed = _parse_iso_to_gmt7(d.get("complete_time_iso"))
            if imported and imported.date() <= end_date and (
                completed is None or end_date < completed.date()
            ):
                model = d.get("board") or "Unknown"
                buffer_by_model[model] = buffer_by_model.get(model, 0) + 1
        self._fill_summary_tree(
            self.summary_buffer_model_tree, self.summary_buffer_model_total,
            f"Buffer as of {end_date.strftime('%d.%m.%Y')} - by Model", buffer_by_model,
        )

        completed_entries = [
            d for d in completed_devices
            if _matches_date_range_and_shift(
                d.get("complete_time_iso"), start_date, end_date, shift_filter
            )
        ]
        completed_by_model: dict[str, int] = {}
        for d in completed_entries:
            model = d.get("board") or "Unknown"
            completed_by_model[model] = completed_by_model.get(model, 0) + 1
        self._fill_summary_tree(
            self.summary_completed_model_tree, self.summary_completed_model_total,
            "Completed from Buffer - by Model", completed_by_model,
        )

    def _tick_elapsed(self):
        """Recompute the Elapsed column every minute without a network call."""
        if self.view_mode == "buffer":
            for device in self.devices:
                if self.tree.exists(device["qr"]):
                    self.tree.set(device["qr"], "elapsed", elapsed_since(device.get("import_time_iso")))
        self.root.after(60000, self._tick_elapsed)

    def _subscribe_mode_listeners(self, mode):
        """Start one app mode's Online Repair/Queue listeners - only ever
        called for a mode this account is actually allowed to use (see
        mode_permission/_allowed_modes). Most "user" role accounts are
        restricted to exactly one mode, never "both" - so also subscribing
        to the OTHER, permanently-inaccessible mode's listeners (as this
        app used to do unconditionally for every session) was pure wasted
        read cost, paid on every single write to collections that account
        could never even switch into viewing.

        Buffer has no listener here at all anymore - see
        _load_buffer_from_sheet. A permanent Firestore listener on Buffer
        (unlike Online Repair, which is bounded to a rolling window) fanned
        out every single scan's read cost to every one of the 10-15
        machines connected at once, which was the actual dominant driver
        behind this project's Firestore bill - Buffer now reads from its
        Google Sheet mirror instead (loaded once here, and again whenever
        the operator clicks "Load"), and writes still go straight to
        Firestore as before (see e.g. _on_scan_result)."""
        self._load_buffer_from_sheet(mode)
        self._load_online_repairs_from_sheet(mode)
        if mode == "debug":
            self.debug_io_queue_watch = listen_io_queue(
                self.db, self._on_io_queue_changed, _DEBUG_QUEUE_DEPARTMENTS
            )
        else:
            self.ar_io_queue_watch = listen_io_queue(
                self.db, self._on_ar_io_queue_changed, _AR_QUEUE_DEPARTMENTS
            )

    def _unsubscribe_mode_listeners(self, mode):
        """Counterpart to _subscribe_mode_listeners - used when a mode
        permission is revoked live (see _refresh_mode_permission_from_accounts),
        so the listener actually stops (and stops costing reads) instead
        of just becoming unreachable through the UI."""
        attrs = (
            ("debug_devices_watch", "debug_online_repair_watch", "debug_io_queue_watch")
            if mode == "debug"
            else ("ar_devices_watch", "ar_online_repair_watch", "ar_io_queue_watch")
        )
        for attr in attrs:
            watch = getattr(self, attr)
            if watch:
                watch.unsubscribe()
                setattr(self, attr, None)

    def _refresh_online_repair_window(self):
        """Online Repair has no permanent Firestore listener anymore (see
        _subscribe_mode_listeners) - this periodic timer (unchanged
        schedule, repurposed) now just re-loads it from the sheet instead,
        so a session left open for a long time still eventually picks up
        other operators' scans even if nobody clicks "Load" - only for
        whichever mode(s) this account is actually allowed into."""
        for mode in self._allowed_modes():
            self._load_online_repairs_from_sheet(mode)
        self.root.after(_ONLINE_REPAIR_WINDOW_REFRESH_MS, self._refresh_online_repair_window)

    def _on_tree_click(self, event):
        if self.view_mode not in ("buffer", "completed", "online_repair", "mrb", "qr_change", "queue"):
            return
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        _items, checked, _id_key = self._view_data()
        if row_id in checked:
            checked.discard(row_id)
        else:
            checked.add(row_id)
        self.tree.set(row_id, "check", "☑" if row_id in checked else "☐")

    # Online Repair/Completed default to newest-first by time (their most
    # useful order for an operator glancing at the list) rather than
    # whatever order Firestore happens to hand back - Buffer has no
    # equivalent single "the" timestamp column that's obviously right by
    # default, so it's left unsorted. Same defaults for both Debug and
    # Assembly Rework, since neither is mode-specific.
    _DEFAULT_SORT_COLUMN = {
        "online_repair": "repair_time", "completed": "complete_time", "mrb": "mrb_time",
        "qr_change": "scan_time", "queue": "scanned_time",
    }

    def _reset_sort_to_default(self):
        self.sort_column = self._DEFAULT_SORT_COLUMN.get(self.view_mode)
        self.sort_reverse = self.sort_column is not None  # descending = newest first

    def _sort_by(self, column):
        self.sort_reverse = self.sort_column == column and not self.sort_reverse
        self.sort_column = column
        self._apply_current_sort()
        self._apply_tree_columns(self._current_columns)
        self._refresh_tree()

    def _apply_current_sort(self):
        """Re-apply the last-clicked sort column/direction to the list backing
        the current view (self.devices for buffer, self.completed_devices for
        completed).

        Called both from _sort_by (user clicked a header) and whenever a new
        Firestore snapshot arrives, since the DB's own ordering wouldn't
        otherwise match whatever sort the user had selected.
        """
        column = self.sort_column
        if column is None or self.view_mode not in (
            "buffer", "completed", "online_repair", "mrb", "qr_change", "queue",
        ):
            return
        items, _checked, _id_key = self._view_data()
        # Prefer the *_iso field (Firestore-shaped records, precise to the
        # millisecond) but fall back to the plain formatted column
        # (Google-Sheet-shaped records from Load/on-demand fetch never
        # carry an _iso field at all - the sheet has no such column). The
        # formatted string ("YYYY-MM-DD HH:MM:SS GMT+7", from
        # format_utc_to_gmt7) sorts correctly as plain text since every
        # component is zero-padded and the trailing "GMT+7" is constant.
        if column in ("elapsed", "import_time"):
            key = lambda d: d.get("import_time_iso") or d.get("import_time") or ""  # noqa: E731
            # smaller elapsed = more recent import_time, so its sort direction
            # is the opposite of a plain sort on the underlying timestamp
            reverse = not self.sort_reverse if column == "elapsed" else self.sort_reverse
            items.sort(key=key, reverse=reverse)
        elif column == "attempt":
            key = lambda d: d.get("attempt_count") or 0  # noqa: E731
            items.sort(key=key, reverse=self.sort_reverse)
        elif column == "complete_time":
            key = lambda d: d.get("complete_time_iso") or d.get("complete_time") or ""  # noqa: E731
            items.sort(key=key, reverse=self.sort_reverse)
        elif column == "repair_time":
            key = lambda d: d.get("repair_time_iso") or d.get("repair_time") or ""  # noqa: E731
            items.sort(key=key, reverse=self.sort_reverse)
        elif column == "mrb_time":
            key = lambda d: d.get("mrb_time_iso") or d.get("mrb_time") or ""  # noqa: E731
            items.sort(key=key, reverse=self.sort_reverse)
        elif column == "scan_time":
            key = lambda d: d.get("scan_time_iso") or d.get("scan_time") or ""  # noqa: E731
            items.sort(key=key, reverse=self.sort_reverse)
        elif column == "scanned_time":
            key = lambda d: d.get("scanned_at_iso") or ""  # noqa: E731
            items.sort(key=key, reverse=self.sort_reverse)
        elif column == "debug":
            key = lambda d: (d.get("debug_operator") or "").lower()  # noqa: E731
            items.sort(key=key, reverse=self.sort_reverse)
        else:
            key = lambda d: (d.get(column) or "").lower()  # noqa: E731
            items.sort(key=key, reverse=self.sort_reverse)

    def _set_busy(self, busy: bool, message: str = ""):
        state = "disabled" if busy else "normal"
        self.scan_entry.config(state=state)
        self.refresh_button.config(
            state=state if self.view_mode in ("buffer", "completed", "mrb", "qr_change") else "disabled"
        )
        self.reconnect_button.config(state=state)
        self._update_delete_button_state(busy=busy)
        if message:
            self.status_label.config(text=message)
        elif not busy:
            self.status_label.config(text="Ready.")
        if not busy:
            self.scan_entry.focus_set()

    # ---- permissions ----
    def _refresh_role_from_accounts(self):
        """Re-derive self.role from the live accounts list, so an owner/admin
        changing someone's role takes effect immediately without requiring
        that person to log out and back in."""
        if self.username == OWNER_USERNAME:
            self.role = "owner"
            return
        account = next((a for a in self.accounts if a.get("username") == self.username), None)
        self.role = account["role"] if account else "user"

    def _refresh_mode_permission_from_accounts(self):
        """Re-derive self.mode_permission from the live accounts list (same
        live-update pattern as _refresh_role_from_accounts), re-enable/
        disable the mode buttons to match, keep each mode's listeners in
        sync with what's actually allowed now (see
        _subscribe_mode_listeners/_unsubscribe_mode_listeners - a newly
        granted mode needs its data to actually start arriving, a newly
        revoked one should stop costing reads, not just become
        unreachable through the UI), and force-switch out of the current
        app_mode immediately if it's no longer permitted."""
        if self.username == OWNER_USERNAME:
            self.mode_permission = "both"
        else:
            account = next((a for a in self.accounts if a.get("username") == self.username), None)
            self.mode_permission = (account.get("mode_permission") or "both") if account else "both"
        allowed = self._allowed_modes()
        for m, btn in self.mode_buttons.items():
            btn.config(state="normal" if m in allowed else "disabled")
        # debug_io_queue_watch/ar_io_queue_watch is the "is this mode
        # currently subscribed at all" signal here - Buffer and Online
        # Repair have no watch object anymore (see _subscribe_mode_listeners),
        # Queue is the only one left with a permanent Firestore listener.
        for mode, watch_attr in (("debug", "debug_io_queue_watch"), ("assembly_rework", "ar_io_queue_watch")):
            currently_subscribed = getattr(self, watch_attr) is not None
            if mode in allowed and not currently_subscribed:
                self._subscribe_mode_listeners(mode)
            elif mode not in allowed and currently_subscribed:
                self._unsubscribe_mode_listeners(mode)
        if self.app_mode not in allowed:
            old_mode = self.app_mode
            self._set_app_mode("debug" if "debug" in allowed else next(iter(allowed)))
            self._show_toast(
                f"Your access to {old_mode.replace('_', ' ')} mode was removed - switched to {self.app_mode}.",
                kind="info",
            )

    def _can_view_reports(self) -> bool:
        """Completed/Chart/Summary are all on-demand fetches (see the
        on-demand fetch / Chart & Summary history sections) - restricting
        the "user" role from them isn't just UI declutter, it directly
        cuts Firestore reads too, since each is a real fetch triggered by
        opening the tab (Chart/Summary can trigger several: Completed's
        full collection plus, for a wide enough range, Online Repair
        history beyond the live window)."""
        return self.role != "user"

    def _refresh_report_access_from_role(self):
        """Re-enable/disable the Completed/Chart/Summary buttons to match
        the current role, and force-switch out of one immediately if the
        role changed while the user was sitting on it - same live-update
        pattern as _refresh_mode_permission_from_accounts."""
        can_view = self._can_view_reports()
        for mode in _REPORT_VIEW_MODES:
            btn = self.view_buttons.get(mode)
            if btn:
                btn.config(state="normal" if can_view else "disabled")
        if not can_view and self.view_mode in _REPORT_VIEW_MODES:
            old_mode = self.view_mode
            self._set_view_mode("online_repair")
            self._show_toast(
                f"Your access to {old_mode.replace('_', ' ')} was removed - switched to Online Repair.",
                kind="info",
            )

    def _can_delete(self) -> bool:
        return self.role in ("owner", "admin")

    def _update_delete_button_state(self, busy: bool = False):
        can_act = (
            not busy
            and self.view_mode in ("buffer", "completed", "online_repair", "mrb", "qr_change", "queue")
            and self._can_delete()
        )
        self.delete_button.config(state="normal" if can_act else "disabled")
        # Buffer has no permanent Firestore listener anymore (see
        # _subscribe_mode_listeners) - this is the only way to pull in
        # other operators' scans, so it's enabled independently of
        # _can_delete (every viewer needs it, not just editors) - same for
        # Online Repair, which has no permanent listener either now.
        self.load_button.config(
            state="normal" if not busy and self.view_mode in ("buffer", "online_repair") else "disabled"
        )

    # ---- scanning ----
    @staticmethod
    def _lerp_color(start_hex, end_hex, t):
        start = tuple(int(start_hex[i:i + 2], 16) for i in (1, 3, 5))
        end = tuple(int(end_hex[i:i + 2], 16) for i in (1, 3, 5))
        mixed = tuple(round(start[c] + (end[c] - start[c]) * t) for c in range(3))
        return f"#{mixed[0]:02x}{mixed[1]:02x}{mixed[2]:02x}"

    def _flash_row(self, qr, pulses=3, steps=10, step_ms=45):
        """Pulse a row's background like a glow (dim -> bright -> dim,
        repeated) to draw attention to it - used instead of an
        interrupting popup when a duplicate QR is scanned, so the operator
        can see the existing entry (and all its info) directly in the list
        rather than just being told it exists.
        """
        if not self.tree.exists(qr):
            return
        original_tags = self.tree.item(qr, "tags")
        base_bg = PANEL_BG if "oddrow" in original_tags else FIELD_BG
        glow_tag = f"glow_{qr}"

        ramp = [i / steps for i in range(steps + 1)]
        frames = (ramp + ramp[::-1]) * pulses

        def _reinsert(tags):
            # Re-tagging an existing row in place via .item(tags=...) can
            # silently fail to repaint until some unrelated click forces a
            # redraw (a ttk quirk on some Tk builds) - deleting and
            # reinserting the row is a structural change that always
            # repaints immediately, same as the existing sort/refresh code
            # already relies on elsewhere in this app.
            index = self.tree.index(qr)
            values = self.tree.item(qr, "values")
            self.tree.delete(qr)
            self.tree.insert("", index, iid=qr, tags=tags, values=values)

        def _draw(step):
            if not self.tree.exists(qr):
                return
            if step >= len(frames):
                _reinsert(original_tags)
                return
            t = frames[step]
            self.tree.tag_configure(
                glow_tag, background=self._lerp_color(base_bg, ACCENT, t),
                foreground=ACCENT_TEXT if t > 0.5 else TEXT,
            )
            _reinsert((glow_tag,))
            self.root.after(step_ms, lambda: _draw(step + 1))

        _draw(0)

    def _on_scan_submit(self, _event=None):
        qr = self.scan_entry.get().strip()
        self.scan_entry.delete(0, "end")
        if not qr:
            return
        if not _VALID_QR_RE.match(qr):
            # Catches Vietnamese input editors (Unikey etc.) mangling a
            # fast scanner "keystroke" burst into accented text - see
            # _VALID_QR_RE. Every scan path (keyboard/USB scanner, serial)
            # funnels through this one method, so this one check covers
            # both.
            self._show_toast(f"Invalid QR: \"{qr}\"", kind="error")
            self.scan_entry.focus_set()
            return
        if self.view_mode == "online_repair":
            # A QR still sitting in Buffer means this unit is being
            # confirmed fixed right now - complete it the same way a
            # passing Buffer Refresh would, instead of logging a separate,
            # unlinked Online Repair entry alongside an untouched Buffer
            # row (see _complete_buffer_from_online_repair).
            if any(d["qr"] == qr for d in self.devices):
                self._complete_buffer_from_online_repair(qr)
                self.scan_entry.focus_set()
                return
            # A genuine repeat repair (the same unit coming back a second
            # time) is legitimate and shouldn't be blocked forever just
            # because this QR was scanned once before - only treat it as
            # an accidental duplicate scan (select/flash the existing row
            # instead of logging a new one) if the most recent entry for
            # this QR is within the last 24h; older than that, log it as
            # a new repair like any other scan.
            matches = [r for r in self.online_repairs if r["qr"] == qr]
            latest = max(matches, key=lambda r: r.get("repair_time_iso") or "", default=None)
            latest_time = _parse_iso_to_gmt7(latest["repair_time_iso"]) if latest else None
            if latest_time is not None and (
                dt.datetime.now(dt.timezone.utc).astimezone(GMT7) - latest_time < dt.timedelta(hours=24)
            ):
                self.tree.selection_set(latest["_doc_id"])
                self.tree.see(latest["_doc_id"])
                self._flash_row(latest["_doc_id"])
                self.scan_entry.focus_set()
                return
            self._set_busy(True, f"Logging {qr}...")
            threading.Thread(target=self._do_online_repair_scan, args=(qr,), daemon=True).start()
            return
        if self.view_mode == "mrb":
            existing_mrb = next((d for d in self.mrb_devices if d["qr"] == qr), None)
            if existing_mrb is not None:
                self.tree.selection_set(qr)
                self.tree.see(qr)
                self._flash_row(qr)
                self.scan_entry.focus_set()
                return
            self._set_busy(True, f"Logging {qr} to MRB...")
            threading.Thread(target=self._do_mrb_scan, args=(qr,), daemon=True).start()
            return
        if self.view_mode == "qr_change":
            # Can't dedupe by the scanned value up front here the way the
            # other views do - the row's real key is the resolved pcb_qr,
            # not the paper QR just scanned, so that has to wait until the
            # lookup in _on_qr_change_scan_result comes back.
            self._set_busy(True, f"Checking {qr}...")
            threading.Thread(target=self._do_qr_change_scan, args=(qr,), daemon=True).start()
            return
        if any(d["qr"] == qr for d in self.devices):
            self._set_view_mode("buffer")
            self.tree.selection_set(qr)
            self.tree.see(qr)
            self._flash_row(qr)
            self.scan_entry.focus_set()
            return
        self._set_busy(True, f"Checking {qr}...")
        threading.Thread(target=self._do_scan, args=(qr,), daemon=True).start()

    # ---- serial QR scanner (e.g. /dev/ttyACM0 on Linux) ----
    def _refresh_serial_ports(self):
        ports = list_serial_ports()
        current = self.serial_port_combo.get()
        self.serial_port_combo["values"] = ports
        if current:
            self.serial_port_combo.set(current)
        elif ports:
            self.serial_port_combo.set(ports[0])

    def _restore_serial_scanner_state(self):
        """Called every time _build_main_frame runs (every login) - the
        SerialScanner connection itself outlives logout/login (see its
        note in __init__), so on a re-login this just needs to reflect
        that state in the freshly-rebuilt widgets, not reconnect. Only on
        the very first call in this app run (nothing connected yet) does
        it try the previously-remembered port automatically."""
        if self.serial_scanner is not None and self.serial_scanner.is_connected:
            self.serial_port_combo.set(self.serial_scanner.port_name)
            self.serial_connect_button.config(text="Disconnect")
            self.serial_status_label.config(text=f"Connected: {self.serial_scanner.port_name}", foreground=ACCENT)
            return
        try:
            saved_port = SERIAL_PORT_CACHE_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            saved_port = ""
        if saved_port:
            self.serial_port_combo.set(saved_port)
        if saved_port and not self._serial_autoconnect_attempted:
            self._serial_autoconnect_attempted = True
            self._connect_serial_scanner(saved_port, silent_on_failure=True)

    def _on_serial_scanner_toggle(self):
        if self.serial_scanner is not None and self.serial_scanner.is_connected:
            self.serial_scanner.stop()
            self.serial_scanner = None
            self.serial_connect_button.config(text="Connect")
            self.serial_status_label.config(text="Not connected", foreground=MUTED)
            return
        port = self.serial_port_combo.get().strip()
        if not port:
            self._show_toast("Choose or type a serial port first.", kind="error")
            return
        self._connect_serial_scanner(port, silent_on_failure=False)

    def _connect_serial_scanner(self, port: str, silent_on_failure: bool):
        scanner = SerialScanner(self.root, port, on_scan=self._on_serial_scan)
        try:
            scanner.start()
        except Exception as exc:  # noqa: BLE001 - bad/missing port, already in use, no permission, etc.
            if not silent_on_failure:
                self._show_toast(f"Could not open {port}: {exc}", kind="error")
            return
        self.serial_scanner = scanner
        self.serial_connect_button.config(text="Disconnect")
        self.serial_status_label.config(text=f"Connected: {port}", foreground=ACCENT)
        try:
            SERIAL_PORT_CACHE_FILE.write_text(port, encoding="utf-8")
        except OSError:  # noqa: BLE001 - cosmetic (just means next launch won't pre-fill it)
            pass

    def _on_serial_scan(self, qr):
        """A line arrived from the serial scanner - feed it through exactly
        the same path a manually-typed QR + Enter uses. Guarded against
        firing while logged out (main_frame/scan_entry don't exist then) -
        the SerialScanner connection outlives logout, but nothing can be
        scanned into a screen that isn't showing."""
        if self.main_frame is None:
            return
        self.scan_entry.delete(0, "end")
        self.scan_entry.insert(0, qr)
        self._on_scan_submit()

    @staticmethod
    def _lookup_fully_failed(result: dict) -> bool:
        """True if a scan result reflects _lookup_board exhausting every
        retry with nothing found (see webdb_client._lookup_board -
        lookup_diagnostics is only ever attached in that exact case)."""
        return bool(result.get("lookup_diagnostics"))

    def _relogin_webdb_locked(self) -> bool:
        """Re-authenticate against WebDB. Must be called with self.lock
        already held (it drives the browser directly).

        Reported symptom this exists for: on machines where the app stays
        open for a long shift, once one QR lookup fails, *every* QR after
        it fails too - until the app is restarted. That pattern doesn't
        match a one-off network blip (which would recover on its own for
        the next scan); it matches the WebDB login session silently
        expiring, after which every request built from that session's
        cookies fails identically, forever, until a fresh login happens.
        Restarting the app forces a fresh login as a side effect - this
        does the same re-login without needing the operator to restart.

        A plain re-login on the *same* driver isn't always enough, though -
        reported follow-up symptom: this would itself start reporting
        failure (both the automatic retry here and the manual Reconnect
        button), recovering only once the operator fully logged out and
        back in. That only differs from a plain re-login in one way: it
        builds a brand new Selenium/Chrome driver instead of reusing the
        current one - so a long-running headless session that's become
        wedged (crashed renderer, detached debugger, etc., not just an
        expired cookie) needs a new driver, not just a new login. This
        does that automatically as a fallback, so the operator shouldn't
        need to log out for this specific case anymore.

        Best-effort: returns whether it succeeded; if it fails, the
        caller's retry will just hit the same original symptom, no worse
        than before this existed.
        """
        try:
            if attempt_login(self.driver, BASE_URL, WEBDB_USERNAME, WEBDB_PASSWORD):
                return True
        except Exception:  # noqa: BLE001
            pass

        old_driver = self.driver
        try:
            new_driver = _build_and_login_driver()
        except Exception:  # noqa: BLE001
            return False
        self.driver = new_driver
        try:
            old_driver.quit()
        except Exception:  # noqa: BLE001 - already replaced; a failure quitting the old one is harmless
            pass
        return True

    def _on_manual_reconnect(self):
        """Manual fallback for _relogin_webdb_locked's automatic recovery -
        same mode-independent WebDB connection either way, so one button
        covers both Debug and Assembly Rework. The automatic re-login
        already fires on every scan that hits a total lookup failure, so
        this should rarely be needed, but gives the operator an explicit
        way to force it (e.g. before a long batch of scans) rather than
        waiting for one to fail first."""
        self._set_busy(True, "Reconnecting to WebDB...")
        threading.Thread(target=self._do_manual_reconnect, daemon=True).start()

    def _do_manual_reconnect(self):
        with self.lock:
            ok = self._relogin_webdb_locked()
        self.result_queue.put(("reconnect_result", ok))

    def _do_scan(self, qr):
        try:
            with self.lock:
                if self.app_mode == "assembly_rework":
                    session = build_api_session(self.driver)
                    result = check_assembly_rework_buffer(session, BASE_URL, qr)
                    if self._lookup_fully_failed(result) and self._relogin_webdb_locked():
                        session = build_api_session(self.driver)
                        result = check_assembly_rework_buffer(session, BASE_URL, qr)
                else:
                    result = check_device_prog_main(self.driver, BASE_URL, qr)
                    if self._lookup_fully_failed(result) and self._relogin_webdb_locked():
                        result = check_device_prog_main(self.driver, BASE_URL, qr)
        except Exception as exc:  # noqa: BLE001
            self.result_queue.put(("error", f"Error checking {qr}: {exc}"))
            return
        if self.app_mode == "assembly_rework":
            self.result_queue.put(("ar_scan_result", result))
        else:
            self.result_queue.put(("scan_result", result))

    def _show_toast(self, message: str, kind: str = "error", duration_ms: int = 3000):
        """Non-blocking notification that auto-dismisses - used for transient
        scan feedback (e.g. QR not found) that shouldn't interrupt the
        operator's workflow the way a modal messagebox would.
        """
        toast = tk.Toplevel(self.root)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        color = DANGER if kind == "error" else ACCENT

        border = tk.Frame(toast, bg=color)
        border.pack(padx=0, pady=0)
        inner = tk.Frame(border, bg=PANEL_BG)
        inner.pack(padx=1, pady=1)
        tk.Label(
            inner, text=message, bg=PANEL_BG, fg=TEXT, font=FONT_BASE,
            padx=18, pady=12, wraplength=420, justify="left",
        ).pack()

        toast.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - toast.winfo_width()) // 2
        y = self.root.winfo_rooty() + 80
        toast.geometry(f"+{x}+{y}")
        toast.after(duration_ms, toast.destroy)

    def _ask_ar_from_district(self, qr: str) -> str | None:
        """Modal chooser for Assembly Rework's From/district (see
        _AR_FROM_LABELS in webdb_client.py) when WebDB's own lookup found
        no current LongTest/TestRoom/QC failure for this QR (a blank
        from_label) - rather than silently recording "No info" (and, for
        Online Repair/MRB, skipping the Fact-Rework sheet write entirely,
        since that write needs a district), the operator says which line
        this actually came from. Test Room is folded into "From Long
        Test" the same way WebDB's own step data is (see
        _AR_FROM_LABELS) - there's no separate third option.

        Blocks (via wait_window) until the operator picks one or closes
        the dialog. Returns the chosen from_label string, or None if
        cancelled - callers must treat None as "don't record anything".
        """
        result = {"value": None}
        dialog = tk.Toplevel(self.root)
        dialog.title("Select From")
        dialog.configure(bg=BG)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text=f"{qr}: no current LongTest/QC failure found on WebDB.\nSelect where this actually came from:",
            style="Header.TLabel", wraplength=360, justify="left",
        ).pack(anchor="w", pady=(0, 16))

        def _choose(value):
            result["value"] = value
            dialog.destroy()

        button_row = ttk.Frame(frame)
        button_row.pack(fill="x")
        ttk.Button(
            button_row, text="QC", style="Accent.TButton", command=lambda: _choose("From QC"),
        ).pack(side="left", padx=(0, 8), fill="x", expand=True)
        ttk.Button(
            button_row, text="Long Test", style="Accent.TButton", command=lambda: _choose("From Long Test"),
        ).pack(side="left", fill="x", expand=True)
        ttk.Button(frame, text="Cancel", style="Secondary.TButton", command=dialog.destroy).pack(
            fill="x", pady=(10, 0)
        )

        dialog.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dialog.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dialog.winfo_height()) // 3
        dialog.geometry(f"+{x}+{y}")
        dialog.grab_set()
        dialog.focus_set()
        dialog.wait_window()
        return result["value"]

    def _log_lookup_failure_if_any(self, qr: str, result: dict, context: str) -> None:
        """If `result` carries a lookup_diagnostics list (see
        webdb_client._lookup_board), the WebDB board lookup came back empty
        after every retry - log it to Firestore so a failure on someone
        else's machine is visible somewhere everyone can check, not just
        whatever local console happened to be open when it occurred.
        Best-effort: fired in a background thread, never blocks or shows
        an error to the operator if the logging itself fails."""
        diagnostics = result.get("lookup_diagnostics")
        if not diagnostics:
            return
        record = {
            "qr": qr,
            "username": self.username,
            "app_mode": self.app_mode,
            "context": context,
            "time": dt.datetime.now(dt.timezone.utc).isoformat(),
            "attempts": diagnostics,
        }
        threading.Thread(target=log_lookup_failure, args=(self.db, record), daemon=True).start()

    def _write_prod5_row(self, write_fn, args, qr: str, sheet_label: str) -> None:
        """Run one PROD5 Google Sheet write (record_from_scan/record_mrb/
        record_line_activity/record_ar_buffer_scan/record_ar_fact_rework_event/
        record_completed, or an undo_* reversal) on a background thread,
        same as before - but unlike a bare `threading.Thread(target=write_fn,
        ...).start()`, an exception here no longer vanishes silently.

        write_fn itself already retries transient failures internally (see
        prod5_sheet._write_with_retry) - by the time an exception reaches
        here, retries are exhausted and it's a real, sheet is genuinely
        out-of-sync with the app. Since this app has no console window
        (built with console=False), an unhandled exception in a bare
        background thread previously had nowhere to go at all: no toast, no
        log, nothing - the exact gap that let a busy Online Repair session
        (e.g. scanning a whole repaired pallet back-to-back) silently lose
        rows without anyone noticing. Now it's both logged centrally (see
        firestore_client.log_sheet_write_failure, same precedent as
        log_lookup_failure for WebDB) and surfaced immediately as a toast.
        """

        def _run():
            try:
                write_fn(*args)
            except Exception as exc:  # noqa: BLE001 - exhausted write_fn's own retries, must not crash the thread
                failure = {
                    "qr": qr,
                    "username": self.username,
                    "app_mode": self.app_mode,
                    "sheet": sheet_label,
                    "time": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "error": f"{type(exc).__name__}: {exc}",
                }
                threading.Thread(target=log_sheet_write_failure, args=(self.db, failure), daemon=True).start()
                self.result_queue.put(("sheet_write_failed", {"qr": qr, "sheet": sheet_label}))

        threading.Thread(target=_run, daemon=True).start()

    def _check_and_remove_from_queue(self, qr) -> bool:
        """True if `qr` was sitting in this app_mode's department Queue
        (see self.io_queue) - checked against the already-live-synced
        local list instead of a fresh Firestore query, both for speed and
        to avoid paying for a lookup on every single Buffer/Online Repair
        scan regardless of whether that QR was ever queued at all. If
        matched, removes it immediately from the local list (instant UI
        feedback) and fires the matching Firestore delete in the
        background - if not, this is a no-op and the caller is expected
        to surface an inline warning (see _show_toast) without blocking
        the scan itself."""
        matched = next((q for q in self.io_queue if q["qr"] == qr), None)
        if matched is None:
            return False
        self.io_queue = [q for q in self.io_queue if q["_doc_id"] != matched["_doc_id"]]
        self.checked_io_queue_ids.discard(matched["_doc_id"])
        self._update_queue_counter_label()
        if self.view_mode == "queue":
            self._apply_current_sort()
            self._refresh_tree()
        threading.Thread(target=remove_io_queue_entries, args=(self.db, qr), daemon=True).start()
        return True

    def _maybe_flag_permanent_repair_for_qr_change(self, qr, info):
        """If the latest Debug-mode Prog Main check for this QR shows
        WebDB's own "permanent repairs found" gate (see
        webdb_client._resolve_prog_main_defect), the PCB has effectively
        been permanently repaired/its paper QR reassigned - track it in
        QR Change too, same as a manual QR Change scan would, reusing the
        pcb_qr this same lookup already returned (no extra
        fetch_pcb_qr_info round-trip). Assembly Rework's own lookups
        (check_assembly_rework_buffer/_repaired) never set this field, so
        this is naturally a no-op there."""
        if not info.get("permanent_repairs_found"):
            return
        pcb_qr = info.get("pcb_qr")
        if not pcb_qr:
            return
        if any(r["pcb_qr"] == pcb_qr for r in self.qr_changes):
            return
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        record = {
            "pcb_qr": pcb_qr,
            "previous_qr": qr,
            "current_qr": "",
            "model": resolve_board_name(self.pcb_choice_rules, info.get("board_name"), info.get("region")) or "No info",
            "scan_time": format_utc_to_gmt7(now_iso),
            "scan_time_iso": now_iso,
        }
        self.qr_changes = self.qr_changes + [record]
        if self.view_mode == "qr_change":
            self._apply_current_sort()
            self._refresh_tree()
        self._write_and_sync_row(
            "qr_change", add_qr_change, (self.db, record),
            sync_qr_change_upserted, (record,), pcb_qr, "qr_change",
        )

    def _on_scan_result(self, result):
        self._set_busy(False)
        qr = result["qr"]
        self._log_lookup_failure_if_any(qr, result, "buffer_scan")
        if self._lookup_fully_failed(result):
            # The board lookup itself came up empty even after the
            # automatic re-login-and-retry in _do_scan - not a genuine
            # "not found", just WebDB being unreachable/slow right now.
            # Never add a "No info" placeholder to the list or sheet for
            # this - the operator should reconnect and scan it again.
            self._show_toast(f"{qr}: reconnect to WebDB and scan again", kind="error")
            self._on_manual_reconnect()
            return
        if result.get("error"):
            self._show_toast(f"{qr}: {result['error']}")
            return
        if result["passed_last_attempt"]:
            step_label = result.get("step_label") or "Prog Main"
            messagebox.showinfo(
                "Passed",
                f"Device {qr} PASSED {step_label} on its most recent attempt "
                f"({format_utc_to_gmt7(result['last_time'])}).\nNot added to the list.",
            )
            return
        resolved_board = resolve_board_name(
            self.pcb_choice_rules, result.get("board_name"), result.get("region")
        )
        if not resolved_board:
            # Never add a "No info" Board to Buffer - an unresolved board
            # name here means the lookup didn't actually come back with
            # anything usable, even though it wasn't flagged as a full
            # lookup failure above - reconnecting and rescanning is the
            # only safe recovery, same as that case.
            self._show_toast(f"{qr}: reconnect to WebDB and scan again", kind="error")
            self._on_manual_reconnect()
            return
        device = {
            "qr": qr,
            "board": resolved_board,
            "board_name": result.get("board_name"),
            "import_time": format_utc_to_gmt7(result["first_fail_time"]),
            "import_time_iso": result["first_fail_time"],
            "attempt_count": result.get("attempt_count"),
            "defect": result["defect_description"] or "No info",
            "color": result.get("color"),
        }
        threading.Thread(
            target=add_device, args=(self.db, device, self._devices_collection), daemon=True
        ).start()
        if self.sheet:
            self._write_prod5_row(sync_device_upserted, (self.sheet, device), qr, "buffer")
        # Buffer has no permanent Firestore listener anymore (see
        # _subscribe_mode_listeners) - show this scan immediately via
        # optimistic local update; other operators' scans need a Load
        # click to appear (see _on_load_buffer_click).
        self.devices = self.devices + [device]
        if self.view_mode == "buffer":
            self._apply_current_sort()
            self._refresh_tree()
        self._maybe_flag_permanent_repair_for_qr_change(qr, result)
        if not self._check_and_remove_from_queue(qr):
            self._show_toast(f"{qr}: not found in queue", kind="error")
        # This FROM sheet is Debug's own PROD5 VTP spreadsheet (categorized
        # by PCB debug rules) - Assembly Rework writes to its own separate
        # sheet instead (see _on_ar_scan_result), never to this one.
        if self.app_mode == "debug" and self.from_sheet and not should_skip_prod5_sheet(result.get("board_name")):
            self._write_prod5_row(
                record_from_scan, (self.from_sheet, device["board"], result.get("color")), qr, "FROM"
            )

    def _on_ar_scan_result(self, result):
        """Assembly Rework's buffer-income result handler - counterpart to
        _on_scan_result, but for check_assembly_rework_buffer's very
        different result shape (from_label instead of pass/fail + defect,
        manufacturing_name/pro_account_name/spec instead of board_name)."""
        self._set_busy(False)
        qr = result["qr"]
        self._log_lookup_failure_if_any(qr, result, "assembly_rework_buffer_scan")
        if self._lookup_fully_failed(result):
            self._show_toast(f"{qr}: reconnect to WebDB and scan again", kind="error")
            self._on_manual_reconnect()
            return
        if result.get("error"):
            self._show_toast(f"{qr}: {result['error']}")
            return
        from_label = result.get("from_label")
        if not from_label:
            from_label = self._ask_ar_from_district(qr)
            if from_label is None:
                return
        board_text = resolve_device_choice(
            self.device_choice_rules, result.get("manufacturing_name"), result.get("pro_account_name"),
            result.get("spec"), result.get("region"), result.get("color"),
        )
        if not board_text and not result.get("manufacturing_name"):
            # Never add a "No info" Device to Buffer - see _on_scan_result's
            # matching guard for Debug mode's Board field. This is distinct
            # from the "no matching Device choice rule" case below (which
            # still has a real manufacturing_name to fall back to display).
            self._show_toast(f"{qr}: reconnect to WebDB and scan again", kind="error")
            self._on_manual_reconnect()
            return
        device = {
            "qr": qr,
            "board": board_text or result.get("manufacturing_name"),
            "import_time": format_utc_to_gmt7(result.get("failed_step_time")),
            "import_time_iso": result.get("failed_step_time"),
            "defect": from_label,
        }
        threading.Thread(
            target=add_device, args=(self.db, device, self._devices_collection), daemon=True
        ).start()
        if self.sheet:
            self._write_prod5_row(sync_device_upserted, (self.sheet, device), qr, "buffer")
        # See _on_scan_result's matching comment - no permanent listener,
        # optimistic local update instead.
        self.devices = self.devices + [device]
        if self.view_mode == "buffer":
            self._apply_current_sort()
            self._refresh_tree()
        in_queue = self._check_and_remove_from_queue(qr)
        queue_note = "" if in_queue else " Not found in queue."
        if not board_text:
            # No safe value to put in the Assembly Rework PROD5 VTP FROM
            # sheet's strictly-validated Board dropdown - the device still
            # shows up in Buffer/DRD Buffer Tracking above, just not there.
            self._show_toast(
                f"{qr}: added to Buffer, but no matching Device choice rule for "
                f"{result.get('manufacturing_name')} / {result.get('region')} / {result.get('color')} "
                f"- FROM sheet not updated.{queue_note}",
                kind="error",
            )
        else:
            if self.ar_prod5_from_sheet:
                self._write_prod5_row(
                    record_ar_buffer_scan, (self.ar_prod5_from_sheet, from_label, board_text), qr, "FROM"
                )
            if not in_queue:
                self._show_toast(f"{qr}: not found in queue", kind="error")

    # ---- online repair scanning ----
    def _do_online_repair_scan(self, qr):
        # Online Repair trusts the scan unconditionally (the operator just
        # fixed it) - no Prog Main pass/fail *gating*, just a best-effort
        # lookup so the log has a meaningful Board/Defect column. Debug
        # still checks Prog Main here (same as Buffer) purely to read its
        # defect_description for display - a device that fails still gets
        # logged as repaired either way. Assembly Rework wants to know
        # which of LongTest/TestRoom/QC is (or was) failing, for the same
        # reason - check_assembly_rework_buffer already computes both the
        # board and that in one call, so it's reused here as a richer
        # lookup, not because Online Repair cares about pass/fail gating.
        info = {"qr": qr, "board_name": None, "region": None, "color": None}
        try:
            with self.lock:
                if self.app_mode == "assembly_rework":
                    session = build_api_session(self.driver)
                    info = check_assembly_rework_buffer(session, BASE_URL, qr)
                    if self._lookup_fully_failed(info) and self._relogin_webdb_locked():
                        session = build_api_session(self.driver)
                        info = check_assembly_rework_buffer(session, BASE_URL, qr)
                else:
                    info = check_device_prog_main(self.driver, BASE_URL, qr)
                    if self._lookup_fully_failed(info) and self._relogin_webdb_locked():
                        info = check_device_prog_main(self.driver, BASE_URL, qr)
        except Exception:  # noqa: BLE001 - lookup is best-effort, still log the repair
            pass
        self.result_queue.put(("online_repair_scan_result", info))

    def _on_online_repair_scan_result(self, info):
        self._set_busy(False)
        qr = info["qr"]
        self._log_lookup_failure_if_any(qr, info, "online_repair_scan")
        if self._lookup_fully_failed(info):
            # Unlike a genuine "no history" result, the board lookup itself
            # came up empty even after the automatic re-login-and-retry -
            # don't log a "No info" repair event for this, the operator
            # should reconnect and scan it again.
            self._show_toast(f"{qr}: reconnect to WebDB and scan again", kind="error")
            self._on_manual_reconnect()
            return
        is_ar = self.app_mode == "assembly_rework"
        ar_from_label = None
        if is_ar:
            resolved_board = resolve_device_choice(
                self.device_choice_rules, info.get("manufacturing_name"), info.get("pro_account_name"),
                info.get("spec"), info.get("region"), info.get("color"),
            ) or info.get("manufacturing_name")
        else:
            resolved_board = resolve_board_name(
                self.pcb_choice_rules, info.get("board_name"), info.get("region")
            )
        if not resolved_board:
            # Never log a "No info" repair event to ANY list - a blank
            # resolved_board here (in either mode) means the lookup didn't
            # actually come back with anything usable, even though it
            # wasn't flagged as a full lookup failure above - reconnecting
            # and rescanning is the only safe recovery.
            self._show_toast(f"{qr}: reconnect to WebDB and scan again", kind="error")
            self._on_manual_reconnect()
            return
        if is_ar:
            # A blank from_label means WebDB shows no current LongTest/
            # TestRoom/QC failure - rather than logging "No info" (and
            # silently skipping the Fact-Rework sheet write below, since
            # that needs a district), ask the operator which line this
            # actually came from.
            ar_from_label = info.get("from_label")
            if not ar_from_label:
                ar_from_label = self._ask_ar_from_district(qr)
                if ar_from_label is None:
                    self._show_toast(f"{qr}: repair not logged - no From selected.", kind="error")
                    return
            defect = ar_from_label
        else:
            defect = info.get("defect_description") or "No info"
            self._maybe_flag_permanent_repair_for_qr_change(qr, info)
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        record = {
            "qr": qr,
            "board": resolved_board,
            "repair_time": format_utc_to_gmt7(now_iso),
            "repair_time_iso": now_iso,
            "debug_operator": self.username,
            "defect": defect,
            "color": info.get("color"),
        }
        threading.Thread(
            target=add_online_repair, args=(self.db, record, self._online_repair_collection), daemon=True
        ).start()
        online_repair_sheet = self.online_repair_sheet
        if online_repair_sheet:
            self._write_prod5_row(sync_online_repair_added, (online_repair_sheet, record), qr, "online_repair")
        # No permanent Firestore listener anymore (see
        # _subscribe_mode_listeners) - show this scan immediately via
        # optimistic local update; other operators' scans need a Load
        # click to appear (see _on_load_buffer_click, now shared with
        # Online Repair too).
        self.online_repairs = self.online_repairs + [{**record, "_doc_id": f"local{len(self.online_repairs)}"}]
        if self.view_mode == "online_repair":
            self._apply_current_sort()
            self._refresh_tree()
        # This unit may have been sitting in this app_mode's department
        # Queue - Online Repair is one of the two places that "picks it
        # back up" (Buffer scan-in is the other, see _on_scan_result/
        # _on_ar_scan_result), so clear it out if it was there. Never a
        # gate on logging the repair itself - just surfaced as a warning
        # below when it wasn't found.
        in_queue = self._check_and_remove_from_queue(qr)
        if is_ar:
            if self.ar_fact_rework_sheet and ar_from_label:
                operator = self.operator_names.get(self.username, self.username)
                district_to = "QC" if "QC" in ar_from_label else "LONG"
                self._write_prod5_row(
                    record_ar_fact_rework_event,
                    (self.ar_fact_rework_sheet, district_to, "Online repair", operator, record["board"]),
                    qr, "Fact-Rework",
                )
        elif self.fact_dr_sheet and not should_skip_prod5_sheet(info.get("board_name")):
            operator = self.operator_names.get(self.username, self.username)
            self._write_prod5_row(
                record_line_activity,
                (self.fact_dr_sheet, record["board"], info.get("color"), operator),
                qr, "Fact-D&R",
            )
        queue_note = "" if in_queue else " Not found in queue."
        self._show_toast(f"{qr}: repair logged.{queue_note}", kind="info" if in_queue else "error")

    # ---- MRB (scrap) scanning ----
    def _do_mrb_scan(self, qr):
        # MRB trusts the scan unconditionally too, same reasoning as
        # Online Repair - it's a manual "scrap this unit" decision, not
        # gated on pass/fail. The lookup is only best-effort, purely to
        # get a meaningful Board/Defect for the MRB log.
        info = {"qr": qr, "board_name": None, "region": None, "color": None}
        try:
            with self.lock:
                if self.app_mode == "assembly_rework":
                    session = build_api_session(self.driver)
                    info = check_assembly_rework_buffer(session, BASE_URL, qr)
                    if self._lookup_fully_failed(info) and self._relogin_webdb_locked():
                        session = build_api_session(self.driver)
                        info = check_assembly_rework_buffer(session, BASE_URL, qr)
                else:
                    info = check_device_prog_main(self.driver, BASE_URL, qr)
                    if self._lookup_fully_failed(info) and self._relogin_webdb_locked():
                        info = check_device_prog_main(self.driver, BASE_URL, qr)
        except Exception:  # noqa: BLE001 - lookup is best-effort, still log the MRB event
            pass
        self.result_queue.put(("mrb_scan_result", info))

    def _on_mrb_scan_result(self, info):
        self._set_busy(False)
        qr = info["qr"]
        self._log_lookup_failure_if_any(qr, info, "mrb_scan")
        if self._lookup_fully_failed(info):
            # Same reasoning as Online Repair - never log a "No info" MRB
            # event (and never touch Buffer) off a lookup that never
            # actually resolved this QR, even after the automatic
            # re-login-and-retry.
            self._show_toast(f"{qr}: reconnect to WebDB and scan again", kind="error")
            self._on_manual_reconnect()
            return
        is_ar = self.app_mode == "assembly_rework"
        if is_ar:
            resolved_board = resolve_device_choice(
                self.device_choice_rules, info.get("manufacturing_name"), info.get("pro_account_name"),
                info.get("spec"), info.get("region"), info.get("color"),
            ) or info.get("manufacturing_name")
        else:
            resolved_board = resolve_board_name(
                self.pcb_choice_rules, info.get("board_name"), info.get("region")
            )
        if not resolved_board:
            # Never log a "No info" MRB event to ANY list - see the matching
            # guard in _on_online_repair_scan_result.
            self._show_toast(f"{qr}: reconnect to WebDB and scan again", kind="error")
            self._on_manual_reconnect()
            return
        if is_ar:
            # A blank from_label means WebDB shows no current LongTest/
            # TestRoom/QC failure - ask the operator which line this
            # actually came from instead of logging "No info".
            defect = info.get("from_label")
            if not defect:
                defect = self._ask_ar_from_district(qr)
                if defect is None:
                    self._show_toast(f"{qr}: MRB not logged - no From selected.", kind="error")
                    return
        else:
            defect = info.get("defect_description") or "No info"
            self._maybe_flag_permanent_repair_for_qr_change(qr, info)
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        record = {
            "qr": qr,
            "board": resolved_board,
            "mrb_time": format_utc_to_gmt7(now_iso),
            "mrb_time_iso": now_iso,
            "debug_operator": self.username,
            "defect": defect,
            "color": info.get("color"),
        }
        # MRB isn't kept on a live listener anymore (see the on-demand
        # fetch section) - update the local list optimistically so this
        # scan shows up immediately for the operator doing the scanning,
        # then _write_and_sync_row writes Firestore and syncs this one row
        # to the sheet in the background.
        # The MRB log itself is recorded either way - only the PROD5 sheet
        # write and the Buffer removal below are conditional on this QR
        # actually being a buffered item right now.
        self.mrb_devices = [d for d in self.mrb_devices if d["qr"] != qr] + [record]
        if self.view_mode == "mrb":
            self._apply_current_sort()
            self._refresh_tree()
        self._write_and_sync_row(
            "mrb", add_mrb_device, (self.db, record, self._mrb_collection),
            sync_mrb_upserted, (record,), qr, "mrb",
        )
        # MRB is a terminal state reached independently of Completed, so
        # this never routes through add_completed_device - but the PROD5
        # sheet write (Fact-D&R_ok/Fact-Rework) tracks units actually
        # moving OUT of the buffer via MRB, so it (and the Buffer removal
        # itself) only happens when this QR is genuinely sitting in Buffer
        # right now. A QR that was never buffered still gets logged to the
        # MRB list above, just without touching Buffer or the PROD5 sheet.
        if any(d["qr"] == qr for d in self.devices):
            threading.Thread(
                target=delete_devices, args=(self.db, [qr], self._devices_collection), daemon=True
            ).start()
            if self.sheet:
                self._write_prod5_row(sync_device_removed, (self.sheet, qr), qr, "buffer")
            self.devices = [d for d in self.devices if d["qr"] != qr]
            self.checked_qrs.discard(qr)
            if self.view_mode == "buffer" and self.tree.exists(qr):
                self.tree.delete(qr)
            self._refresh_summary()
            if is_ar:
                if self.ar_fact_rework_sheet:
                    operator = self.operator_names.get(self.username, self.username)
                    self._write_prod5_row(
                        record_ar_fact_rework_event,
                        (self.ar_fact_rework_sheet, "MRB", "Repair from buffer", operator, record["board"]),
                        qr, "Fact-Rework",
                    )
            elif self.fact_ok_sheet and not should_skip_prod5_sheet(info.get("board_name")):
                self._write_prod5_row(
                    record_mrb, (self.fact_ok_sheet, record["board"], info.get("color")), qr, "Fact-D&R_ok"
                )
        if is_ar and not resolved_board:
            self._show_toast(
                f"{qr}: sent to MRB, but no matching Device choice rule for "
                f"{info.get('manufacturing_name')} / {info.get('region')} / {info.get('color')}",
                kind="error",
            )
        else:
            self._show_toast(f"{qr}: sent to MRB", kind="info")

    # ---- QR change tracking (paper QR reassignment) ----
    def _do_qr_change_scan(self, qr):
        """Resolve the scanned paper QR to its permanent PCB QR - same
        automatic re-login-and-retry pattern as every other scan flow here
        (see _relogin_webdb_locked)."""
        diagnostics = []
        pcb_qr = None
        model = None
        try:
            with self.lock:
                session = build_api_session(self.driver)
                pcb_qr, model = fetch_pcb_qr_info(session, BASE_URL, qr, diagnostics)
                if pcb_qr is None and diagnostics and self._relogin_webdb_locked():
                    diagnostics = []
                    session = build_api_session(self.driver)
                    pcb_qr, model = fetch_pcb_qr_info(session, BASE_URL, qr, diagnostics)
        except Exception as exc:  # noqa: BLE001
            self.result_queue.put(("error", f"Error checking {qr}: {exc}"))
            return
        # Only attach lookup_diagnostics when the lookup actually failed
        # (pcb_qr is None) - same "diag_extra" convention every other
        # webdb_client caller uses (see check_device_prog_main_session).
        # Missing this was the actual bug: fetch_pcb_qr_info's internal
        # component-then-dev fallback logs a "component: 404" diagnostics
        # entry on EVERY successful device-type lookup (component 404s
        # first, then dev succeeds - completely normal), so attaching
        # diagnostics unconditionally made _lookup_fully_failed report
        # "please try again" on every single scan, success or not.
        self.result_queue.put((
            "qr_change_scan_result",
            {
                "qr": qr, "pcb_qr": pcb_qr, "model": model,
                "lookup_diagnostics": diagnostics if pcb_qr is None else None,
            },
        ))

    def _on_qr_change_scan_result(self, result):
        self._set_busy(False)
        qr = result["qr"]
        self._log_lookup_failure_if_any(qr, result, "qr_change_scan")
        if self._lookup_fully_failed(result):
            self._show_toast(f"{qr}: please try again", kind="error")
            return
        pcb_qr = result.get("pcb_qr")
        if not pcb_qr:
            self._show_toast(f"{qr}: no PCB QR found for this device.", kind="error")
            return
        existing = next((r for r in self.qr_changes if r["pcb_qr"] == pcb_qr), None)
        if existing is not None:
            self.tree.selection_set(pcb_qr)
            self.tree.see(pcb_qr)
            self._flash_row(pcb_qr)
            return
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        record = {
            "pcb_qr": pcb_qr,
            "previous_qr": qr,
            "current_qr": "",
            "model": result.get("model") or "No info",
            "scan_time": format_utc_to_gmt7(now_iso),
            "scan_time_iso": now_iso,
        }
        # QR Change isn't kept on a live listener anymore (see the
        # on-demand fetch section) - update the local list optimistically
        # so this scan shows up immediately, then _write_and_sync_row
        # writes Firestore and syncs this one row to the sheet.
        self.qr_changes = self.qr_changes + [record]
        self._apply_current_sort()
        self._refresh_tree()
        self._write_and_sync_row(
            "qr_change", add_qr_change, (self.db, record),
            sync_qr_change_upserted, (record,), pcb_qr, "qr_change",
        )
        self._show_toast(f"{qr}: added to QR change list (PCB QR {pcb_qr})", kind="info")

    # ---- manual delete ----
    def _on_delete_selected(self, _event=None):
        if not self._can_delete():
            self._show_toast("You don't have permission to delete.", kind="error")
            return
        if self.view_mode == "buffer":
            checked_set, delete_fn, collection = self.checked_qrs, delete_devices, self._devices_collection
        elif self.view_mode == "completed":
            checked_set, delete_fn, collection = (
                self.checked_completed_qrs, delete_completed_devices, self._completed_collection
            )
        elif self.view_mode == "online_repair":
            checked_set, delete_fn, collection = (
                self.checked_online_repair_ids, delete_online_repairs, self._online_repair_collection
            )
        elif self.view_mode == "mrb":
            checked_set, delete_fn, collection = (
                self.checked_mrb_qrs, delete_mrb_devices, self._mrb_collection
            )
        elif self.view_mode == "qr_change":
            checked_set, delete_fn, collection = (
                self.checked_qr_change_ids, delete_qr_changes, QR_CHANGES_COLLECTION
            )
        elif self.view_mode == "queue":
            checked_set, delete_fn, collection = (
                self.checked_io_queue_ids, delete_io_queue_entries, IO_QUEUE_COLLECTION
            )
        else:
            return

        selected_ids = set(checked_set)
        if not selected_ids:
            self._show_toast("No devices checked to delete.", kind="info")
            return
        checked_set -= selected_ids
        self._set_busy(True, f"Deleting {len(selected_ids)} device(s)...")
        # Captured before the optimistic local-list filtering below removes
        # them - an admin/owner deleting a Buffer/Online Repair item that's
        # still within the current day+shift also undoes its Qty on the
        # matching PROD5 sheet (see _undo_sheet_entries_for_delete).
        removed_items = []
        # Remove from view immediately instead of waiting on the Firestore
        # round-trip - otherwise the rows stayed visible until the next
        # snapshot arrived, which looked like the delete had silently failed
        # and prompted a confusing second delete attempt (which then only
        # acted on whatever single row the Treeview still had selected).
        # (qr-for-logging, sheet_sync_args) pairs, built consistently per
        # branch so a later zip/index mismatch can't pair the wrong qr with
        # the wrong sheet-delete args (online_repair's removed_items isn't
        # in the same order as selected_ids, unlike the qr-keyed modes).
        sheet_delete_fn, sheet_deletes = None, []
        if self.view_mode == "buffer":
            removed_items = [d for d in self.devices if d["qr"] in selected_ids]
            self.devices = [d for d in self.devices if d["qr"] not in selected_ids]
            self._refresh_summary()
            sheet_delete_fn = sync_device_removed
            sheet_deletes = [(qr, (qr,)) for qr in selected_ids]
        elif self.view_mode == "completed":
            self.completed_devices = [d for d in self.completed_devices if d["qr"] not in selected_ids]
            sheet_delete_fn = sync_completed_removed
            sheet_deletes = [(qr, (qr,)) for qr in selected_ids]
        elif self.view_mode == "mrb":
            self.mrb_devices = [d for d in self.mrb_devices if d["qr"] not in selected_ids]
            sheet_delete_fn = sync_mrb_removed
            sheet_deletes = [(qr, (qr,)) for qr in selected_ids]
        elif self.view_mode == "qr_change":
            self.qr_changes = [d for d in self.qr_changes if d["pcb_qr"] not in selected_ids]
            sheet_delete_fn = sync_qr_change_removed
            sheet_deletes = [(pcb_qr, (pcb_qr,)) for pcb_qr in selected_ids]
        elif self.view_mode == "queue":
            self.io_queue = [d for d in self.io_queue if d["_doc_id"] not in selected_ids]
            self._update_queue_counter_label()
        else:
            removed_items = [d for d in self.online_repairs if d["_doc_id"] in selected_ids]
            self.online_repairs = [d for d in self.online_repairs if d["_doc_id"] not in selected_ids]
            sheet_delete_fn = sync_online_repair_removed
            sheet_deletes = [
                (item["qr"], (item["qr"], item.get("repair_time", ""))) for item in removed_items
            ]
        for row_id in selected_ids:
            if self.tree.exists(row_id):
                self.tree.delete(row_id)
        threading.Thread(
            target=self._do_delete, args=(list(selected_ids), delete_fn, collection), daemon=True
        ).start()
        sheet = self.sheet if self.view_mode == "buffer" else self._on_demand_sheet(self.view_mode)
        if sheet is None and self.view_mode == "online_repair":
            sheet = self.online_repair_sheet
        if sheet is not None and sheet_delete_fn is not None:
            for qr_for_log, args in sheet_deletes:
                self._write_prod5_row(sheet_delete_fn, (sheet, *args), qr_for_log, self.view_mode)
        if removed_items:
            threading.Thread(
                target=self._undo_sheet_entries_for_delete,
                args=(self.view_mode, self.app_mode, removed_items), daemon=True,
            ).start()

    def _do_delete(self, qrs, delete_fn, collection):
        try:
            delete_fn(self.db, qrs, collection)
        except Exception as exc:  # noqa: BLE001
            self.result_queue.put(("error", f"Failed to delete {len(qrs)} device(s): {exc}"))
            return
        self.result_queue.put(("delete_done", None))

    def _undo_sheet_entries_for_delete(self, view_mode, app_mode, removed_items):
        """Best-effort companion to _do_delete: when a deleted Buffer/Online
        Repair item's own scan timestamp still falls in the CURRENT
        day+shift, remove its matching Qty-1 from the relevant PROD5 sheet
        too (or delete/blank that line if Qty would hit 0) - so the sheet's
        running count doesn't silently drift from what the app still shows.
        Items from an earlier day+shift are left untouched entirely, since
        their sheet line belongs to a period that's already closed out.
        Runs in its own thread, after Firestore deletion has already been
        kicked off - a failure here must never block or roll back the
        delete itself, so every per-item error is swallowed."""
        now_gmt7 = dt.datetime.now(dt.timezone.utc).astimezone(GMT7)
        expected_shift, expected_period = shift_and_period(now_gmt7)
        for item in removed_items:
            try:
                if view_mode == "buffer":
                    item_local = _parse_iso_to_gmt7(item.get("import_time_iso"))
                    if item_local is None:
                        continue
                    if shift_and_period(item_local) != (expected_shift, expected_period):
                        continue
                    if app_mode == "assembly_rework":
                        if self.ar_prod5_from_sheet:
                            undo_ar_buffer_scan(
                                self.ar_prod5_from_sheet, item.get("defect"), item.get("board"),
                                expected_period, expected_shift,
                            )
                    elif self.from_sheet:
                        undo_from_scan(
                            self.from_sheet, item.get("board"), item.get("color"),
                            expected_period, expected_shift,
                        )
                elif view_mode == "online_repair":
                    item_local = _parse_iso_to_gmt7(item.get("repair_time_iso"))
                    if item_local is None:
                        continue
                    if shift_and_period(item_local) != (expected_shift, expected_period):
                        continue
                    raw_operator = item.get("debug_operator")
                    operator = self.operator_names.get(raw_operator, raw_operator)
                    if app_mode == "assembly_rework":
                        from_label = item.get("defect")
                        if self.ar_fact_rework_sheet and from_label:
                            district_to = "QC" if "QC" in from_label else "LONG"
                            undo_ar_online_repair(
                                self.ar_fact_rework_sheet, district_to, operator, item.get("board"),
                                expected_period, expected_shift,
                            )
                    elif self.fact_dr_sheet:
                        undo_line_activity(
                            self.fact_dr_sheet, item.get("board"), item.get("color"), operator,
                            expected_period, expected_shift,
                        )
            except Exception as exc:  # noqa: BLE001 - best-effort, must never block/undo the delete itself
                # Previously swallowed with no trace at all - now at least
                # logged centrally (same precedent as log_lookup_failure/
                # log_sheet_write_failure elsewhere), even though this path
                # still deliberately never surfaces a toast - undoing a
                # sheet row on delete is a much rarer, lower-stakes path
                # than the scan-time writes that motivated that toast.
                try:
                    log_sheet_write_failure(self.db, {
                        "qr": item.get("qr"), "username": self.username, "app_mode": app_mode,
                        "sheet": "undo", "time": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                except Exception:  # noqa: BLE001 - logging the failure must never itself raise
                    pass
                continue

    # ---- on-demand fetch (Completed / MRB / QR Change) ----
    # These three views are not used every day (unlike Buffer and Online
    # Repair, which stay on permanent Firestore listeners set up at
    # login), so keeping a live on_snapshot listener running for them the
    # whole session would mean paying a full-collection read on every
    # single write to every connected client's machine, forever, whether
    # or not anyone actually has that tab open. Instead, each is fetched
    # once on demand - when its tab is opened, when switching Debug/
    # Assembly Rework mode while it's showing (Completed/MRB only - QR
    # Change is a single shared collection, not mode-split), and via its
    # own Refresh button - and the Google Sheet mirror + local list are
    # both refreshed from that same read right after any write.
    def _on_demand_collection(self, mode):
        if mode == "completed":
            return self._completed_collection
        if mode == "mrb":
            return self._mrb_collection
        if mode == "qr_change":
            return QR_CHANGES_COLLECTION
        return None

    def _on_demand_sheet(self, mode):
        return {"completed": self.completed_sheet, "mrb": self.mrb_sheet, "qr_change": self.qr_change_sheet}.get(mode)

    def _on_demand_read_fn(self, mode):
        return {
            "completed": read_completed_from_sheet, "mrb": read_mrb_from_sheet, "qr_change": read_qr_changes_from_sheet,
        }.get(mode)

    def _fetch_on_demand(self, mode):
        """Load the current Completed/MRB/QR Change list from its Google
        Sheet mirror instead of a Firestore list_devices() read - same
        "read via the sheet, not Firestore" move as Buffer's Load button
        (see _load_buffer_from_sheet), just triggered by opening the tab
        (or Refresh) here instead of a dedicated button, since these views
        were already on-demand rather than live-listened."""
        sheet = self._on_demand_sheet(mode)
        read_fn = self._on_demand_read_fn(mode)
        if sheet is None or read_fn is None:
            return
        app_mode_at_request = self.app_mode
        self._set_busy(True, "Loading...")

        def _run():
            try:
                items = read_fn(sheet)
            except Exception:
                items = None
            self.result_queue.put((
                "on_demand_fetch_result",
                {"mode": mode, "app_mode": app_mode_at_request, "items": items},
            ))

        threading.Thread(target=_run, daemon=True).start()

    def _write_and_sync_row(self, mode, write_fn, write_args, sheet_sync_fn, sheet_sync_args, qr, sheet_label):
        """Write to Firestore, then sync just the one changed row to the
        mode's Sheet - both on a background thread. Replaces the old
        _write_and_resync, which re-read the ENTIRE collection from
        Firestore and re-exported the ENTIRE sheet after every single
        write - the caller is expected to already have updated the local
        list optimistically before calling this (see e.g.
        _on_qr_change_scan_result), same as Buffer's write sites."""
        sheet = self._on_demand_sheet(mode)

        def _run():
            try:
                write_fn(*write_args)
            except Exception as exc:  # noqa: BLE001 - reported below, same precedent as _write_prod5_row
                failure = {
                    "qr": qr, "username": self.username, "app_mode": self.app_mode,
                    "sheet": f"{sheet_label} (Firestore)", "time": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "error": f"{type(exc).__name__}: {exc}",
                }
                threading.Thread(target=log_sheet_write_failure, args=(self.db, failure), daemon=True).start()
                return
            if sheet is not None:
                try:
                    sheet_sync_fn(sheet, *sheet_sync_args)
                except Exception as exc:  # noqa: BLE001
                    failure = {
                        "qr": qr, "username": self.username, "app_mode": self.app_mode,
                        "sheet": sheet_label, "time": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    threading.Thread(target=log_sheet_write_failure, args=(self.db, failure), daemon=True).start()

        threading.Thread(target=_run, daemon=True).start()

    # ---- load Buffer from its Google Sheet mirror ----
    def _load_buffer_from_sheet(self, mode, show_busy=False):
        """One-time (non-live) load of the current Buffer list from its
        Google Sheet mirror - see _subscribe_mode_listeners for why Buffer
        has no permanent Firestore listener anymore. Called once per mode
        at login, and again whenever the operator clicks "Load" (see
        _on_load_buffer_click)."""
        sheet = self.debug_sheet if mode == "debug" else self.ar_sheet
        if sheet is None:
            return
        if show_busy:
            self._set_busy(True, "Loading Buffer...")

        def _run():
            try:
                devices = read_devices_from_sheet(sheet)
            except Exception as exc:  # noqa: BLE001 - reported to the operator below, not raised on a bg thread
                devices = None
                error = str(exc)
            else:
                error = None
            self.result_queue.put(("buffer_loaded", {"mode": mode, "devices": devices, "error": error}))

        threading.Thread(target=_run, daemon=True).start()

    def _load_online_repairs_from_sheet(self, mode, show_busy=False):
        """Same idea as _load_buffer_from_sheet, for Online Repair - see
        _subscribe_mode_listeners for why this has no permanent Firestore
        listener anymore either."""
        sheet = self.debug_online_repair_sheet if mode == "debug" else self.ar_online_repair_sheet
        if sheet is None:
            return
        if show_busy:
            self._set_busy(True, "Loading Online Repair...")

        def _run():
            try:
                records = read_online_repairs_from_sheet(sheet)
            except Exception as exc:  # noqa: BLE001
                records = None
                error = str(exc)
            else:
                error = None
            self.result_queue.put(("online_repairs_loaded", {"mode": mode, "records": records, "error": error}))

        threading.Thread(target=_run, daemon=True).start()

    def _on_load_buffer_click(self):
        """Shared "Load" button for both Buffer and Online Repair - see
        _update_delete_button_state for where it's enabled/disabled."""
        if self.view_mode == "online_repair":
            self._load_online_repairs_from_sheet(self.app_mode, show_busy=True)
        else:
            self._load_buffer_from_sheet(self.app_mode, show_busy=True)

    def _on_buffer_loaded(self, data):
        self._set_busy(False)
        if data["devices"] is None:
            self._show_toast(f"Could not load Buffer from the sheet: {data['error']}", kind="error")
            return
        if data["mode"] == "debug":
            self.debug_devices = data["devices"]
        else:
            self.ar_devices = data["devices"]
        if self.app_mode == data["mode"]:
            self.checked_qrs &= {d["qr"] for d in self.devices}
            if self.view_mode == "buffer":
                self._apply_current_sort()
                self._refresh_tree()

    def _on_online_repairs_loaded(self, data):
        self._set_busy(False)
        if data["records"] is None:
            self._show_toast(f"Could not load Online Repair from the sheet: {data['error']}", kind="error")
            return
        if data["mode"] == "debug":
            self.debug_online_repairs = data["records"]
        else:
            self.ar_online_repairs = data["records"]
        if self.app_mode == data["mode"]:
            self.checked_online_repair_ids &= {r["_doc_id"] for r in self.online_repairs}
            if self.view_mode == "online_repair":
                self._apply_current_sort()
                self._refresh_tree()

    # ---- refresh ----
    def _on_refresh_click(self):
        if self.view_mode == "qr_change":
            if not self.qr_changes:
                messagebox.showinfo("Notice", "The list is empty.")
                return
            self._set_busy(True, "Checking for QR changes...")
            threading.Thread(target=self._do_qr_change_refresh, daemon=True).start()
            return
        if self.view_mode in ("completed", "mrb"):
            self._fetch_on_demand(self.view_mode)
            return
        if not self.devices:
            messagebox.showinfo("Notice", "The list is empty.")
            return
        self._set_busy(True, "Checking the list...")
        threading.Thread(target=self._do_refresh, daemon=True).start()

    def _do_refresh(self):
        # Build the API session (a Selenium cookie round-trip) just once,
        # under the lock, then release it - checking N devices one at a
        # time, each rebuilding its own session and waiting on its own HTTP
        # round-trip, was the main reason Refresh got slower as the buffer
        # grew. The actual per-device lookups are plain HTTP calls against
        # the extracted session, not the Selenium driver, so they can run
        # concurrently without touching self.driver at all.
        with self.lock:
            session = build_api_session(self.driver)
        devices_snapshot = list(self.devices)
        is_ar = self.app_mode == "assembly_rework"

        def _make_check(sess):
            def _check(device):
                try:
                    if is_ar:
                        # Assembly Rework's own condition (see
                        # check_assembly_rework_repaired): a successful
                        # Repair *and* a successful ASM logged after the
                        # device originally failed LongTest/TestRoom/QC -
                        # not Prog Main.
                        repaired, diagnostics = check_assembly_rework_repaired(
                            sess, BASE_URL, device["qr"], device.get("import_time_iso")
                        )
                        return device, repaired, diagnostics, None
                    result = check_device_prog_main_session(sess, BASE_URL, device["qr"])
                    return device, result, result.get("lookup_diagnostics"), None
                except Exception as exc:  # noqa: BLE001
                    return device, None, None, str(exc)
            return _check

        def _run_batch(sess, batch):
            passed_here, needs_retry_here, errors_here, permanent_repair_here = [], [], [], []
            with ThreadPoolExecutor(max_workers=8) as executor:
                for device, result, diagnostics, error in executor.map(_make_check(sess), batch):
                    if error:
                        errors_here.append((device["qr"], error))
                        continue
                    if diagnostics:
                        # The board lookup itself came up empty after every
                        # retry (see _lookup_board) - a "not passed"/"not
                        # repaired" result built on that isn't trustworthy
                        # (the underlying WebDB session may have silently
                        # expired), so don't treat it as a real negative
                        # yet - retry this device once after a re-login.
                        needs_retry_here.append(device)
                        continue
                    if is_ar:
                        if result:
                            passed_here.append({"qr": device["qr"]})
                    elif result.get("passed_last_attempt"):
                        passed_here.append({
                            "qr": device["qr"],
                            "last_time": result.get("last_time"),
                            "debug_operator": result.get("debug_operator"),
                            "complete_time": result.get("complete_time"),
                            "color": result.get("color"),
                            "board_name": result.get("board_name"),
                        })
                    elif result.get("permanent_repairs_found"):
                        # Still sitting in Buffer (didn't pass), but WebDB
                        # now shows this PCB has been permanently repaired -
                        # see _maybe_flag_permanent_repair_for_qr_change.
                        permanent_repair_here.append({
                            "qr": device["qr"],
                            "pcb_qr": result.get("pcb_qr"),
                            "board_name": result.get("board_name"),
                            "region": result.get("region"),
                            "permanent_repairs_found": True,
                        })
            return passed_here, needs_retry_here, errors_here, permanent_repair_here

        passed, needs_retry, errors, permanent_repairs = _run_batch(session, devices_snapshot)

        if needs_retry:
            with self.lock:
                self._relogin_webdb_locked()
                session = build_api_session(self.driver)
            retry_passed, still_failing, retry_errors, retry_permanent_repairs = _run_batch(session, needs_retry)
            passed.extend(retry_passed)
            errors.extend(retry_errors)
            permanent_repairs.extend(retry_permanent_repairs)
            for device in still_failing:
                threading.Thread(
                    target=log_lookup_failure,
                    args=(self.db, {
                        "qr": device["qr"], "username": self.username, "app_mode": self.app_mode,
                        "context": "refresh", "time": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "attempts": [{"note": "still failing after relogin retry during refresh"}],
                    }),
                    daemon=True,
                ).start()
                # Previously silent to the operator (Firestore log only) -
                # a device stuck here looks identical to one that's
                # genuinely just not repaired yet, with nothing on screen
                # to say otherwise. Surface it through the same "Some
                # devices failed to check" warning as any other error so
                # it's visibly distinct from "not repaired", not silently
                # dropped.
                errors.append((device["qr"], "Could not verify (WebDB unreachable after retry) - try Refresh again"))

        self.result_queue.put((
            "refresh_result", {"passed": passed, "errors": errors, "permanent_repairs": permanent_repairs},
        ))

    def _on_refresh_result(self, data):
        self._set_busy(False)
        devices_collection = self._devices_collection
        completed_collection = self._completed_collection
        is_ar = self.app_mode == "assembly_rework"
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        ar_operator = self.operator_names.get(self.username, self.username)
        to_delete = []
        completed_now = []
        for item in data["passed"]:
            qr = item["qr"]
            original = next((d for d in self.devices if d["qr"] == qr), {})
            if is_ar:
                completed_device = {
                    "qr": qr,
                    "board": original.get("board", ""),
                    "import_time": original.get("import_time", ""),
                    "import_time_iso": original.get("import_time_iso"),
                    "defect": original.get("defect", ""),
                    "complete_time": format_utc_to_gmt7(now_iso),
                    "complete_time_iso": now_iso,
                    "debug_operator": ar_operator,
                }
            else:
                completed_device = {
                    "qr": qr,
                    "board": original.get("board", ""),
                    "import_time": original.get("import_time", ""),
                    "import_time_iso": original.get("import_time_iso"),
                    "attempt_count": original.get("attempt_count"),
                    "defect": original.get("defect", ""),
                    "complete_time": format_utc_to_gmt7(item.get("complete_time")),
                    "complete_time_iso": item.get("complete_time"),
                    "debug_operator": item.get("debug_operator") or "No info",
                }
            if is_ar:
                if self.ar_fact_rework_sheet:
                    # "From QC" -> "QC", "From Long Test" -> "LONG" (the
                    # buffer-income defect label, reversed back to Fact-
                    # Rework's District-to vocabulary).
                    district_to = "QC" if "QC" in (original.get("defect") or "") else "LONG"
                    self._write_prod5_row(
                        record_ar_fact_rework_event,
                        (
                            self.ar_fact_rework_sheet, district_to, "Repair from buffer",
                            ar_operator, completed_device["board"],
                        ),
                        qr, "Fact-Rework",
                    )
            elif self.fact_ok_sheet and not should_skip_prod5_sheet(item.get("board_name")):
                self._write_prod5_row(
                    record_completed,
                    (self.fact_ok_sheet, completed_device["board"], item.get("color")),
                    qr, "Fact-D&R_ok",
                )
            to_delete.append(qr)
            self.checked_qrs.discard(qr)
            completed_now.append(completed_device)
        if to_delete:
            threading.Thread(
                target=delete_devices, args=(self.db, to_delete, devices_collection), daemon=True
            ).start()
            if self.sheet:
                for qr in to_delete:
                    self._write_prod5_row(sync_device_removed, (self.sheet, qr), qr, "buffer")
            # See _on_scan_result's matching comment - no permanent Buffer
            # listener anymore, so this batch's own removals need an
            # explicit local update.
            self.devices = [d for d in self.devices if d["qr"] not in to_delete]
            if self.view_mode == "buffer":
                self._apply_current_sort()
                self._refresh_tree()
        if completed_now:
            # Completed has no permanent listener (see the on-demand fetch
            # section) - each device in this batch is written to Firestore
            # and its own row synced to the sheet in one pass, on one
            # background thread (not one thread per device, to keep this
            # batch's writes ordered rather than racing each other).
            completed_sheet = self.completed_sheet

            def _write_all(devices=list(completed_now), db=self.db, collection=completed_collection, sheet=completed_sheet):
                for device in devices:
                    add_completed_device(db, device, collection)
                    if sheet is not None:
                        try:
                            sync_completed_upserted(sheet, device)
                        except Exception as exc:  # noqa: BLE001
                            failure = {
                                "qr": device.get("qr"), "username": self.username, "app_mode": self.app_mode,
                                "sheet": "completed", "time": dt.datetime.now(dt.timezone.utc).isoformat(),
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                            threading.Thread(
                                target=log_sheet_write_failure, args=(self.db, failure), daemon=True
                            ).start()

            threading.Thread(target=_write_all, daemon=True).start()
            self._show_completed_notification(completed_now)
        for item in data.get("permanent_repairs", []):
            self._maybe_flag_permanent_repair_for_qr_change(item["qr"], item)
        if data["errors"]:
            msg = "\n".join(f"{qr}: {err}" for qr, err in data["errors"])
            messagebox.showwarning("Some devices failed to check", msg)

    def _complete_buffer_from_online_repair(self, qr):
        """Scanning a QR in Online Repair mode that's currently sitting in
        Buffer means the operator is confirming it right there and then -
        treat it exactly like a Buffer Refresh completion for this one
        device (see _on_refresh_result) instead of ALSO logging a separate
        Online Repair entry, so the same physical unit never ends up
        listed in both places at once with no link between them.

        Reuses the existing Buffer row's own data (board/defect/
        import_time) rather than doing a fresh WebDB lookup - scanning it
        here already commits to "this one just came back fixed", the same
        way a passing Refresh does.
        """
        original = next((d for d in self.devices if d["qr"] == qr), None)
        if original is None:
            return
        is_ar = self.app_mode == "assembly_rework"
        devices_collection = self._devices_collection
        completed_collection = self._completed_collection
        operator = self.operator_names.get(self.username, self.username)
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

        completed_device = {
            "qr": qr,
            "board": original.get("board", ""),
            "import_time": original.get("import_time", ""),
            "import_time_iso": original.get("import_time_iso"),
            "defect": original.get("defect", ""),
            "complete_time": format_utc_to_gmt7(now_iso),
            "complete_time_iso": now_iso,
            "debug_operator": operator,
        }
        if not is_ar:
            completed_device["attempt_count"] = original.get("attempt_count")

        if is_ar:
            if self.ar_fact_rework_sheet:
                district_to = "QC" if "QC" in (original.get("defect") or "") else "LONG"
                self._write_prod5_row(
                    record_ar_fact_rework_event,
                    (
                        self.ar_fact_rework_sheet, district_to, "Repair from buffer",
                        operator, completed_device["board"],
                    ),
                    qr, "Fact-Rework",
                )
        elif self.fact_ok_sheet and not should_skip_prod5_sheet(original.get("board_name")):
            self._write_prod5_row(
                record_completed,
                (self.fact_ok_sheet, completed_device["board"], original.get("color")),
                qr, "Fact-D&R_ok",
            )

        self.checked_qrs.discard(qr)

        completed_sheet = self.completed_sheet

        def _write_all(
            db=self.db, qr=qr, device=completed_device,
            devices_collection=devices_collection, completed_collection=completed_collection, sheet=completed_sheet,
        ):
            delete_devices(db, [qr], devices_collection)
            add_completed_device(db, device, completed_collection)
            if sheet is not None:
                try:
                    sync_completed_upserted(sheet, device)
                except Exception as exc:  # noqa: BLE001
                    failure = {
                        "qr": qr, "username": self.username, "app_mode": self.app_mode,
                        "sheet": "completed", "time": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    threading.Thread(target=log_sheet_write_failure, args=(self.db, failure), daemon=True).start()

        threading.Thread(target=_write_all, daemon=True).start()
        if self.sheet:
            self._write_prod5_row(sync_device_removed, (self.sheet, qr), qr, "buffer")
        # See _on_scan_result's matching comment - no permanent Buffer
        # listener anymore, so this removal needs an explicit local update.
        self.devices = [d for d in self.devices if d["qr"] != qr]
        if self.view_mode == "buffer":
            self._apply_current_sort()
            self._refresh_tree()
        self._show_completed_notification([completed_device])

    def _do_qr_change_refresh(self):
        """For every tracked PCB QR, ask WebDB for its current paper QR
        (one batched call - see check_pcb_qr_current) and compare against
        the row's own previous_qr baseline. Only rows where it's actually
        different get queued for a Firestore write - unchanged rows are
        left alone entirely, matching the "don't do anything if it's the
        same" behaviour asked for."""
        rows_snapshot = list(self.qr_changes)
        pcb_qrs = [row["pcb_qr"] for row in rows_snapshot]
        try:
            with self.lock:
                session = build_api_session(self.driver)
                current_map = check_pcb_qr_current(session, BASE_URL, pcb_qrs)
                if not current_map and pcb_qrs and self._relogin_webdb_locked():
                    session = build_api_session(self.driver)
                    current_map = check_pcb_qr_current(session, BASE_URL, pcb_qrs)
        except Exception as exc:  # noqa: BLE001
            self.result_queue.put(("error", f"Error checking QR changes: {exc}"))
            return

        changed = [
            (row["pcb_qr"], current_map[row["pcb_qr"]])
            for row in rows_snapshot
            if current_map.get(row["pcb_qr"]) and current_map[row["pcb_qr"]] != row["previous_qr"]
        ]
        self.result_queue.put(("qr_change_refresh_result", changed))

    def _on_qr_change_refresh_result(self, changed):
        self._set_busy(False)
        if changed:
            changed_map = dict(changed)
            self.qr_changes = [
                {**row, "current_qr": changed_map[row["pcb_qr"]]} if row["pcb_qr"] in changed_map else row
                for row in self.qr_changes
            ]
            self._apply_current_sort()
            self._refresh_tree()

            qr_change_sheet = self.qr_change_sheet

            def _write_all(changed=changed, db=self.db, sheet=qr_change_sheet):
                for pcb_qr, current_qr in changed:
                    update_qr_change_current(db, pcb_qr, current_qr)
                    if sheet is not None:
                        try:
                            sync_qr_change_current_updated(sheet, pcb_qr, current_qr)
                        except Exception as exc:  # noqa: BLE001
                            failure = {
                                "qr": pcb_qr, "username": self.username, "app_mode": self.app_mode,
                                "sheet": "qr_change", "time": dt.datetime.now(dt.timezone.utc).isoformat(),
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                            threading.Thread(
                                target=log_sheet_write_failure, args=(self.db, failure), daemon=True
                            ).start()

            threading.Thread(target=_write_all, daemon=True).start()
            self._show_toast(f"{len(changed)} QR change(s) detected and updated.", kind="info")
        else:
            self._show_toast("No QR changes detected.", kind="info")

    def _show_completed_notification(self, completed_devices):
        """Read-only popup listing every device just moved to Completed by a
        refresh - informational only, no selection/actions available."""
        window = tk.Toplevel(self.root)
        window.title("Devices Completed")
        window.configure(bg=BG)
        window.transient(self.root)
        window.geometry("620x360")
        window.minsize(480, 260)

        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame, text=f"{len(completed_devices)} device(s) PASSED their Prog check and moved to Completed",
            style="Header.TLabel", wraplength=560,
        ).pack(anchor="w", pady=(0, 12))

        table_frame = ttk.Frame(frame)
        table_frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(
            table_frame, columns=("id", "model", "complete_time"), show="headings", height=10
        )
        tree.heading("id", text="ID")
        tree.heading("model", text="Model")
        tree.heading("complete_time", text="Complete Time")
        tree.column("id", width=140, anchor="w")
        tree.column("model", width=220, anchor="w")
        tree.column("complete_time", width=180, anchor="center")
        tree.tag_configure("evenrow", background=FIELD_BG)
        tree.tag_configure("oddrow", background=PANEL_BG)
        tree_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=tree_scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")
        for i, device in enumerate(completed_devices):
            tree.insert(
                "", "end",
                tags=("evenrow" if i % 2 == 0 else "oddrow",),
                values=(device["qr"], device.get("board") or "No info", device.get("complete_time", "")),
            )

        ttk.Button(frame, text="OK", style="Accent.TButton", command=window.destroy).pack(
            anchor="e", pady=(12, 0)
        )
        window.focus_set()

    # ---- settings (owner/admin only): manage app accounts + roles ----
    def _open_settings_window(self):
        if self.role not in ("owner", "admin"):
            return
        mode_labels = {"both": "Both", "debug": "Debug", "assembly_rework": "Assembly Rework"}
        mode_values_by_label = {v: k for k, v in mode_labels.items()}

        window = tk.Toplevel(self.root)
        window.title("Settings")
        window.configure(bg=BG)
        window.transient(self.root)
        window.geometry("560x660")
        window.minsize(500, 520)

        notebook = ttk.Notebook(window)
        notebook.pack(fill="both", expand=True, padx=12, pady=12)
        accounts_tab = ttk.Frame(notebook)
        queue_tab = ttk.Frame(notebook)
        notebook.add(accounts_tab, text="App Accounts")
        notebook.add(queue_tab, text="Queue Registration")

        frame = ttk.Frame(accounts_tab, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="App Accounts", style="Header.TLabel").pack(anchor="w", pady=(0, 4))
        ttk.Label(
            frame,
            text=f"Owner ({OWNER_USERNAME}) always has full access. "
                 "Admins can delete and manage accounts; Users cannot. "
                 "Mode controls which of Debug / Assembly Rework an account may use.",
            style="Muted.TLabel", wraplength=520,
        ).pack(anchor="w", pady=(0, 12))

        list_frame = ttk.Frame(frame)
        list_frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(list_frame, columns=("username", "role", "mode"), show="headings", height=10)
        tree.heading("username", text="Username")
        tree.heading("role", text="Role")
        tree.heading("mode", text="Mode")
        tree.column("username", width=220, anchor="w")
        tree.column("role", width=100, anchor="center")
        tree.column("mode", width=140, anchor="center")
        tree.tag_configure("evenrow", background=FIELD_BG)
        tree.tag_configure("oddrow", background=PANEL_BG)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        def _reload():
            tree.delete(*tree.get_children())
            tree.insert(
                "", "end", iid=OWNER_USERNAME, tags=("evenrow",),
                values=(OWNER_USERNAME, "owner", mode_labels["both"]),
            )
            others = sorted(
                (a for a in self.accounts if a.get("username") != OWNER_USERNAME),
                key=lambda a: a.get("username", ""),
            )
            for i, account in enumerate(others, start=1):
                mode_label = mode_labels.get(account.get("mode_permission") or "both", "Both")
                tree.insert(
                    "", "end", iid=account["username"], tags=("evenrow" if i % 2 == 0 else "oddrow",),
                    values=(account["username"], account.get("role", "user"), mode_label),
                )

        _reload()
        self._settings_reload_fn = _reload

        def _on_window_close():
            self._settings_reload_fn = None
            self._queue_settings_reload_fn = None
            if self.queue_accounts_watch:
                self.queue_accounts_watch.unsubscribe()
                self.queue_accounts_watch = None
            window.destroy()

        window.protocol("WM_DELETE_WINDOW", _on_window_close)

        # ---- add / update account ----
        add_frame = ttk.Frame(frame)
        add_frame.pack(fill="x", pady=(12, 0))
        ttk.Label(add_frame, text="Username").grid(row=0, column=0, sticky="w")
        ttk.Label(add_frame, text="Password").grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(add_frame, text="Role").grid(row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Label(add_frame, text="Mode").grid(row=0, column=3, sticky="w", padx=(8, 0))
        username_entry = ttk.Entry(add_frame, width=14)
        username_entry.grid(row=1, column=0)
        password_entry = ttk.Entry(add_frame, width=14, show="*")
        password_entry.grid(row=1, column=1, padx=(8, 0))
        role_var = tk.StringVar(value="user")
        ttk.Combobox(
            add_frame, textvariable=role_var, values=["user", "admin"], state="readonly", width=8
        ).grid(row=1, column=2, padx=(8, 0))
        add_mode_var = tk.StringVar(value=mode_labels["both"])
        ttk.Combobox(
            add_frame, textvariable=add_mode_var, values=list(mode_labels.values()),
            state="readonly", width=14,
        ).grid(row=1, column=3, padx=(8, 0))

        def _add_account():
            uname = username_entry.get().strip()
            pwd = password_entry.get()
            if not uname or not pwd:
                self._show_toast("Enter both a username and password.", kind="error")
                return
            if uname == OWNER_USERNAME:
                self._show_toast(f"{OWNER_USERNAME} is already the owner.", kind="error")
                return
            mode_permission = mode_values_by_label[add_mode_var.get()]
            username_entry.delete(0, "end")
            password_entry.delete(0, "end")
            threading.Thread(
                target=set_app_account, args=(self.db, uname, pwd, role_var.get(), mode_permission),
                daemon=True,
            ).start()

        ttk.Button(frame, text="Add / Update Account", style="Accent.TButton", command=_add_account).pack(
            anchor="w", pady=(8, 0)
        )

        # ---- manage selected account ----
        def _selected_username():
            selection = tree.selection()
            if not selection or selection[0] == OWNER_USERNAME:
                self._show_toast("Select a non-owner account first.", kind="info")
                return None
            return selection[0]

        def _toggle_role():
            uname = _selected_username()
            if not uname:
                return
            account = next((a for a in self.accounts if a.get("username") == uname), None)
            new_role = "user" if (account or {}).get("role") == "admin" else "admin"
            threading.Thread(target=set_account_role, args=(self.db, uname, new_role), daemon=True).start()

        def _remove_account():
            uname = _selected_username()
            if not uname:
                return
            threading.Thread(target=delete_app_account, args=(self.db, uname), daemon=True).start()

        set_mode_row = ttk.Frame(frame)
        set_mode_row.pack(fill="x", pady=(14, 0))
        ttk.Label(set_mode_row, text="Set mode for selected:", style="Muted.TLabel").pack(side="left")
        set_mode_var = tk.StringVar(value=mode_labels["both"])
        ttk.Combobox(
            set_mode_row, textvariable=set_mode_var, values=list(mode_labels.values()),
            state="readonly", width=14,
        ).pack(side="left", padx=(8, 8))

        def _apply_mode():
            uname = _selected_username()
            if not uname:
                return
            mode_permission = mode_values_by_label[set_mode_var.get()]
            threading.Thread(
                target=set_account_mode, args=(self.db, uname, mode_permission), daemon=True
            ).start()

        ttk.Button(set_mode_row, text="Apply", style="Secondary.TButton", command=_apply_mode).pack(
            side="left"
        )

        button_row = ttk.Frame(frame)
        button_row.pack(fill="x", pady=(10, 0))
        ttk.Button(button_row, text="Toggle Admin/User", style="Secondary.TButton", command=_toggle_role).pack(
            side="left"
        )
        ttk.Button(button_row, text="Remove Account", style="Danger.TButton", command=_remove_account).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(button_row, text="Close", style="Secondary.TButton", command=_on_window_close).pack(
            side="right"
        )

        # ---- Queue Registration tab: department scan-page (public/*/index.html) logins ----
        dept_labels = _QUEUE_DEPARTMENT_LABELS
        dept_values_by_label = {v: k for k, v in dept_labels.items()}

        queue_frame = ttk.Frame(queue_tab, padding=16)
        queue_frame.pack(fill="both", expand=True)
        ttk.Label(queue_frame, text="Queue Registration Accounts", style="Header.TLabel").pack(
            anchor="w", pady=(0, 4)
        )
        ttk.Label(
            queue_frame,
            text="Logins for the 5 department scan web pages - each account may scan into "
                 "exactly one department. Separate from the App Accounts above.",
            style="Muted.TLabel", wraplength=520,
        ).pack(anchor="w", pady=(0, 12))

        queue_list_frame = ttk.Frame(queue_frame)
        queue_list_frame.pack(fill="both", expand=True)
        queue_tree = ttk.Treeview(
            queue_list_frame, columns=("username", "department"), show="headings", height=10
        )
        queue_tree.heading("username", text="Username")
        queue_tree.heading("department", text="Department")
        queue_tree.column("username", width=260, anchor="w")
        queue_tree.column("department", width=180, anchor="center")
        queue_tree.tag_configure("evenrow", background=FIELD_BG)
        queue_tree.tag_configure("oddrow", background=PANEL_BG)
        queue_scroll = ttk.Scrollbar(queue_list_frame, orient="vertical", command=queue_tree.yview)
        queue_tree.configure(yscrollcommand=queue_scroll.set)
        queue_tree.pack(side="left", fill="both", expand=True)
        queue_scroll.pack(side="right", fill="y")

        def _queue_reload():
            queue_tree.delete(*queue_tree.get_children())
            accounts = sorted(self.queue_accounts, key=lambda a: a.get("username", ""))
            for i, account in enumerate(accounts):
                dept_label = dept_labels.get(account.get("department"), account.get("department", ""))
                queue_tree.insert(
                    "", "end", iid=account["username"], tags=("evenrow" if i % 2 == 0 else "oddrow",),
                    values=(account["username"], dept_label),
                )

        _queue_reload()
        self._queue_settings_reload_fn = _queue_reload
        self.queue_accounts_watch = listen_queue_accounts(self.db, self._on_queue_accounts_changed)

        queue_add_frame = ttk.Frame(queue_frame)
        queue_add_frame.pack(fill="x", pady=(12, 0))
        ttk.Label(queue_add_frame, text="Username").grid(row=0, column=0, sticky="w")
        ttk.Label(queue_add_frame, text="Password").grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(queue_add_frame, text="Department").grid(row=0, column=2, sticky="w", padx=(8, 0))
        queue_username_entry = ttk.Entry(queue_add_frame, width=16)
        queue_username_entry.grid(row=1, column=0)
        queue_password_entry = ttk.Entry(queue_add_frame, width=16, show="*")
        queue_password_entry.grid(row=1, column=1, padx=(8, 0))
        queue_add_dept_var = tk.StringVar(value=dept_labels["hard-test"])
        ttk.Combobox(
            queue_add_frame, textvariable=queue_add_dept_var, values=list(dept_labels.values()),
            state="readonly", width=14,
        ).grid(row=1, column=2, padx=(8, 0))

        def _add_queue_account():
            uname = queue_username_entry.get().strip()
            pwd = queue_password_entry.get()
            if not uname or not pwd:
                self._show_toast("Enter both a username and password.", kind="error")
                return
            department = dept_values_by_label[queue_add_dept_var.get()]
            queue_username_entry.delete(0, "end")
            queue_password_entry.delete(0, "end")
            threading.Thread(
                target=set_queue_account, args=(self.db, uname, pwd, department), daemon=True,
            ).start()

        ttk.Button(
            queue_frame, text="Add / Update Account", style="Accent.TButton", command=_add_queue_account
        ).pack(anchor="w", pady=(8, 0))

        def _selected_queue_username():
            selection = queue_tree.selection()
            if not selection:
                self._show_toast("Select an account first.", kind="info")
                return None
            return selection[0]

        def _remove_queue_account():
            uname = _selected_queue_username()
            if not uname:
                return
            threading.Thread(target=delete_queue_account, args=(self.db, uname), daemon=True).start()

        queue_set_dept_row = ttk.Frame(queue_frame)
        queue_set_dept_row.pack(fill="x", pady=(14, 0))
        ttk.Label(queue_set_dept_row, text="Set department for selected:", style="Muted.TLabel").pack(
            side="left"
        )
        queue_set_dept_var = tk.StringVar(value=dept_labels["hard-test"])
        ttk.Combobox(
            queue_set_dept_row, textvariable=queue_set_dept_var, values=list(dept_labels.values()),
            state="readonly", width=14,
        ).pack(side="left", padx=(8, 8))

        def _apply_queue_department():
            uname = _selected_queue_username()
            if not uname:
                return
            department = dept_values_by_label[queue_set_dept_var.get()]
            threading.Thread(
                target=set_queue_account_department, args=(self.db, uname, department), daemon=True
            ).start()

        ttk.Button(
            queue_set_dept_row, text="Apply", style="Secondary.TButton", command=_apply_queue_department
        ).pack(side="left")

        queue_button_row = ttk.Frame(queue_frame)
        queue_button_row.pack(fill="x", pady=(10, 0))
        ttk.Button(
            queue_button_row, text="Remove Account", style="Danger.TButton", command=_remove_queue_account
        ).pack(side="left")
        ttk.Button(queue_button_row, text="Close", style="Secondary.TButton", command=_on_window_close).pack(
            side="right"
        )

        window.focus_set()

    # ---- logout ----
    def _on_logout(self):
        if not messagebox.askyesno("Logout", "Are you sure you want to log out?"):
            return
        for watch_attr in (
            "debug_devices_watch", "debug_online_repair_watch", "debug_io_queue_watch",
            "ar_devices_watch", "ar_online_repair_watch", "ar_io_queue_watch",
            "accounts_watch", "queue_accounts_watch",
        ):
            watch = getattr(self, watch_attr)
            if watch:
                watch.unsubscribe()
                setattr(self, watch_attr, None)
        self.accounts = []
        self.role = None
        self.db = None
        self.debug_sheet = None
        self.debug_completed_sheet = None
        self.debug_online_repair_sheet = None
        self.debug_mrb_sheet = None
        self.ar_sheet = None
        self.ar_completed_sheet = None
        self.ar_online_repair_sheet = None
        self.ar_mrb_sheet = None
        self.qr_change_sheet = None
        self.pcb_choice_rules = []
        self.operator_names = {}
        self.from_sheet = None
        self.fact_ok_sheet = None
        self.fact_dr_sheet = None
        self.ar_prod5_from_sheet = None
        self.ar_fact_rework_sheet = None
        self.device_choice_rules = []
        self.debug_devices = []
        self.debug_completed_devices = []
        self.debug_online_repairs = []
        self.debug_mrb_devices = []
        self.debug_io_queue = []
        self.ar_devices = []
        self.ar_completed_devices = []
        self.ar_online_repairs = []
        self.ar_mrb_devices = []
        self.ar_io_queue = []
        self.qr_changes = []
        self.checked_qrs = set()
        self.checked_completed_qrs = set()
        self.checked_online_repair_ids = set()
        self.checked_mrb_qrs = set()
        self.checked_qr_change_ids = set()
        self.checked_io_queue_ids = set()
        self.view_mode = "online_repair"
        self.app_mode = "debug"
        self.mode_permission = "both"
        with self.lock:
            if self.driver:
                try:
                    # Clear cookies/storage to actually log out of the site,
                    # but keep the rest of the browser profile (disk cache,
                    # cached JS/CSS bundles) so the next login stays fast -
                    # wiping the whole profile forced a slow cold start.
                    self.driver.delete_all_cookies()
                    self.driver.execute_script(
                        "window.localStorage.clear(); window.sessionStorage.clear();"
                    )
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self.driver.quit()
                except Exception:  # noqa: BLE001
                    pass
                self.driver = None
        self.username = None
        self.main_frame.destroy()
        self.main_frame = None
        self._build_login_frame()

    def _on_close(self):
        if self.serial_scanner is not None:
            self.serial_scanner.stop()
        for watch_attr in (
            "debug_devices_watch", "debug_online_repair_watch", "debug_io_queue_watch",
            "ar_devices_watch", "ar_online_repair_watch", "ar_io_queue_watch",
            "accounts_watch", "queue_accounts_watch",
        ):
            watch = getattr(self, watch_attr)
            if watch:
                watch.unsubscribe()
        with self.lock:
            if self.driver:
                try:
                    self.driver.quit()
                except Exception:  # noqa: BLE001
                    pass
        self.root.destroy()

    # ---------------- result polling ----------------
    def _poll_results(self):
        try:
            while True:
                self._handle_result(self.result_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(150, self._poll_results)

    def _handle_result(self, item):
        kind = item[0]
        if kind == "login_result":
            data = item[1]
            self.login_button.config(state="normal")
            if data["ok"]:
                self.driver = data["driver"]
                self.db = data["db"]
                self.debug_sheet = data["sheet"]
                self.debug_completed_sheet = data["completed_sheet"]
                self.debug_online_repair_sheet = data["online_repair_sheet"]
                self.ar_sheet = data["ar_sheet"]
                self.ar_completed_sheet = data["ar_completed_sheet"]
                self.ar_online_repair_sheet = data["ar_online_repair_sheet"]
                self.debug_mrb_sheet = data["mrb_sheet"]
                self.ar_mrb_sheet = data["ar_mrb_sheet"]
                self.qr_change_sheet = data["qr_change_sheet"]
                self.pcb_choice_rules = data["pcb_choice_rules"]
                self.operator_names = data["operator_names"]
                self.from_sheet = data["from_sheet"]
                self.fact_ok_sheet = data["fact_ok_sheet"]
                self.fact_dr_sheet = data["fact_dr_sheet"]
                self.ar_prod5_from_sheet = data["ar_prod5_from_sheet"]
                self.ar_fact_rework_sheet = data["ar_fact_rework_sheet"]
                self.device_choice_rules = data["device_choice_rules"]
                self.username = data["username"]
                self.role = data["role"]
                self.mode_permission = data["mode_permission"]
                allowed = self._allowed_modes()
                self.app_mode = "debug" if "debug" in allowed else next(iter(allowed))
                self.login_frame.destroy()
                self.login_frame = None
                self._build_main_frame()
                _force_repaint(self.root)
                # Completed/MRB/QR Change are fetched on demand instead of
                # a permanent listener (see the "on-demand fetch" section) -
                # not used every day, so there's deliberately no watch set
                # up for any of them here. Buffer/Online Repair/Queue are
                # only ever subscribed for modes this account is actually
                # allowed into (see _subscribe_mode_listeners) - most
                # "user" role accounts are restricted to exactly one mode,
                # so this halves the listener count (and the read cost
                # that comes with it) for them versus subscribing to both
                # unconditionally.
                for mode in allowed:
                    self._subscribe_mode_listeners(mode)
                self.accounts_watch = listen_app_accounts(self.db, self._on_accounts_changed)
                if data["sheet_warning"]:
                    messagebox.showwarning("Report sheet", data["sheet_warning"])
            else:
                self.login_status_label.config(text=data["error"] or "Login failed.", foreground=DANGER)
        elif kind == "buffer_loaded":
            self._on_buffer_loaded(item[1])
        elif kind == "online_repairs_loaded":
            self._on_online_repairs_loaded(item[1])
        elif kind == "devices_snapshot":
            self.debug_devices = item[1]
            if self.app_mode == "debug":
                self.checked_qrs &= {d["qr"] for d in self.debug_devices}
                self._on_snapshot_update()
            if self.debug_sheet:
                threading.Thread(
                    target=export_devices, args=(self.debug_sheet, list(self.debug_devices)), daemon=True
                ).start()
        elif kind == "ar_devices_snapshot":
            self.ar_devices = item[1]
            if self.app_mode == "assembly_rework":
                self.checked_qrs &= {d["qr"] for d in self.ar_devices}
                self._on_snapshot_update()
            if self.ar_sheet:
                threading.Thread(
                    target=export_devices, args=(self.ar_sheet, list(self.ar_devices)), daemon=True
                ).start()
        elif kind == "online_repair_snapshot":
            self.debug_online_repairs = item[1]
            if self.app_mode == "debug":
                self.checked_online_repair_ids &= {d["_doc_id"] for d in self.debug_online_repairs}
                self._on_snapshot_update()
            if self.debug_online_repair_sheet:
                threading.Thread(
                    target=export_online_repairs, args=(self.debug_online_repair_sheet, item[1]), daemon=True
                ).start()
        elif kind == "ar_online_repair_snapshot":
            self.ar_online_repairs = item[1]
            if self.app_mode == "assembly_rework":
                self.checked_online_repair_ids &= {d["_doc_id"] for d in self.ar_online_repairs}
                self._on_snapshot_update()
            if self.ar_online_repair_sheet:
                threading.Thread(
                    target=export_online_repairs, args=(self.ar_online_repair_sheet, item[1]), daemon=True
                ).start()
        elif kind == "io_queue_snapshot":
            # No Google Sheet mirror here (unlike devices/online_repairs) -
            # each department's own scan page already writes straight to
            # its own sheet tab via the Cloud Function; this listener only
            # drives this app's own Queue view + counter.
            self.debug_io_queue = item[1]
            if self.app_mode == "debug":
                self.checked_io_queue_ids &= {d["_doc_id"] for d in self.debug_io_queue}
                self._update_queue_counter_label()
                self._on_snapshot_update()
        elif kind == "ar_io_queue_snapshot":
            self.ar_io_queue = item[1]
            if self.app_mode == "assembly_rework":
                self.checked_io_queue_ids &= {d["_doc_id"] for d in self.ar_io_queue}
                self._update_queue_counter_label()
                self._on_snapshot_update()
        elif kind == "on_demand_fetch_result":
            data = item[1]
            self._set_busy(False)
            mode, items = data["mode"], data["items"]
            if items is None:
                self._show_toast("Failed to load - please try again.", kind="error")
            elif mode in ("completed", "mrb") and data["app_mode"] != self.app_mode:
                # Stale: the user switched Debug/Assembly Rework mode again
                # before this landed - it belongs to neither list anymore,
                # whatever triggered the new mode's own view already kicked
                # off its own fetch.
                pass
            else:
                if mode == "completed":
                    self.completed_devices = items
                    self.checked_completed_qrs &= {d["qr"] for d in items}
                elif mode == "mrb":
                    self.mrb_devices = items
                    self.checked_mrb_qrs &= {d["qr"] for d in items}
                elif mode == "qr_change":
                    self.qr_changes = items
                    self.checked_qr_change_ids &= {d["pcb_qr"] for d in items}
                if self.view_mode == mode:
                    self._apply_current_sort()
                    self._refresh_tree()
                    self._refresh_summary()
                self._update_delete_button_state()
        elif kind == "chart_history_result":
            data = item[1]
            stale = (
                data["token"] != self._chart_fetch_token
                or self.view_mode != "chart"
                or data["days"] != self.chart_range_days
                or data["app_mode"] != self.app_mode
            )
            if not stale:
                repairs = data["repairs"] if data["repairs"] is not None else self.online_repairs
                self._chart_completed_cache = data["completed"]
                self._chart_repairs_cache = data["repairs"]
                self._render_chart_with(self.devices, data["completed"], repairs, data["days"])
        elif kind == "summary_history_result":
            data = item[1]
            stale = (
                data["token"] != self._summary_fetch_token
                or self.view_mode != "summary"
                or data["app_mode"] != self.app_mode
            )
            if not stale:
                self._summary_completed_cache = data["completed"]
                self._summary_repairs_cache = data["repairs"]
                self._summary_range_cache = (data["start_date"], data["end_date"], data["shift"])
                self._render_summary_report_with(
                    self.devices, data["completed"], data["repairs"],
                    data["start_date"], data["end_date"], data["shift"],
                )
        elif kind == "accounts_snapshot":
            self.accounts = item[1]
            self._refresh_role_from_accounts()
            self._refresh_mode_permission_from_accounts()
            self._refresh_report_access_from_role()
            self._update_delete_button_state()
            if self._settings_reload_fn:
                self._settings_reload_fn()
        elif kind == "queue_accounts_snapshot":
            self.queue_accounts = item[1]
            if self._queue_settings_reload_fn:
                self._queue_settings_reload_fn()
        elif kind == "sheet_write_failed":
            data = item[1]
            self._show_toast(
                f"{data['qr']}: FAILED to write to {data['sheet']} sheet - check manually", kind="error"
            )
        elif kind == "scan_result":
            self._on_scan_result(item[1])
        elif kind == "ar_scan_result":
            self._on_ar_scan_result(item[1])
        elif kind == "online_repair_scan_result":
            self._on_online_repair_scan_result(item[1])
        elif kind == "mrb_scan_result":
            self._on_mrb_scan_result(item[1])
        elif kind == "qr_change_scan_result":
            self._on_qr_change_scan_result(item[1])
        elif kind == "qr_change_refresh_result":
            self._on_qr_change_refresh_result(item[1])
        elif kind == "refresh_result":
            self._on_refresh_result(item[1])
        elif kind == "reconnect_result":
            self._set_busy(False)
            if item[1]:
                self._show_toast("Reconnected to WebDB.", kind="info")
            else:
                self._show_toast("Reconnect failed - please try again.", kind="error")
        elif kind == "delete_done":
            self._set_busy(False)
        elif kind == "error":
            self._set_busy(False)
            messagebox.showerror("Error", item[1])

    def _on_devices_changed(self, devices):
        """Called on Firestore's own background thread - just hand off to the
        main-thread queue, never touch Tkinter widgets from here directly."""
        self.result_queue.put(("devices_snapshot", devices))

    def _on_online_repair_changed(self, records):
        self.result_queue.put(("online_repair_snapshot", records))

    def _on_ar_devices_changed(self, devices):
        self.result_queue.put(("ar_devices_snapshot", devices))

    def _on_ar_online_repair_changed(self, records):
        self.result_queue.put(("ar_online_repair_snapshot", records))

    @staticmethod
    def _format_io_queue_records(records):
        """The scan pages write `scanned_at` as a Firestore serverTimestamp,
        not a pre-formatted ISO string like every other collection here
        (those are written by this Python app itself) - so unlike
        add_device/add_online_repair/etc.'s records, this has to be
        converted to the app's usual _iso/display-string pair on read
        instead of at write time."""
        for record in records:
            scanned_at = record.get("scanned_at")
            iso = scanned_at.isoformat() if scanned_at is not None else None
            record["scanned_at_iso"] = iso
            record["scanned_time"] = format_utc_to_gmt7(iso) if iso else ""
        return records

    def _on_io_queue_changed(self, records):
        self.result_queue.put(("io_queue_snapshot", self._format_io_queue_records(records)))

    def _on_ar_io_queue_changed(self, records):
        self.result_queue.put(("ar_io_queue_snapshot", self._format_io_queue_records(records)))

    def _on_accounts_changed(self, accounts):
        self.result_queue.put(("accounts_snapshot", accounts))

    def _on_queue_accounts_changed(self, accounts):
        self.result_queue.put(("queue_accounts_snapshot", accounts))


def _enable_dpi_awareness() -> None:
    """Without this, Windows applies DPI virtualization to non-DPI-aware
    processes, which can make window geometry/sizing (e.g. maximizing)
    behave inconsistently on scaled displays."""
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - not on Windows, or API unavailable
        pass


def _set_windows_app_id() -> None:
    """Without this, Windows groups this process under the generic
    python.exe taskbar icon/grouping (since that's the actual .exe),
    regardless of what icon the Tk window itself sets via iconbitmap -
    giving the process its own AppUserModelID makes Explorer treat it as a
    distinct app, so the taskbar button picks up our icon too.

    The ID string itself was changed from the old "DRD.BufferTrackingTool"
    - on machines that ran an earlier build before the icon issues here
    were fixed, Windows can cache a wrong/missing icon against that exact
    ID (showing Tk's own default "feather" icon in the taskbar) and keep
    reusing that stale cache indefinitely even after the exe is replaced
    with a fixed one. A different ID is treated as a brand new app, so
    there's nothing stale to inherit.
    """
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "DRD.AccountingTool"
        )
    except Exception:  # noqa: BLE001 - not on Windows, or API unavailable
        pass


def main():
    _enable_dpi_awareness()
    _set_windows_app_id()
    root = tk.Tk()
    WebdbApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
