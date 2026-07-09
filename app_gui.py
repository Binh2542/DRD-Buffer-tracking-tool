import datetime as dt
import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import matplotlib.dates as mdates
from dotenv import load_dotenv
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator

from buffer_stats import compute_daily_buffer_counts
from firestore_client import (
    add_completed_device,
    add_device,
    delete_devices,
    init_firestore,
    listen_completed_devices,
    listen_devices,
)
from pcb_choice import load_pcb_choice_rules, resolve_board_name
from prod5_sheet import init_from_sheet, record_from_scan
from sheets_export import export_completed_devices, export_devices, init_completed_sheet, init_sheet
from webdb_client import (
    attempt_login,
    build_driver,
    check_device_prog_main,
    elapsed_since,
    format_utc_to_gmt7,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

SESSION_FILE = BASE_DIR / "session.json"
FIREBASE_KEY_FILE = BASE_DIR / "firebase_key.json"
PROFILE_DIR = BASE_DIR / "chrome_profile"
BASE_URL = "https://main.prod.m11g.ajax.systems/webaut/webdb/"
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
PROD5_SHEET_ID = "1otPfRvWa2zGREGLi_5SzsOi8DyPZyeo24cVwS5bNTos"  # "Buffer Debug PROD5 VTP"

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

FONT_BASE = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_HEADER = ("Segoe UI", 15, "bold")

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
    ("id", "ID", 115, "w", False, None),
    ("board", "Board", 180, "w", False, "board"),
    ("import_time", "Import Time", 186, "center", False, "import_time"),
    ("attempt", "Attempt", 75, "center", False, "attempt"),
    ("complete_time", "Complete Time", 186, "center", False, "complete_time"),
    ("debug", "Debug", 260, "w", False, "debug"),
    ("defect", "Defect", 320, "w", True, "defect"),
]


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


class WebdbApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("DRD Buffer Tracking Tool")
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
        _setup_style(root)

        self.driver = None
        self.username = None
        self.db = None
        self.sheet = None
        self.completed_sheet = None
        self.pcb_choice_rules: list = []
        self.from_sheet = None
        self.devices_watch = None
        self.completed_watch = None
        self.devices: list[dict] = []
        self.completed_devices: list[dict] = []
        self.view_mode = "buffer"  # "buffer" | "completed" | "chart"
        self.view_buttons: dict[str, ttk.Button] = {}
        self._current_columns = BUFFER_COLUMNS
        self.chart_canvas = None
        self.chart_ax = None
        self.chart_range_days = 90
        self.chart_range_buttons: dict[int, ttk.Button] = {}
        self.result_queue: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.checked_qrs: set[str] = set()
        self.sort_column = None
        self.sort_reverse = False

        self.login_frame = None
        self.main_frame = None

        self._build_login_frame()
        self._try_auto_login()
        self.root.after(150, self._poll_results)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- persistence ----------------
    def _load_session(self):
        if SESSION_FILE.exists():
            try:
                data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
                return data.get("username"), data.get("password")
            except (json.JSONDecodeError, OSError):
                return None, None
        return None, None

    def _save_session(self, username, password):
        SESSION_FILE.write_text(
            json.dumps({"username": username, "password": password}), encoding="utf-8"
        )

    def _clear_session(self):
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()

    # ---------------- login screen ----------------
    def _build_login_frame(self):
        self.login_frame = ttk.Frame(self.root, padding=40)
        self.login_frame.pack(fill="both", expand=True)

        ttk.Label(self.login_frame, text="DRD Buffer Tracking", style="Header.TLabel").grid(
            row=0, column=0, columnspan=2, pady=(0, 4), sticky="w"
        )
        ttk.Label(self.login_frame, text="Login to WebDB", style="Muted.TLabel").grid(
            row=1, column=0, columnspan=2, pady=(0, 20), sticky="w"
        )

        ttk.Label(self.login_frame, text="Username").grid(row=2, column=0, sticky="e", padx=(0, 10), pady=6)
        self.username_entry = ttk.Entry(self.login_frame, width=30)
        self.username_entry.grid(row=2, column=1, pady=6)

        ttk.Label(self.login_frame, text="Password").grid(row=3, column=0, sticky="e", padx=(0, 10), pady=6)
        self.password_entry = ttk.Entry(self.login_frame, width=30, show="*")
        self.password_entry.grid(row=3, column=1, pady=6)
        self.password_entry.bind("<Return>", lambda _e: self._on_login_click())

        self.login_status_label = ttk.Label(self.login_frame, text="", style="Error.TLabel", wraplength=320)
        self.login_status_label.grid(row=4, column=0, columnspan=2, pady=8)

        self.login_button = ttk.Button(
            self.login_frame, text="LOGIN", style="Accent.TButton", command=self._on_login_click
        )
        self.login_button.grid(row=5, column=0, columnspan=2, pady=12, sticky="we")

    def _try_auto_login(self):
        username, password = self._load_session()
        if username and password:
            self.username_entry.insert(0, username)
            self.password_entry.insert(0, password)
            self._on_login_click()

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
        def fail(error, wrong_credentials=False):
            self.result_queue.put((
                "login_result",
                {"ok": False, "error": error, "username": username, "password": password,
                 "wrong_credentials": wrong_credentials},
            ))

        try:
            driver = build_driver(PROFILE_DIR, headless=True)
        except Exception as exc:  # noqa: BLE001 - infra error, not a credentials problem
            fail(f"Failed to start browser: {exc}")
            return
        try:
            ok = attempt_login(driver, BASE_URL, username, password)
        except Exception as exc:  # noqa: BLE001
            driver.quit()
            fail(f"Login error: {exc}")
            return
        if not ok:
            driver.quit()
            fail("Incorrect username or password.", wrong_credentials=True)
            return
        try:
            db = init_firestore(FIREBASE_KEY_FILE)
        except Exception as exc:  # noqa: BLE001
            driver.quit()
            fail(f"Failed to connect to the shared database: {exc}")
            return

        sheet = None
        completed_sheet = None
        pcb_choice_rules = []
        sheet_warning = None
        if GOOGLE_SHEET_ID:
            try:
                sheet = init_sheet(FIREBASE_KEY_FILE, GOOGLE_SHEET_ID)
                completed_sheet = init_completed_sheet(FIREBASE_KEY_FILE, GOOGLE_SHEET_ID)
                pcb_choice_rules = load_pcb_choice_rules(FIREBASE_KEY_FILE, GOOGLE_SHEET_ID)
            except Exception as exc:  # noqa: BLE001 - reporting is best-effort, don't block login
                sheet_warning = f"Could not connect to the report Google Sheet: {exc}"

        from_sheet = None
        try:
            from_sheet = init_from_sheet(FIREBASE_KEY_FILE, PROD5_SHEET_ID)
        except Exception as exc:  # noqa: BLE001 - reporting is best-effort, don't block login
            extra = f"Could not connect to the PROD5 VTP FROM sheet: {exc}"
            sheet_warning = f"{sheet_warning}\n{extra}" if sheet_warning else extra

        self.result_queue.put((
            "login_result",
            {"ok": True, "driver": driver, "db": db, "sheet": sheet, "completed_sheet": completed_sheet,
             "pcb_choice_rules": pcb_choice_rules, "from_sheet": from_sheet, "sheet_warning": sheet_warning,
             "username": username, "password": password},
        ))

    # ---------------- main screen ----------------
    def _build_main_frame(self):
        self.main_frame = ttk.Frame(self.root, padding=16)
        self.main_frame.pack(fill="both", expand=True)

        top = ttk.Frame(self.main_frame)
        top.pack(fill="x", pady=(0, 14))
        header_box = ttk.Frame(top)
        header_box.pack(side="left")
        ttk.Label(header_box, text="DRD Buffer Tracking", style="Header.TLabel").pack(anchor="w")
        user_row = ttk.Frame(header_box)
        user_row.pack(anchor="w")
        ttk.Label(user_row, text="Logged in: ", style="Muted.TLabel").pack(side="left")
        ttk.Label(user_row, text=self.username, style="Accent.TLabel").pack(side="left")
        ttk.Button(top, text="Logout", style="Danger.TButton", command=self._on_logout).pack(side="right")

        scan_frame = ttk.Frame(self.main_frame)
        scan_frame.pack(fill="x", pady=(0, 10))
        ttk.Label(scan_frame, text="Scan QR:").pack(side="left")
        self.scan_entry = ttk.Entry(scan_frame, width=40)
        self.scan_entry.pack(side="left", padx=8, fill="x", expand=True)
        self.scan_entry.bind("<Return>", self._on_scan_submit)

        self.status_label = ttk.Label(self.main_frame, text="Ready.", style="Muted.TLabel")
        self.status_label.pack(fill="x", pady=(0, 8))

        view_row = ttk.Frame(self.main_frame)
        view_row.pack(fill="x", pady=(0, 10))
        for mode, label in (("buffer", "Buffer"), ("completed", "Completed List"), ("chart", "Chart")):
            btn = ttk.Button(
                view_row, text=label,
                style="Accent.TButton" if mode == "buffer" else "Secondary.TButton",
                command=lambda m=mode: self._set_view_mode(m),
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

        button_row = ttk.Frame(self.main_frame)
        button_row.pack(fill="x", pady=(12, 0))
        self.refresh_button = ttk.Button(
            button_row, text="Refresh", style="Accent.TButton", command=self._on_refresh_click
        )
        self.refresh_button.pack(side="left", padx=(0, 8))
        self.delete_button = ttk.Button(
            button_row, text="Delete", style="Danger.TButton", command=self._on_delete_selected
        )
        self.delete_button.pack(side="left")

        self._apply_tree_columns(BUFFER_COLUMNS)
        self._refresh_tree()
        self.scan_entry.focus_set()
        self._tick_elapsed()

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

    def _set_view_mode(self, mode):
        if mode == self.view_mode:
            return
        self.view_mode = mode
        self.sort_column = None
        self.sort_reverse = False
        for m, btn in self.view_buttons.items():
            btn.config(style="Accent.TButton" if m == mode else "Secondary.TButton")

        if mode == "chart":
            self.content_frame.pack_forget()
            self._render_chart()
            self.chart_frame.pack(fill="both", expand=True)
            self.refresh_button.config(state="disabled")
            self.delete_button.config(state="disabled")
        else:
            self.chart_frame.pack_forget()
            self.content_frame.pack(fill="both", expand=True)
            self._apply_tree_columns(BUFFER_COLUMNS if mode == "buffer" else COMPLETED_COLUMNS)
            self._refresh_tree()
            state = "normal" if mode == "buffer" else "disabled"
            self.refresh_button.config(state=state)
            self.delete_button.config(state=state)

    def _refresh_tree(self):
        if self.view_mode == "chart":
            return
        for row in self.tree.get_children():
            self.tree.delete(row)
        items = self.devices if self.view_mode == "buffer" else self.completed_devices
        for i, device in enumerate(items):
            tag = "evenrow" if i % 2 == 0 else "oddrow"
            if self.view_mode == "buffer":
                values = (
                    "☑" if device["qr"] in self.checked_qrs else "☐",
                    device["qr"],
                    device.get("board", ""),
                    device.get("import_time", ""),
                    elapsed_since(device.get("import_time_iso")),
                    device.get("attempt_count", ""),
                    device.get("defect", ""),
                )
            else:
                values = (
                    device.get("qr", ""),
                    device.get("board", ""),
                    device.get("import_time", ""),
                    device.get("attempt_count", ""),
                    device.get("complete_time", ""),
                    device.get("debug_operator", "") or "No info",
                    device.get("defect", ""),
                )
            self.tree.insert("", "end", iid=device["qr"], tags=(tag,), values=values)
        self._refresh_summary()

    def _refresh_summary(self):
        for row in self.summary_tree.get_children():
            self.summary_tree.delete(row)
        counts: dict[str, int] = {}
        for device in self.devices:
            model = device.get("board") or "Unknown"
            counts[model] = counts.get(model, 0) + 1
        for i, (model, qty) in enumerate(sorted(counts.items())):
            self.summary_tree.insert(
                "", "end",
                tags=("evenrow" if i % 2 == 0 else "oddrow",),
                values=(model, qty),
            )
        self.summary_total_label.config(text=f"Total PCB: {len(self.devices)}")

    def _on_snapshot_update(self):
        """Called whenever a new Firestore snapshot (buffer or completed)
        arrives, regardless of which view is currently showing."""
        if self.view_mode == "chart":
            self._render_chart()
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
        days = self.chart_range_days
        counts = compute_daily_buffer_counts(self.devices, self.completed_devices, days=days)
        dates = [c[0] for c in counts]
        values = [c[1] for c in counts]

        range_label = {7: "last week", 30: "last month", 90: "last 3 months"}.get(days, f"last {days} days")

        ax = self.chart_ax
        ax.clear()
        ax.set_facecolor(BG)
        if dates:
            ax.plot(dates, values, color=ACCENT, linewidth=2, marker="o" if days <= 30 else None, markersize=4)
            ax.fill_between(dates, values, color=ACCENT, alpha=0.15)
        ax.set_title(f"Buffer Quantity by Day ({range_label})", color=TEXT, fontsize=12)
        ax.tick_params(colors=TEXT, labelsize=8)
        for spine in ax.spines.values():
            spine.set_color(BORDER)
        ax.grid(True, color=BORDER, linewidth=0.5, alpha=0.5)
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
        self.chart_canvas.figure.autofmt_xdate()
        self.chart_canvas.draw()

    def _tick_elapsed(self):
        """Recompute the Elapsed column every minute without a network call."""
        if self.view_mode == "buffer":
            for device in self.devices:
                if self.tree.exists(device["qr"]):
                    self.tree.set(device["qr"], "elapsed", elapsed_since(device.get("import_time_iso")))
        self.root.after(60000, self._tick_elapsed)

    def _on_tree_click(self, event):
        if self.view_mode != "buffer":
            return
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        qr = self.tree.identify_row(event.y)
        if not qr:
            return
        if qr in self.checked_qrs:
            self.checked_qrs.discard(qr)
        else:
            self.checked_qrs.add(qr)
        self.tree.set(qr, "check", "☑" if qr in self.checked_qrs else "☐")

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
        if column is None or self.view_mode not in ("buffer", "completed"):
            return
        items = self.devices if self.view_mode == "buffer" else self.completed_devices
        if column in ("elapsed", "import_time"):
            key = lambda d: d.get("import_time_iso") or ""  # noqa: E731
            # smaller elapsed = more recent import_time, so its sort direction
            # is the opposite of a plain sort on the underlying timestamp
            reverse = not self.sort_reverse if column == "elapsed" else self.sort_reverse
            items.sort(key=key, reverse=reverse)
        elif column == "attempt":
            key = lambda d: d.get("attempt_count") or 0  # noqa: E731
            items.sort(key=key, reverse=self.sort_reverse)
        elif column == "complete_time":
            key = lambda d: d.get("complete_time_iso") or ""  # noqa: E731
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
        buffer_state = state if self.view_mode == "buffer" else "disabled"
        self.refresh_button.config(state=buffer_state)
        self.delete_button.config(state=buffer_state)
        if message:
            self.status_label.config(text=message)
        elif not busy:
            self.status_label.config(text="Ready.")
        if not busy:
            self.scan_entry.focus_set()

    # ---- scanning ----
    def _on_scan_submit(self, _event=None):
        qr = self.scan_entry.get().strip()
        self.scan_entry.delete(0, "end")
        if not qr:
            return
        if any(d["qr"] == qr for d in self.devices):
            messagebox.showinfo("Notice", f"Device {qr} is already in the list.")
            self.scan_entry.focus_set()
            return
        self._set_busy(True, f"Checking {qr}...")
        threading.Thread(target=self._do_scan, args=(qr,), daemon=True).start()

    def _do_scan(self, qr):
        try:
            with self.lock:
                result = check_device_prog_main(self.driver, BASE_URL, qr)
        except Exception as exc:  # noqa: BLE001
            self.result_queue.put(("error", f"Error checking {qr}: {exc}"))
            return
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

    def _on_scan_result(self, result):
        self._set_busy(False)
        qr = result["qr"]
        if result.get("error"):
            self._show_toast(f"{qr}: {result['error']}")
            return
        if result["passed_last_attempt"]:
            messagebox.showinfo(
                "Passed",
                f"Device {qr} PASSED Prog Main on its most recent attempt "
                f"({format_utc_to_gmt7(result['last_time'])}).\nNot added to the list.",
            )
            return
        resolved_board = resolve_board_name(
            self.pcb_choice_rules, result.get("board_name"), result.get("region")
        )
        device = {
            "qr": qr,
            "board": resolved_board or "No info",
            "import_time": format_utc_to_gmt7(result["first_fail_time"]),
            "import_time_iso": result["first_fail_time"],
            "attempt_count": result.get("attempt_count"),
            "defect": result["defect_description"] or "No info",
        }
        # The Firestore listener will pick this up and refresh the table for
        # everyone (including us) - no need to touch self.devices locally.
        threading.Thread(target=add_device, args=(self.db, device), daemon=True).start()
        if self.from_sheet:
            threading.Thread(
                target=record_from_scan, args=(self.from_sheet, device["board"], result.get("color")),
                daemon=True,
            ).start()

    # ---- manual delete ----
    def _on_delete_selected(self, _event=None):
        if self.view_mode != "buffer":
            return
        selected_qrs = set(self.checked_qrs)
        if not selected_qrs:
            self._show_toast("No devices checked to delete.", kind="info")
            return
        self.checked_qrs -= selected_qrs
        self._set_busy(True, f"Deleting {len(selected_qrs)} device(s)...")
        # Remove from view immediately instead of waiting on the Firestore
        # round-trip - otherwise the rows stayed visible until the next
        # snapshot arrived, which looked like the delete had silently failed
        # and prompted a confusing second delete attempt (which then only
        # acted on whatever single row the Treeview still had selected).
        self.devices = [d for d in self.devices if d["qr"] not in selected_qrs]
        for qr in selected_qrs:
            if self.tree.exists(qr):
                self.tree.delete(qr)
        self._refresh_summary()
        threading.Thread(target=self._do_delete, args=(list(selected_qrs),), daemon=True).start()

    def _do_delete(self, qrs):
        try:
            delete_devices(self.db, qrs)
        except Exception as exc:  # noqa: BLE001
            self.result_queue.put(("error", f"Failed to delete {len(qrs)} device(s): {exc}"))
            return
        self.result_queue.put(("delete_done", None))

    # ---- refresh ----
    def _on_refresh_click(self):
        if not self.devices:
            messagebox.showinfo("Notice", "The list is empty.")
            return
        self._set_busy(True, "Checking the list...")
        threading.Thread(target=self._do_refresh, daemon=True).start()

    def _do_refresh(self):
        passed = []
        errors = []
        with self.lock:
            for device in list(self.devices):
                try:
                    result = check_device_prog_main(self.driver, BASE_URL, device["qr"])
                except Exception as exc:  # noqa: BLE001
                    errors.append((device["qr"], str(exc)))
                    continue
                if result.get("passed_last_attempt"):
                    passed.append({
                        "qr": device["qr"],
                        "last_time": result.get("last_time"),
                        "debug_operator": result.get("debug_operator"),
                        "complete_time": result.get("complete_time"),
                    })
        self.result_queue.put(("refresh_result", {"passed": passed, "errors": errors}))

    def _on_refresh_result(self, data):
        self._set_busy(False)
        to_delete = []
        completed_now = []
        for item in data["passed"]:
            qr = item["qr"]
            original = next((d for d in self.devices if d["qr"] == qr), {})
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
            threading.Thread(
                target=add_completed_device, args=(self.db, completed_device), daemon=True
            ).start()
            to_delete.append(qr)
            self.checked_qrs.discard(qr)
            completed_now.append(completed_device)
        if to_delete:
            threading.Thread(target=delete_devices, args=(self.db, to_delete), daemon=True).start()
        if completed_now:
            self._show_completed_notification(completed_now)
        if data["errors"]:
            msg = "\n".join(f"{qr}: {err}" for qr, err in data["errors"])
            messagebox.showwarning("Some devices failed to check", msg)

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
            frame, text=f"{len(completed_devices)} device(s) PASSED Prog Main and moved to Completed",
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

    # ---- logout ----
    def _on_logout(self):
        if not messagebox.askyesno("Logout", "Are you sure you want to log out?"):
            return
        if self.devices_watch:
            self.devices_watch.unsubscribe()
            self.devices_watch = None
        if self.completed_watch:
            self.completed_watch.unsubscribe()
            self.completed_watch = None
        self.db = None
        self.sheet = None
        self.completed_sheet = None
        self.pcb_choice_rules = []
        self.from_sheet = None
        self.devices = []
        self.completed_devices = []
        self.checked_qrs = set()
        self.view_mode = "buffer"
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
        self._clear_session()
        self.username = None
        self.main_frame.destroy()
        self.main_frame = None
        self._build_login_frame()

    def _on_close(self):
        if self.devices_watch:
            self.devices_watch.unsubscribe()
        if self.completed_watch:
            self.completed_watch.unsubscribe()
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
                self.sheet = data["sheet"]
                self.completed_sheet = data["completed_sheet"]
                self.pcb_choice_rules = data["pcb_choice_rules"]
                self.from_sheet = data["from_sheet"]
                self.username = data["username"]
                self._save_session(data["username"], data["password"])
                self.login_frame.destroy()
                self.login_frame = None
                self._build_main_frame()
                self.devices_watch = listen_devices(self.db, self._on_devices_changed)
                self.completed_watch = listen_completed_devices(self.db, self._on_completed_changed)
                if data["sheet_warning"]:
                    messagebox.showwarning("Report sheet", data["sheet_warning"])
            else:
                self.login_status_label.config(text=data["error"] or "Login failed.", foreground=DANGER)
                if data["wrong_credentials"]:
                    self._clear_session()
        elif kind == "devices_snapshot":
            self.devices = item[1]
            self.checked_qrs &= {d["qr"] for d in self.devices}
            self._on_snapshot_update()
            if self.sheet:
                threading.Thread(
                    target=export_devices, args=(self.sheet, list(self.devices)), daemon=True
                ).start()
        elif kind == "completed_snapshot":
            self.completed_devices = item[1]
            self._on_snapshot_update()
            if self.completed_sheet:
                threading.Thread(
                    target=export_completed_devices, args=(self.completed_sheet, item[1]), daemon=True
                ).start()
        elif kind == "scan_result":
            self._on_scan_result(item[1])
        elif kind == "refresh_result":
            self._on_refresh_result(item[1])
        elif kind == "delete_done":
            self._set_busy(False)
        elif kind == "error":
            self._set_busy(False)
            messagebox.showerror("Error", item[1])

    def _on_devices_changed(self, devices):
        """Called on Firestore's own background thread - just hand off to the
        main-thread queue, never touch Tkinter widgets from here directly."""
        self.result_queue.put(("devices_snapshot", devices))

    def _on_completed_changed(self, devices):
        self.result_queue.put(("completed_snapshot", devices))


def _enable_dpi_awareness() -> None:
    """Without this, Windows applies DPI virtualization to non-DPI-aware
    processes, which can make window geometry/sizing (e.g. maximizing)
    behave inconsistently on scaled displays."""
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - not on Windows, or API unavailable
        pass


def main():
    _enable_dpi_awareness()
    root = tk.Tk()
    WebdbApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
