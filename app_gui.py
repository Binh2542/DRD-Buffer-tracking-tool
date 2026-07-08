import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from dotenv import load_dotenv

from firestore_client import add_device, delete_devices, init_firestore, listen_devices
from sheets_export import export_devices, init_sheet
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
        self.devices_watch = None
        self.devices: list[dict] = []
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
        sheet_warning = None
        if GOOGLE_SHEET_ID:
            try:
                sheet = init_sheet(FIREBASE_KEY_FILE, GOOGLE_SHEET_ID)
            except Exception as exc:  # noqa: BLE001 - reporting is best-effort, don't block login
                sheet_warning = f"Could not connect to the report Google Sheet: {exc}"

        self.result_queue.put((
            "login_result",
            {"ok": True, "driver": driver, "db": db, "sheet": sheet, "sheet_warning": sheet_warning,
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

        content_frame = ttk.Frame(self.main_frame)
        content_frame.pack(fill="both", expand=True)

        table_frame = ttk.Frame(content_frame)
        table_frame.pack(side="left", fill="both", expand=True)

        columns = ("check", "id", "board", "import_time", "elapsed", "attempt", "defect")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=12)
        self.tree.heading("check", text="")
        self.tree.heading("id", text="ID")
        self.tree.heading("board", text="Board", command=lambda: self._sort_by("board"))
        self.tree.heading("import_time", text="Import Time", command=lambda: self._sort_by("import_time"))
        self.tree.heading("elapsed", text="Elapsed", command=lambda: self._sort_by("elapsed"))
        self.tree.heading("attempt", text="Attempt", command=lambda: self._sort_by("attempt"))
        self.tree.heading("defect", text="Defect", command=lambda: self._sort_by("defect"))
        self.tree.column("check", width=42, minwidth=42, anchor="center", stretch=False)
        self.tree.column("id", width=130, minwidth=110, stretch=False)
        self.tree.column("board", width=220, minwidth=150, stretch=False)
        self.tree.column("import_time", width=190, minwidth=160, anchor="center", stretch=False)
        self.tree.column("elapsed", width=110, minwidth=90, anchor="center", stretch=False)
        self.tree.column("attempt", width=80, minwidth=70, anchor="center", stretch=False)
        self.tree.column("defect", width=450, minwidth=220, stretch=True)

        self.tree.tag_configure("evenrow", background=FIELD_BG)
        self.tree.tag_configure("oddrow", background=PANEL_BG)

        tree_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")
        self.tree.bind("<Delete>", self._on_delete_selected)
        self.tree.bind("<Button-1>", self._on_tree_click)

        summary_frame = ttk.Frame(content_frame, padding=(16, 0, 0, 0))
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

        self._refresh_tree()
        self.scan_entry.focus_set()
        self._tick_elapsed()

    def _refresh_tree(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        for i, device in enumerate(self.devices):
            self.tree.insert(
                "", "end", iid=device["qr"],
                tags=("evenrow" if i % 2 == 0 else "oddrow",),
                values=(
                    "☑" if device["qr"] in self.checked_qrs else "☐",
                    device["qr"],
                    device.get("board", ""),
                    device["import_time"],
                    elapsed_since(device.get("import_time_iso")),
                    device.get("attempt_count", ""),
                    device["defect"],
                ),
            )
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

    def _tick_elapsed(self):
        """Recompute the Elapsed column every minute without a network call."""
        for device in self.devices:
            if self.tree.exists(device["qr"]):
                self.tree.set(device["qr"], "elapsed", elapsed_since(device.get("import_time_iso")))
        self.root.after(60000, self._tick_elapsed)

    def _on_tree_click(self, event):
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
        self._refresh_tree()

    def _apply_current_sort(self):
        """Re-apply the last-clicked sort column/direction to self.devices.

        Called both from _sort_by (user clicked a header) and whenever a new
        Firestore snapshot arrives, since the DB's own ordering wouldn't
        otherwise match whatever sort the user had selected.
        """
        column = self.sort_column
        if column is None:
            return
        if column in ("elapsed", "import_time"):
            key = lambda d: d.get("import_time_iso") or ""  # noqa: E731
            # smaller elapsed = more recent import_time, so its sort direction
            # is the opposite of a plain sort on the underlying timestamp
            reverse = not self.sort_reverse if column == "elapsed" else self.sort_reverse
            self.devices.sort(key=key, reverse=reverse)
        elif column == "attempt":
            key = lambda d: d.get("attempt_count") or 0  # noqa: E731
            self.devices.sort(key=key, reverse=self.sort_reverse)
        else:
            key = lambda d: (d.get(column) or "").lower()  # noqa: E731
            self.devices.sort(key=key, reverse=self.sort_reverse)

    def _set_busy(self, busy: bool, message: str = ""):
        state = "disabled" if busy else "normal"
        self.scan_entry.config(state=state)
        self.refresh_button.config(state=state)
        self.delete_button.config(state=state)
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

    def _on_scan_result(self, result):
        self._set_busy(False)
        qr = result["qr"]
        if result.get("error"):
            messagebox.showerror("Error", f"{qr}: {result['error']}")
            return
        if result["passed_last_attempt"]:
            messagebox.showinfo(
                "Passed",
                f"Device {qr} PASSED Prog Main on its most recent attempt "
                f"({format_utc_to_gmt7(result['last_time'])}).\nNot added to the list.",
            )
            return
        device = {
            "qr": qr,
            "board": result.get("board_name") or "No info",
            "import_time": format_utc_to_gmt7(result["first_fail_time"]),
            "import_time_iso": result["first_fail_time"],
            "attempt_count": result.get("attempt_count"),
            "defect": result["defect_description"] or "No info",
        }
        # The Firestore listener will pick this up and refresh the table for
        # everyone (including us) - no need to touch self.devices locally.
        threading.Thread(target=add_device, args=(self.db, device), daemon=True).start()

    # ---- manual delete ----
    def _on_delete_selected(self, _event=None):
        selected_qrs = self.checked_qrs or set(self.tree.selection())
        if not selected_qrs:
            messagebox.showinfo("Notice", "No devices selected to delete.")
            return
        if not messagebox.askyesno(
            "Delete devices",
            f"Delete {len(selected_qrs)} selected device(s) from the list?\n" + ", ".join(selected_qrs),
        ):
            return
        self.checked_qrs -= selected_qrs
        threading.Thread(target=delete_devices, args=(self.db, list(selected_qrs)), daemon=True).start()

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
                    passed.append((device["qr"], result.get("last_time")))
        self.result_queue.put(("refresh_result", {"passed": passed, "errors": errors}))

    def _on_refresh_result(self, data):
        self._set_busy(False)
        to_delete = []
        for qr, last_time in data["passed"]:
            if messagebox.askyesno(
                "Device passed",
                f"Device {qr} PASSED Prog Main on its most recent attempt "
                f"({format_utc_to_gmt7(last_time)}).\nRemove it from the list?",
            ):
                to_delete.append(qr)
                self.checked_qrs.discard(qr)
        if to_delete:
            threading.Thread(target=delete_devices, args=(self.db, to_delete), daemon=True).start()
        if data["errors"]:
            msg = "\n".join(f"{qr}: {err}" for qr, err in data["errors"])
            messagebox.showwarning("Some devices failed to check", msg)

    # ---- logout ----
    def _on_logout(self):
        if not messagebox.askyesno("Logout", "Are you sure you want to log out?"):
            return
        if self.devices_watch:
            self.devices_watch.unsubscribe()
            self.devices_watch = None
        self.db = None
        self.sheet = None
        self.devices = []
        self.checked_qrs = set()
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
                self.username = data["username"]
                self._save_session(data["username"], data["password"])
                self.login_frame.destroy()
                self.login_frame = None
                self._build_main_frame()
                self.devices_watch = listen_devices(self.db, self._on_devices_changed)
                if data["sheet_warning"]:
                    messagebox.showwarning("Report sheet", data["sheet_warning"])
            else:
                self.login_status_label.config(text=data["error"] or "Login failed.", foreground=DANGER)
                if data["wrong_credentials"]:
                    self._clear_session()
        elif kind == "devices_snapshot":
            self.devices = item[1]
            self.checked_qrs &= {d["qr"] for d in self.devices}
            self._apply_current_sort()
            self._refresh_tree()
            if self.sheet:
                threading.Thread(
                    target=export_devices, args=(self.sheet, list(self.devices)), daemon=True
                ).start()
        elif kind == "scan_result":
            self._on_scan_result(item[1])
        elif kind == "refresh_result":
            self._on_refresh_result(item[1])
        elif kind == "error":
            self._set_busy(False)
            messagebox.showerror("Error", item[1])

    def _on_devices_changed(self, devices):
        """Called on Firestore's own background thread - just hand off to the
        main-thread queue, never touch Tkinter widgets from here directly."""
        self.result_queue.put(("devices_snapshot", devices))


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
