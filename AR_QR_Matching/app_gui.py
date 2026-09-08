"""AR_QR_Matching - a small two-tab tool for pairing a PCB with its plastic
case by QR code.

Workflow:
  Scan tab  - scan every QR in the batch, in physical order. Each scan gets
              a position number (1, 2, 3, ...).
  Match tab - scan QRs again, one at a time (in any order); each scan shows
              a big "Position X" so the operator can find/verify the
              matching part, and that row's status flips to "Checked" so
              it's obviously already handled.

Same dark/green theme and window-icon handling as DRD Accounting Tool
(this project's sibling tool) for a consistent look, but fully standalone -
no shared code, no cloud backend, nothing persisted between runs beyond one
in-memory session.
"""
import re
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from serial_scanner import SerialScanner, list_serial_ports

APP_TITLE = "AR_QR_Matching"
APP_VERSION = "V1.0.0"


def _frozen_base_dir() -> Path:
    """Where assets/ lives when packaged - next to the actual executable,
    not inside PyInstaller's temp extraction dir (see DRD Accounting
    Tool's app_gui.py for the same reasoning, this only differs in never
    needing the macOS .app-bundle correction since this tool doesn't ship
    one)."""
    return Path(sys.executable).resolve().parent


BASE_DIR = _frozen_base_dir() if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def _resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", BASE_DIR))
    return base / relative


ICON_PNG = _resource_path("assets/icon.png")
ICON_ICO = _resource_path("assets/icon.ico")
# Remembers the last serial QR scanner port selected (e.g. "/dev/ttyACM0"
# on Linux) across restarts - same pattern DRD Accounting Tool uses.
SERIAL_PORT_CACHE_FILE = BASE_DIR / ".serial_port"

# ---------------- dark / green theme (matches DRD Accounting Tool) ----------------
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
CHECKED_BG = "#20262f"  # a visibly-darker-than-normal row fill for already-matched entries

FONT_BASE = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_HEADER = ("Segoe UI", 15, "bold")
FONT_BIG = ("Segoe UI", 64, "bold")
FONT_BIG_SUB = ("Segoe UI", 14)

# Same reasoning as DRD Accounting Tool: a barcode scanner emulating fast
# keystrokes can come out mangled if a Vietnamese input editor (Unikey etc.)
# is switched on - reject anything outside this charset up front rather
# than silently recording garbage as a real QR.
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
    style.configure("Big.TLabel", background=BG, foreground=MUTED, font=FONT_BIG)
    style.configure("BigSub.TLabel", background=BG, foreground=MUTED, font=FONT_BIG_SUB)

    style.configure(
        "TEntry",
        fieldbackground=FIELD_BG, foreground=TEXT, insertcolor=TEXT,
        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
        borderwidth=1, padding=8,
    )
    style.map(
        "TEntry",
        bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)],
    )

    style.configure(
        "Accent.TButton", background=ACCENT, foreground=ACCENT_TEXT, font=FONT_BOLD,
        padding=(14, 8), borderwidth=0, focuscolor=ACCENT,
    )
    style.map(
        "Accent.TButton",
        background=[("active", ACCENT_DARK), ("disabled", "#2a303a")],
        foreground=[("disabled", MUTED)],
    )

    style.configure(
        "Secondary.TButton", background=PANEL_BG, foreground=TEXT, font=FONT_BASE,
        padding=(12, 7), borderwidth=1, bordercolor=BORDER, focuscolor=PANEL_BG,
    )
    style.map(
        "Secondary.TButton",
        background=[("active", FIELD_BG), ("disabled", PANEL_BG)],
        foreground=[("disabled", MUTED)], bordercolor=[("active", ACCENT)],
    )

    style.configure(
        "Danger.TButton", background=PANEL_BG, foreground=DANGER, font=FONT_BASE,
        padding=(12, 7), borderwidth=1, bordercolor=BORDER, focuscolor=PANEL_BG,
    )
    style.map(
        "Danger.TButton",
        background=[("active", "#2a1c1f"), ("disabled", PANEL_BG)], bordercolor=[("active", DANGER)],
    )

    style.configure(
        "Treeview", background=FIELD_BG, fieldbackground=FIELD_BG, foreground=TEXT,
        rowheight=30, borderwidth=0, font=FONT_BASE,
    )
    style.configure(
        "Treeview.Heading", background=PANEL_BG, foreground=ACCENT, font=FONT_BOLD,
        relief="flat", borderwidth=0, padding=(8, 8),
    )
    style.map("Treeview.Heading", background=[("active", FIELD_BG)])
    style.map("Treeview", background=[("selected", SELECT_BG)], foreground=[("selected", TEXT)])

    style.configure("Vertical.TScrollbar", background=PANEL_BG, troughcolor=BG, bordercolor=BORDER, arrowcolor=TEXT)

    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure(
        "TNotebook.Tab", background=PANEL_BG, foreground=MUTED, font=FONT_BOLD,
        padding=(18, 10), borderwidth=0,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", FIELD_BG)],
        foreground=[("selected", ACCENT)],
    )


def _apply_window_icon(root: tk.Tk) -> None:
    """Cosmetic only - never let an icon problem block startup."""
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
    """Windows 10 (20H1+)/11 only - matches DRD Accounting Tool's title bar."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        dwmapi = ctypes.windll.dwmapi
        value = ctypes.c_int(1)
        for attribute in (20, 19):
            dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:  # noqa: BLE001
        pass


class MatchingApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_TITLE} {APP_VERSION}")
        self.root.geometry("980x680")
        self.root.minsize(760, 560)

        self.scan_list: list[str] = []  # QR codes in scanned order - index+1 is the position
        self.matched: set[str] = set()  # QRs already confirmed in the Match tab

        self.serial_scanner: SerialScanner | None = None
        self.serial_port_combo = None
        self.serial_connect_button = None
        self.serial_status_label = None
        self._serial_autoconnect_attempted = False

        _setup_style(self.root)
        _apply_window_icon(self.root)
        _apply_dark_titlebar(self.root)

        self._build_layout()
        self._refresh_serial_ports()
        self._restore_serial_scanner_state()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(50, self.scan_entry.focus_set)

    # ---------------- layout ----------------
    def _build_layout(self):
        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=18, pady=(16, 8))
        ttk.Label(header, text=APP_TITLE, style="Header.TLabel").pack(side="left")
        ttk.Button(
            header, text="New Session", style="Danger.TButton", command=self._new_session,
        ).pack(side="right")

        serial_frame = ttk.Frame(self.root)
        serial_frame.pack(fill="x", padx=18, pady=(0, 10))
        ttk.Label(serial_frame, text="Serial Scanner Port:", style="Muted.TLabel").pack(side="left")
        self.serial_port_combo = ttk.Combobox(serial_frame, width=22)
        self.serial_port_combo.pack(side="left", padx=8)
        ttk.Button(
            serial_frame, text="⟳", width=3, style="Secondary.TButton", command=self._refresh_serial_ports,
        ).pack(side="left")
        self.serial_connect_button = ttk.Button(
            serial_frame, text="Connect", style="Secondary.TButton", command=self._on_serial_scanner_toggle,
        )
        self.serial_connect_button.pack(side="left", padx=(8, 0))
        self.serial_status_label = ttk.Label(serial_frame, text="Not connected", style="Muted.TLabel")
        self.serial_status_label.pack(side="left", padx=(12, 0))

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self.scan_tab = ttk.Frame(self.notebook)
        self.match_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.scan_tab, text="Scan")
        self.notebook.add(self.match_tab, text="Match")

        self._build_scan_tab()
        self._build_match_tab()

    def _build_scan_tab(self):
        frame = self.scan_tab
        top = ttk.Frame(frame)
        top.pack(fill="x", padx=4, pady=(16, 10))
        ttk.Label(top, text="Scan each QR (PCB or case) in order:", style="TLabel").pack(anchor="w", pady=(0, 6))

        entry_row = ttk.Frame(top)
        entry_row.pack(fill="x")
        self.scan_entry = ttk.Entry(entry_row, font=("Segoe UI", 13))
        self.scan_entry.pack(side="left", fill="x", expand=True, ipady=4)
        self.scan_entry.bind("<Return>", self._on_scan_submit)
        ttk.Button(
            entry_row, text="Complete Scanning →", style="Accent.TButton", command=self._complete_scanning,
        ).pack(side="left", padx=(10, 0))

        self.scan_count_label = ttk.Label(frame, text="Total scanned: 0", style="Muted.TLabel")
        self.scan_count_label.pack(anchor="w", padx=4, pady=(4, 10))

        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill="both", expand=True, padx=4)
        self.scan_tree = ttk.Treeview(
            tree_frame, columns=("pos", "qr"), show="headings", selectmode="browse",
        )
        self.scan_tree.heading("pos", text="Position")
        self.scan_tree.heading("qr", text="QR Code")
        self.scan_tree.column("pos", width=100, anchor="center", stretch=False)
        self.scan_tree.column("qr", width=400, anchor="w", stretch=True)
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.scan_tree.yview)
        self.scan_tree.configure(yscrollcommand=scrollbar.set)
        self.scan_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _build_match_tab(self):
        frame = self.match_tab
        top = ttk.Frame(frame)
        top.pack(fill="x", padx=4, pady=(16, 10))
        ttk.Label(top, text="Scan a QR to find its position:", style="TLabel").pack(anchor="w", pady=(0, 6))

        self.match_entry = ttk.Entry(top, font=("Segoe UI", 13))
        self.match_entry.pack(fill="x", ipady=4)
        self.match_entry.bind("<Return>", self._on_match_submit)

        result_box = ttk.Frame(frame)
        result_box.pack(fill="x", padx=4, pady=(14, 6))
        self.position_label = ttk.Label(result_box, text="—", style="Big.TLabel", anchor="center")
        self.position_label.pack(fill="x")
        self.position_sub_label = ttk.Label(
            result_box, text="Scan a QR above to see its position", style="BigSub.TLabel", anchor="center",
        )
        self.position_sub_label.pack(fill="x")

        self.match_count_label = ttk.Label(frame, text="Checked: 0 / 0", style="Muted.TLabel")
        self.match_count_label.pack(anchor="w", padx=4, pady=(10, 10))

        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill="both", expand=True, padx=4)
        self.match_tree = ttk.Treeview(
            tree_frame, columns=("pos", "qr", "status"), show="headings", selectmode="browse",
        )
        self.match_tree.heading("pos", text="Position")
        self.match_tree.heading("qr", text="QR Code")
        self.match_tree.heading("status", text="Status")
        self.match_tree.column("pos", width=100, anchor="center", stretch=False)
        self.match_tree.column("qr", width=340, anchor="w", stretch=True)
        self.match_tree.column("status", width=140, anchor="center", stretch=False)
        self.match_tree.tag_configure("checked", background=CHECKED_BG, foreground=MUTED)
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.match_tree.yview)
        self.match_tree.configure(yscrollcommand=scrollbar.set)
        self.match_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    # ---------------- scan tab behavior ----------------
    def _on_scan_submit(self, _event=None):
        qr = self.scan_entry.get().strip()
        self.scan_entry.delete(0, "end")
        if not qr:
            return
        if not _VALID_QR_RE.match(qr):
            messagebox.showwarning("Invalid QR", f'"{qr}" doesn\'t look like a valid QR code.')
            self.scan_entry.focus_set()
            return
        if qr in self.scan_list:
            position = self.scan_list.index(qr) + 1
            messagebox.showwarning("Duplicate QR", f'"{qr}" was already scanned (position {position}).')
            self.scan_entry.focus_set()
            return
        self.scan_list.append(qr)
        self._refresh_scan_tree()
        self.scan_entry.focus_set()

    def _refresh_scan_tree(self):
        self.scan_tree.delete(*self.scan_tree.get_children())
        for i, qr in enumerate(self.scan_list, start=1):
            self.scan_tree.insert("", "end", iid=qr, values=(i, qr))
        if self.scan_list:
            self.scan_tree.see(self.scan_list[-1])
        self.scan_count_label.config(text=f"Total scanned: {len(self.scan_list)}")

    def _complete_scanning(self):
        if not self.scan_list:
            messagebox.showinfo("Notice", "Scan at least one QR code first.")
            return
        self.notebook.select(self.match_tab)

    # ---------------- match tab behavior ----------------
    def _on_match_submit(self, _event=None):
        qr = self.match_entry.get().strip()
        self.match_entry.delete(0, "end")
        if not qr:
            return
        if not _VALID_QR_RE.match(qr):
            self._show_position_result(f'"{qr}" is not a valid QR', DANGER, "")
            self.match_entry.focus_set()
            return
        if not self.scan_list:
            self._show_position_result("No QR list yet", DANGER, "Scan QRs in the Scan tab first")
            self.match_entry.focus_set()
            return
        if qr not in self.scan_list:
            self._show_position_result("NOT FOUND", DANGER, f'"{qr}" is not in the scanned list')
            self.match_entry.focus_set()
            return

        position = self.scan_list.index(qr) + 1
        already_checked = qr in self.matched
        self.matched.add(qr)

        if already_checked:
            self._show_position_result(f"Position {position}", MUTED, "Already checked - scanned again")
        else:
            self._show_position_result(f"Position {position}", ACCENT, "")

        self._refresh_match_tree(select_qr=qr)
        self.match_entry.focus_set()

    def _show_position_result(self, text, color, sub_text):
        self.position_label.config(text=text, foreground=color)
        self.position_sub_label.config(text=sub_text)

    def _refresh_match_tree(self, select_qr=None):
        self.match_tree.delete(*self.match_tree.get_children())
        for i, qr in enumerate(self.scan_list, start=1):
            checked = qr in self.matched
            status = "✓ Checked" if checked else "Pending"
            tags = ("checked",) if checked else ()
            self.match_tree.insert("", "end", iid=qr, values=(i, qr, status), tags=tags)
        self.match_count_label.config(text=f"Checked: {len(self.matched)} / {len(self.scan_list)}")
        if select_qr and self.match_tree.exists(select_qr):
            self.match_tree.selection_set(select_qr)
            self.match_tree.see(select_qr)

    # ---------------- serial scanner ----------------
    def _refresh_serial_ports(self):
        ports = list_serial_ports()
        current = self.serial_port_combo.get()
        self.serial_port_combo["values"] = ports
        if current:
            self.serial_port_combo.set(current)
        elif ports:
            self.serial_port_combo.set(ports[0])

    def _restore_serial_scanner_state(self):
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
            messagebox.showwarning("Serial Scanner", "Choose or type a serial port first.")
            return
        self._connect_serial_scanner(port, silent_on_failure=False)

    def _connect_serial_scanner(self, port: str, silent_on_failure: bool):
        scanner = SerialScanner(self.root, port, on_scan=self._on_serial_scan)
        try:
            scanner.start()
        except Exception as exc:  # noqa: BLE001 - bad/missing port, already in use, no permission, etc.
            if not silent_on_failure:
                messagebox.showerror("Serial Scanner", f"Could not open {port}: {exc}")
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
        the same path a manually-typed QR + Enter uses, routed to whichever
        tab is actually showing right now so a scan always lands wherever
        the operator is currently looking."""
        if str(self.notebook.select()) == str(self.match_tab):
            self.match_entry.delete(0, "end")
            self.match_entry.insert(0, qr)
            self._on_match_submit()
        else:
            self.scan_entry.delete(0, "end")
            self.scan_entry.insert(0, qr)
            self._on_scan_submit()

    def _on_close(self):
        if self.serial_scanner is not None:
            self.serial_scanner.stop()
        self.root.destroy()

    # ---------------- shared ----------------
    def _on_tab_changed(self, _event=None):
        current = self.notebook.select()
        if current == str(self.match_tab):
            self._refresh_match_tree()
            self.root.after(50, self.match_entry.focus_set)
        elif current == str(self.scan_tab):
            self.root.after(50, self.scan_entry.focus_set)

    def _new_session(self):
        if not self.scan_list and not self.matched:
            return
        if not messagebox.askyesno(
            "New Session", "Clear the current scan/match list and start over?",
        ):
            return
        self.scan_list.clear()
        self.matched.clear()
        self._refresh_scan_tree()
        self._refresh_match_tree()
        self._show_position_result("—", MUTED, "Scan a QR above to see its position")
        self.notebook.select(self.scan_tab)
        self.scan_entry.focus_set()


def main():
    root = tk.Tk()
    MatchingApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
