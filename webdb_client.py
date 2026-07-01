import csv
import datetime as dt
import re
import time
from pathlib import Path
from urllib.parse import quote

import psutil
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

LOGIN_WAIT_SECONDS = 20
RESULT_WAIT_SECONDS = 20

_DRIVER_PATH_CACHE_FILE = Path(__file__).resolve().parent / ".chromedriver_path"


def _resolve_chromedriver_path() -> str:
    """Resolve the chromedriver binary path, caching it to disk.

    ChromeDriverManager().install() pings the network to check for a newer
    version on every call, which can be slow or hang on a flaky/proxied
    connection - unrelated to the target site's own reachability. Once we
    know a working path, reuse it instead of re-checking every time.
    """
    if _DRIVER_PATH_CACHE_FILE.exists():
        cached = _DRIVER_PATH_CACHE_FILE.read_text(encoding="utf-8").strip()
        if cached and Path(cached).exists():
            return cached
    path = ChromeDriverManager().install()
    _DRIVER_PATH_CACHE_FILE.write_text(path, encoding="utf-8")
    return path


def _kill_stale_profile_processes(profile_dir: Path) -> None:
    """Kill any leftover chrome/chromedriver processes still holding our
    dedicated automation profile (e.g. from a prior crash or force-kill).
    Only touches processes referencing this specific profile dir, so it
    never affects the user's own Chrome windows/profiles."""
    target = str(profile_dir.resolve()).lower()
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            if name not in ("chrome.exe", "chromedriver.exe"):
                continue
            cmdline = proc.info["cmdline"] or []
            if any(target in arg.lower() for arg in cmdline):
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def build_driver(profile_dir: Path, headless: bool = False) -> webdriver.Chrome:
    _kill_stale_profile_processes(profile_dir)

    options = Options()
    options.add_argument(f"--user-data-dir={profile_dir.resolve()}")
    options.add_argument("--profile-directory=Default")
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--start-maximized")

    driver_path = _resolve_chromedriver_path()
    try:
        return webdriver.Chrome(service=Service(driver_path), options=options)
    except WebDriverException:
        # Stale lock from a previous crash/force-kill - clean up and retry once.
        _kill_stale_profile_processes(profile_dir)
        time.sleep(1)
        return webdriver.Chrome(service=Service(driver_path), options=options)


def _find_username_field(driver):
    selectors = [
        "input[name='username']",
        "input[name='login']",
        "input[type='email']",
        "input[type='text']",
    ]
    for selector in selectors:
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            if element.is_displayed():
                return element
    return None


def attempt_login(driver, base_url: str, username: str, password: str) -> bool:
    """Try to log in. Returns True if a session is active afterwards, False otherwise.

    Never blocks on user input - safe to call from a background thread (GUI use).
    """
    driver.get(base_url)
    try:
        password_field = WebDriverWait(driver, LOGIN_WAIT_SECONDS).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']"))
        )
    except TimeoutException:
        return True  # no login form shown -> session already active

    if not password_field.is_displayed():
        return True

    username_field = _find_username_field(driver)
    if username_field is None:
        return False

    username_field.clear()
    username_field.send_keys(username)
    password_field.clear()
    password_field.send_keys(password)

    buttons = [
        b for b in driver.find_elements(By.CSS_SELECTOR, "button, input[type='submit']")
        if b.is_displayed()
    ]
    login_buttons = [b for b in buttons if b.text.strip().lower() == "login"]
    if login_buttons:
        login_buttons[0].click()
    elif buttons:
        buttons[0].click()
    else:
        password_field.send_keys(Keys.RETURN)

    try:
        WebDriverWait(driver, LOGIN_WAIT_SECONDS).until(EC.staleness_of(password_field))
        return True
    except TimeoutException:
        return False


def ensure_logged_in(driver, base_url: str, username: str, password: str) -> None:
    """CLI-friendly login: falls back to asking the user to log in by hand."""
    if attempt_login(driver, base_url, username, password):
        return
    print("Khong the tu dong dang nhap.")
    print("Hay dang nhap thu cong trong cua so trinh duyet, roi quay lai day va nhan Enter...")
    input()


def _click_button_by_text(driver, *labels) -> bool:
    wanted = {label.lower() for label in labels}
    for button in driver.find_elements(By.TAG_NAME, "button"):
        if button.is_displayed() and button.text.strip().lower() in wanted:
            button.click()
            return True
    return False


def _harvest_completions_modal(driver, timeout: int = 10) -> tuple[str, list[str]]:
    modal = WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.ID, "data-modal"))
    )
    headings = modal.find_elements(By.TAG_NAME, "h2")
    label = headings[0].text.strip() if headings else "unknown_step"

    completions = []
    seen_pages = set()
    for _ in range(50):  # safety cap against unexpected pagination loops
        info_json_text = ""
        show_buttons = [b for b in modal.find_elements(By.TAG_NAME, "button") if b.text.strip().lower() == "show"]
        if show_buttons:
            show_buttons[0].click()
            try:
                json_modal = WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.ID, "json-modal-bg"))
                )
                time.sleep(0.5)
                info_json_text = json_modal.text
                close_buttons = [
                    b for b in json_modal.find_elements(By.TAG_NAME, "button")
                    if b.text.strip().lower() == "close"
                ]
                if close_buttons:
                    close_buttons[0].click()
                else:
                    driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            except TimeoutException:
                pass
            try:
                WebDriverWait(driver, 5).until(
                    lambda d: not modal.find_elements(By.CSS_SELECTOR, ".animate-spin")
                )
            except TimeoutException:
                pass
            time.sleep(0.3)

        entry = modal.text
        if info_json_text:
            entry += "\n[Raw test info]\n" + info_json_text
        completions.append(entry)

        pager_divs = [
            d for d in modal.find_elements(By.TAG_NAME, "div")
            if re.fullmatch(r"\d+\s*/\s*\d+", d.text.strip())
        ]
        if not pager_divs:
            break
        current, total = (int(n) for n in re.findall(r"\d+", pager_divs[0].text))
        if current in seen_pages or current >= total:
            break
        seen_pages.add(current)

        nav_buttons = pager_divs[0].find_element(By.XPATH, "..").find_elements(By.TAG_NAME, "button")
        if len(nav_buttons) < 2:
            break
        nav_buttons[-1].click()
        time.sleep(0.15)  # client-side pagination only - the full attempt list is already loaded
        # the modal's DOM is re-rendered on page change, so old element
        # references (including `modal` itself) can go stale - re-fetch it.
        try:
            modal = driver.find_element(By.ID, "data-modal")
        except NoSuchElementException:
            break

    close_buttons = modal.find_elements(By.CSS_SELECTOR, "button.sticky")
    if close_buttons:
        close_buttons[0].click()
    else:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    time.sleep(0.2)
    return label, completions


def _collect_step_history(driver) -> list[tuple[str, list[str]]]:
    button_count = len([
        b for b in driver.find_elements(By.TAG_NAME, "button")
        if b.is_displayed() and b.text.strip().lower() == "show all completions"
    ])
    step_logs = []
    for i in range(button_count):
        buttons = [
            b for b in driver.find_elements(By.TAG_NAME, "button")
            if b.is_displayed() and b.text.strip().lower() == "show all completions"
        ]
        if i >= len(buttons):
            break
        buttons[i].click()
        try:
            label, completions = _harvest_completions_modal(driver)
            step_logs.append((label, completions))
        except TimeoutException:
            continue
    return step_logs


def _format_step_history(step_logs: list[tuple[str, list[str]]]) -> str:
    if not step_logs:
        return ""
    lines = ["", "=" * 60, "STEP COMPLETION HISTORY (all attempts, newest/oldest as shown)", "=" * 60]
    for label, completions in step_logs:
        for idx, text in enumerate(completions, start=1):
            lines.append(f"\n--- {label.upper()} attempt {idx}/{len(completions)} ---")
            lines.append(text)
    return "\n".join(lines)


def _load_device_page(driver, url: str, max_click_attempts: int = 6) -> bool:
    """Navigate to a device URL and click Load until data appears.

    Two independent things can hide "General Info":
    1. The Load button can be visually present and "clickable" before React
       has actually wired up its click handler (a hydration race) - fixed by
       clicking again in place rather than reloading the whole page.
    2. The site remembers an Expand/Collapse-All preference in the shared
       browser profile's storage. If a previous run left it collapsed, a
       fresh Load will render the device card without expanding General
       Info, so we also click "Expand All" whenever it's offered.
    """
    driver.get(url)
    try:
        WebDriverWait(driver, RESULT_WAIT_SECONDS).until(
            EC.element_to_be_clickable((By.XPATH, "//button[normalize-space(text())='Load']"))
        )
    except TimeoutException:
        pass

    for _ in range(max_click_attempts):
        if "General Info" in driver.find_element(By.TAG_NAME, "body").text:
            return True
        buttons = [
            b for b in driver.find_elements(By.XPATH, "//button[normalize-space(text())='Load']")
            if b.is_displayed()
        ]
        if buttons:
            buttons[0].click()
        _click_button_by_text(driver, "expand all")
        try:
            WebDriverWait(driver, 6).until(
                lambda d: "General Info" in d.find_element(By.TAG_NAME, "body").text
            )
            return True
        except TimeoutException:
            time.sleep(0.8)
            _click_button_by_text(driver, "expand all")

    return "General Info" in driver.find_element(By.TAG_NAME, "body").text


def fetch_device_data(driver, base_url: str, qr_code: str, include_history: bool = True) -> dict:
    target = quote(f"device:{qr_code}", safe="")
    url = f"{base_url.rstrip('/')}/?target={target}"
    _load_device_page(driver, url)

    if _click_button_by_text(driver, "expand all"):
        time.sleep(0.2)

    body_text = driver.find_element(By.TAG_NAME, "body").text

    if include_history:
        step_logs = _collect_step_history(driver)
        body_text += _format_step_history(step_logs)

    html = driver.page_source
    return {"url": url, "text": body_text, "html": html}


def _close_modal(modal, driver) -> None:
    close_buttons = modal.find_elements(By.CSS_SELECTOR, "button.sticky")
    if close_buttons:
        close_buttons[0].click()
    else:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    time.sleep(0.3)


def _wait_for_step_buttons(driver, timeout: int = 15) -> None:
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: any(
                b.is_displayed() and b.text.strip().lower() == "show all completions"
                for b in d.find_elements(By.TAG_NAME, "button")
            )
        )
    except TimeoutException:
        pass


def _open_step_modal(driver, step_label: str, timeout: int = 10):
    _wait_for_step_buttons(driver)
    buttons = [
        b for b in driver.find_elements(By.TAG_NAME, "button")
        if b.is_displayed() and b.text.strip().lower() == "show all completions"
    ]
    for button in buttons:
        button.click()
        try:
            modal = WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.ID, "data-modal"))
            )
        except TimeoutException:
            continue
        headings = modal.find_elements(By.TAG_NAME, "h2")
        heading = headings[0].text.strip().lower() if headings else ""
        if heading == step_label.lower():
            return modal
        _close_modal(modal, driver)
    return None


def _modal_field(modal, label: str):
    lines = modal.text.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == label:
            return lines[i + 1].strip() if i + 1 < len(lines) else None
        if stripped.startswith(label):
            rest = stripped[len(label):].strip()
            if rest:
                return rest
    return None


def _modal_page_info(modal):
    pager_divs = [
        d for d in modal.find_elements(By.TAG_NAME, "div")
        if re.fullmatch(r"\d+\s*/\s*\d+", d.text.strip())
    ]
    if not pager_divs:
        return None, None, None
    current, total = (int(n) for n in re.findall(r"\d+", pager_divs[0].text))
    return current, total, pager_divs[0]


def _modal_next_page(pager_div) -> bool:
    nav_buttons = pager_div.find_element(By.XPATH, "..").find_elements(By.TAG_NAME, "button")
    if len(nav_buttons) < 2:
        return False
    nav_buttons[-1].click()
    time.sleep(0.15)  # client-side pagination only - the full attempt list is already loaded
    return True


def _reveal_modal_description(driver, modal):
    show_buttons = [b for b in modal.find_elements(By.TAG_NAME, "button") if b.text.strip().lower() == "show"]
    if not show_buttons:
        return None
    show_buttons[0].click()
    try:
        json_modal = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.ID, "json-modal-bg"))
        )
        time.sleep(0.3)
        close_buttons = [
            b for b in json_modal.find_elements(By.TAG_NAME, "button")
            if b.text.strip().lower() == "close"
        ]
        if close_buttons:
            close_buttons[0].click()
        else:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except TimeoutException:
        pass
    try:
        WebDriverWait(driver, 5).until(
            lambda d: not modal.find_elements(By.CSS_SELECTOR, ".animate-spin")
        )
    except TimeoutException:
        pass
    time.sleep(0.3)
    moment = _modal_field(modal, "Moment:")
    description = _modal_field(modal, "Description:")
    if moment and description:
        return f"{moment}: {description}"
    return description or moment


def format_utc_to_gmt7(iso_time) -> str:
    if not iso_time:
        return iso_time
    try:
        parsed = dt.datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
    except ValueError:
        return iso_time
    local = parsed.astimezone(dt.timezone(dt.timedelta(hours=7)))
    return local.strftime("%Y-%m-%d %H:%M:%S") + " GMT+7"


def check_device_prog_main(driver, base_url: str, qr_code: str) -> dict:
    """Status of the most recent Prog Main attempt for a device.

    If the latest attempt failed, walks back through history to find the
    timestamp of the first attempt in the current run of consecutive
    failures - i.e. when this defect first appeared ("import time").
    """
    target = quote(f"device:{qr_code}", safe="")
    url = f"{base_url.rstrip('/')}/?target={target}"
    _load_device_page(driver, url)

    modal = _open_step_modal(driver, "prog_main")
    if modal is None:
        time.sleep(2)  # some steps' buttons can mount slightly later than others
        modal = _open_step_modal(driver, "prog_main")
    if modal is None:
        return {"qr": qr_code, "error": "Khong tim thay lich su Prog Main cho thiet bi nay."}

    success = _modal_field(modal, "Success:")
    last_time = _modal_field(modal, "Time:")
    passed = success == "✅"

    if passed:
        _close_modal(modal, driver)
        return {
            "qr": qr_code,
            "passed_last_attempt": True,
            "last_time": last_time,
            "defect_description": None,
            "first_fail_time": None,
        }

    defect_description = _reveal_modal_description(driver, modal)
    first_fail_time = last_time

    while True:
        # The modal's DOM can be re-rendered by React at any point (e.g. after
        # revealing the description, or turning a page), which invalidates
        # any previously-held element references - always re-fetch it fresh.
        try:
            modal = driver.find_element(By.ID, "data-modal")
        except NoSuchElementException:
            break
        try:
            current, total, pager_div = _modal_page_info(modal)
        except StaleElementReferenceException:
            continue
        if current is None or current >= total:
            break
        if not _modal_next_page(pager_div):
            break
        try:
            modal = driver.find_element(By.ID, "data-modal")
        except NoSuchElementException:
            break
        if _modal_field(modal, "Success:") == "✅":
            break
        first_fail_time = _modal_field(modal, "Time:")

    _close_modal(modal, driver)
    return {
        "qr": qr_code,
        "passed_last_attempt": False,
        "last_time": last_time,
        "defect_description": defect_description,
        "first_fail_time": first_fail_time,
    }


def save_result(output_dir: Path, qr_code: str, result: dict) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_qr = "".join(c if c.isalnum() else "_" for c in qr_code)

    html_path = output_dir / f"{timestamp}_{safe_qr}.html"
    html_path.write_text(result["html"], encoding="utf-8")

    text_path = output_dir / f"{timestamp}_{safe_qr}.txt"
    text_path.write_text(result["text"], encoding="utf-8")

    log_path = output_dir / "scan_log.csv"
    is_new = not log_path.exists()
    with log_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["timestamp", "qr_code", "url", "text_file", "html_file"])
        writer.writerow([
            timestamp,
            qr_code,
            result["url"],
            text_path.name,
            html_path.name,
        ])
    return text_path
