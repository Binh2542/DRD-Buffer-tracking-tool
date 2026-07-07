import csv
import datetime as dt
import re
import time
from pathlib import Path
from urllib.parse import quote

import psutil
import requests
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
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
        # Wait for whichever renders first: a login form (not authenticated)
        # or any button on the already-authenticated page (e.g. "Load"). Only
        # waiting on the password field would block for the full timeout on
        # every already-logged-in launch, since it never appears in that case.
        WebDriverWait(driver, LOGIN_WAIT_SECONDS).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, "input[type='password']")
            or d.find_elements(By.TAG_NAME, "button")
        )
    except TimeoutException:
        return True  # page never rendered anything - nothing we can do, assume session valid

    password_fields = [
        f for f in driver.find_elements(By.CSS_SELECTOR, "input[type='password']") if f.is_displayed()
    ]
    if not password_fields:
        return True  # no login form shown -> session already active
    password_field = password_fields[0]

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


def format_utc_to_gmt7(iso_time) -> str:
    if not iso_time:
        return iso_time
    try:
        parsed = dt.datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
    except ValueError:
        return iso_time
    local = parsed.astimezone(dt.timezone(dt.timedelta(hours=7)))
    return local.strftime("%Y-%m-%d %H:%M:%S") + " GMT+7"


def elapsed_since(iso_time) -> str:
    """Human-readable elapsed time from an ISO UTC timestamp to now, e.g. '2d 5h 30m'."""
    if not iso_time:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
    except ValueError:
        return ""
    delta = dt.datetime.now(dt.timezone.utc) - parsed
    total_minutes = max(0, int(delta.total_seconds() // 60))
    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    return f"{days}d {hours}h {minutes}m"


def _api_base_url(base_url: str) -> str:
    origin = base_url.split("/webaut/")[0]
    return f"{origin}/core-db/api/v1"


def _api_session_from_driver(driver) -> requests.Session:
    """Build a requests session reusing the browser's auth cookies.

    The site's data endpoints are plain JSON REST APIs behind cookie auth -
    calling them directly with requests is dramatically faster than driving
    the React UI (no page render/hydration, no button clicks), matching the
    speed of looking a device up by hand in a real browser.
    """
    session = requests.Session()
    for cookie in driver.get_cookies():
        session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"])
    session.headers.update({"Accept": "application/json, text/plain, */*"})
    return session


def check_device_prog_main(driver, base_url: str, qr_code: str) -> dict:
    """Status of the most recent Prog Main attempt for a device.

    If the latest attempt failed, walks back through history to find the
    timestamp of the first attempt in the current run of consecutive
    failures - i.e. when this defect first appeared ("import time").

    Calls the site's REST API directly (same one the "Prog Main" panel and
    its "Show all completions" history use) instead of driving the browser
    UI, which is both far faster and immune to the page's own hydration
    quirks.
    """
    session = _api_session_from_driver(driver)
    api_base = _api_base_url(base_url)

    board_name = None
    try:
        dev_response = session.get(f"{api_base}/general/dev/{qr_code}/", timeout=15)
        if dev_response.ok:
            board_name = dev_response.json().get("board_name")
    except requests.RequestException:
        pass

    try:
        response = session.get(
            f"{api_base}/prog/dev/",
            params={"qr": qr_code, "time_ordered": "true", "step_type": "prog_main"},
            timeout=15,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
    except requests.RequestException as exc:
        return {"qr": qr_code, "error": f"Loi goi API: {exc}"}

    if not results:
        return {"qr": qr_code, "error": "Khong tim thay lich su Prog Main cho thiet bi nay."}

    latest = results[0]
    last_time = latest.get("time")
    passed = bool(latest.get("success"))

    if passed:
        return {
            "qr": qr_code,
            "passed_last_attempt": True,
            "last_time": last_time,
            "defect_description": None,
            "first_fail_time": None,
            "board_name": board_name,
        }

    fail_reasons = (latest.get("info") or {}).get("fail_reasons") or []
    if fail_reasons:
        moment = fail_reasons[0].get("moment")
        reason = fail_reasons[0].get("reason")
        defect_description = f"{moment}: {reason}" if moment and reason else (reason or moment)
    else:
        defect_description = None

    first_fail_time = last_time
    attempt_count = 1
    for attempt in results[1:]:
        if attempt.get("success"):
            break
        first_fail_time = attempt.get("time")
        attempt_count += 1

    return {
        "qr": qr_code,
        "passed_last_attempt": False,
        "last_time": last_time,
        "defect_description": defect_description,
        "first_fail_time": first_fail_time,
        "board_name": board_name,
        "attempt_count": attempt_count,
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
