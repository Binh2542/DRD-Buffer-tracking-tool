import json
import queue
import shutil
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from webdb_client import (
    attempt_login,
    build_driver,
    check_device_prog_main,
    format_utc_to_gmt7,
)

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / "session.json"
DEVICE_LIST_FILE = BASE_DIR / "device_list.json"
PROFILE_DIR = BASE_DIR / "chrome_profile"
BASE_URL = "https://main.prod.m11g.ajax.systems/webaut/webdb/"


class WebdbApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("DRD Buffer Tracking Tool")
        self.root.geometry("700x520")

        self.driver = None
        self.username = None
        self.devices = self._load_device_list()
        self.result_queue: queue.Queue = queue.Queue()
        self.lock = threading.Lock()

        self.login_frame = None
        self.main_frame = None

        self._build_login_frame()
        self._try_auto_login()
        self.root.after(150, self._poll_results)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- persistence ----------------
    def _load_device_list(self):
        if DEVICE_LIST_FILE.exists():
            try:
                return json.loads(DEVICE_LIST_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return []
        return []

    def _save_device_list(self):
        DEVICE_LIST_FILE.write_text(
            json.dumps(self.devices, ensure_ascii=False, indent=2), encoding="utf-8"
        )

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
        self.login_frame = ttk.Frame(self.root, padding=30)
        self.login_frame.pack(fill="both", expand=True)

        ttk.Label(self.login_frame, text="Dang nhap WebDB", font=("Segoe UI", 14, "bold")).grid(
            row=0, column=0, columnspan=2, pady=(0, 15)
        )

        ttk.Label(self.login_frame, text="Username:").grid(row=1, column=0, sticky="e", pady=5)
        self.username_entry = ttk.Entry(self.login_frame, width=32)
        self.username_entry.grid(row=1, column=1, pady=5)

        ttk.Label(self.login_frame, text="Password:").grid(row=2, column=0, sticky="e", pady=5)
        self.password_entry = ttk.Entry(self.login_frame, width=32, show="*")
        self.password_entry.grid(row=2, column=1, pady=5)
        self.password_entry.bind("<Return>", lambda _e: self._on_login_click())

        self.login_status_label = ttk.Label(self.login_frame, text="", foreground="red", wraplength=320)
        self.login_status_label.grid(row=3, column=0, columnspan=2, pady=5)

        self.login_button = ttk.Button(self.login_frame, text="Login", command=self._on_login_click)
        self.login_button.grid(row=4, column=0, columnspan=2, pady=10)

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
            self.login_status_label.config(text="Vui long nhap day du username/password.")
            return
        self.login_button.config(state="disabled")
        self.login_status_label.config(text="Dang dang nhap...", foreground="blue")
        threading.Thread(target=self._do_login, args=(username, password), daemon=True).start()

    def _do_login(self, username, password):
        try:
            driver = build_driver(PROFILE_DIR, headless=True)
        except Exception as exc:  # noqa: BLE001 - infra error, not a credentials problem
            self.result_queue.put(
                ("login_result", False, f"Loi khoi dong trinh duyet: {exc}", None, username, password, False)
            )
            return
        try:
            ok = attempt_login(driver, BASE_URL, username, password)
        except Exception as exc:  # noqa: BLE001
            driver.quit()
            self.result_queue.put(
                ("login_result", False, f"Loi khi dang nhap: {exc}", None, username, password, False)
            )
            return
        if ok:
            self.result_queue.put(("login_result", True, None, driver, username, password, False))
        else:
            driver.quit()
            self.result_queue.put(
                ("login_result", False, "Sai username hoac password.", None, username, password, True)
            )

    # ---------------- main screen ----------------
    def _build_main_frame(self):
        self.main_frame = ttk.Frame(self.root, padding=10)
        self.main_frame.pack(fill="both", expand=True)

        top = ttk.Frame(self.main_frame)
        top.pack(fill="x")
        ttk.Label(top, text=f"Dang nhap: {self.username}").pack(side="left")
        ttk.Button(top, text="Logout", command=self._on_logout).pack(side="right")

        scan_frame = ttk.Frame(self.main_frame)
        scan_frame.pack(fill="x", pady=8)
        ttk.Label(scan_frame, text="Quet QR:").pack(side="left")
        self.scan_entry = ttk.Entry(scan_frame, width=40)
        self.scan_entry.pack(side="left", padx=5)
        self.scan_entry.bind("<Return>", self._on_scan_submit)

        self.status_label = ttk.Label(self.main_frame, text="San sang.")
        self.status_label.pack(fill="x")

        columns = ("id", "import_time", "defect")
        self.tree = ttk.Treeview(self.main_frame, columns=columns, show="headings", height=15)
        self.tree.heading("id", text="ID")
        self.tree.heading("import_time", text="Import Time")
        self.tree.heading("defect", text="Defect")
        self.tree.column("id", width=110)
        self.tree.column("import_time", width=190)
        self.tree.column("defect", width=340)
        self.tree.pack(fill="both", expand=True, pady=8)

        self.refresh_button = ttk.Button(self.main_frame, text="Refresh", command=self._on_refresh_click)
        self.refresh_button.pack()

        self._refresh_tree()
        self.scan_entry.focus_set()

    def _refresh_tree(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        for device in self.devices:
            self.tree.insert(
                "", "end", iid=device["qr"],
                values=(device["qr"], device["import_time"], device["defect"]),
            )

    def _set_busy(self, busy: bool, message: str = ""):
        state = "disabled" if busy else "normal"
        self.scan_entry.config(state=state)
        self.refresh_button.config(state=state)
        if message:
            self.status_label.config(text=message)
        elif not busy:
            self.status_label.config(text="San sang.")
        if not busy:
            self.scan_entry.focus_set()

    # ---- scanning ----
    def _on_scan_submit(self, _event=None):
        qr = self.scan_entry.get().strip()
        self.scan_entry.delete(0, "end")
        if not qr:
            return
        if any(d["qr"] == qr for d in self.devices):
            messagebox.showinfo("Thong bao", f"Thiet bi {qr} da co trong danh sach.")
            self.scan_entry.focus_set()
            return
        self._set_busy(True, f"Dang kiem tra {qr}...")
        threading.Thread(target=self._do_scan, args=(qr,), daemon=True).start()

    def _do_scan(self, qr):
        try:
            with self.lock:
                result = check_device_prog_main(self.driver, BASE_URL, qr)
        except Exception as exc:  # noqa: BLE001
            self.result_queue.put(("error", f"Loi khi kiem tra {qr}: {exc}"))
            return
        self.result_queue.put(("scan_result", result))

    def _on_scan_result(self, result):
        self._set_busy(False)
        qr = result["qr"]
        if result.get("error"):
            messagebox.showerror("Loi", f"{qr}: {result['error']}")
            return
        if result["passed_last_attempt"]:
            messagebox.showinfo(
                "Da PASS",
                f"Thiet bi {qr} da PASS Prog Main o lan gan nhat "
                f"({format_utc_to_gmt7(result['last_time'])}).\nKhong them vao danh sach.",
            )
            return
        device = {
            "qr": qr,
            "import_time": format_utc_to_gmt7(result["first_fail_time"]),
            "defect": result["defect_description"] or "No info",
        }
        self.devices.append(device)
        self._save_device_list()
        self._refresh_tree()

    # ---- refresh ----
    def _on_refresh_click(self):
        if not self.devices:
            messagebox.showinfo("Thong bao", "Danh sach dang trong.")
            return
        self._set_busy(True, "Dang kiem tra danh sach...")
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
        for qr, last_time in data["passed"]:
            if messagebox.askyesno(
                "Thiet bi da PASS",
                f"Thiet bi {qr} da PASS Prog Main o lan gan nhat "
                f"({format_utc_to_gmt7(last_time)}).\nXoa khoi danh sach?",
            ):
                self.devices = [d for d in self.devices if d["qr"] != qr]
        if data["errors"]:
            msg = "\n".join(f"{qr}: {err}" for qr, err in data["errors"])
            messagebox.showwarning("Mot so thiet bi loi khi kiem tra", msg)
        self._save_device_list()
        self._refresh_tree()

    # ---- logout ----
    def _on_logout(self):
        if not messagebox.askyesno("Logout", "Ban co chac muon dang xuat?"):
            return
        with self.lock:
            if self.driver:
                try:
                    self.driver.quit()
                except Exception:  # noqa: BLE001
                    pass
                self.driver = None
        self._clear_session()
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
        self.username = None
        self.main_frame.destroy()
        self.main_frame = None
        self._build_login_frame()

    def _on_close(self):
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
            _, ok, error, driver, username, password, wrong_credentials = item
            self.login_button.config(state="normal")
            if ok:
                self.driver = driver
                self.username = username
                self._save_session(username, password)
                self.login_frame.destroy()
                self.login_frame = None
                self._build_main_frame()
            else:
                self.login_status_label.config(text=error or "Dang nhap that bai.")
                if wrong_credentials:
                    self._clear_session()
        elif kind == "scan_result":
            self._on_scan_result(item[1])
        elif kind == "refresh_result":
            self._on_refresh_result(item[1])
        elif kind == "error":
            self._set_busy(False)
            messagebox.showerror("Loi", item[1])


def main():
    root = tk.Tk()
    WebdbApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
