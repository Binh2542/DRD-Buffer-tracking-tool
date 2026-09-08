import csv
import datetime as dt
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import psutil
import requests
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    SessionNotCreatedException,
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


def _base_dir() -> Path:
    """Where this cache file (and .env/firebase_key.json - see app_gui.py's
    matching _frozen_base_dir) should live when packaged: next to the
    actual executable, not inside PyInstaller's temp extraction dir -
    __file__ for a frozen/bundled module resolves inside that temp dir,
    a fresh one every launch, so a cache file placed there would never
    actually persist between runs (silently defeating the whole point of
    caching this and forcing a slow network re-check on every login).

    On Windows/Linux, sys.executable already points straight at the
    running executable - .parent is exactly right. On macOS,
    sys.executable is buried inside the .app bundle's own internals
    (Foo.app/Contents/MacOS/Foo), three levels below the folder the app
    otherwise treats as "next to the executable" - walk back up past
    Contents/MacOS/Foo.app to correct for that.
    """
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent
    exe_path = Path(sys.executable).resolve()
    if sys.platform == "darwin" and exe_path.match("*.app/Contents/MacOS/*"):
        return exe_path.parents[2].parent
    return exe_path.parent


_DRIVER_PATH_CACHE_FILE = _base_dir() / ".chromedriver_path"


def _resolve_chromedriver_path(force_refresh: bool = False) -> str:
    """Resolve the chromedriver binary path, caching it to disk.

    ChromeDriverManager().install() pings the network to check for a newer
    version on every call, which can be slow or hang on a flaky/proxied
    connection - unrelated to the target site's own reachability. Once we
    know a working path, reuse it instead of re-checking every time.

    The cache is only ever validated by file *existence*, not by whether it
    still matches the locally installed Chrome - Chrome auto-updates itself
    silently, so a path cached before an update becomes version-incompatible
    without ever going missing. `force_refresh` lets build_driver bypass a
    known-stale cache entry (see its SessionNotCreatedException handling)
    instead of being stuck retrying the same broken path forever.
    """
    if not force_refresh and _DRIVER_PATH_CACHE_FILE.exists():
        cached = _DRIVER_PATH_CACHE_FILE.read_text(encoding="utf-8").strip()
        if cached and Path(cached).exists():
            return cached
    path = ChromeDriverManager().install()
    _DRIVER_PATH_CACHE_FILE.write_text(path, encoding="utf-8")
    return path


_CHROME_PROCESS_NAMES = (
    "chrome.exe", "chromedriver.exe",  # Windows
    "google chrome", "google chrome helper", "chromedriver",  # macOS
    "chrome", "chromium",  # generic/Linux
)


def _kill_stale_profile_processes(profile_dir: Path) -> None:
    """Kill any leftover chrome/chromedriver processes still holding our
    dedicated automation profile (e.g. from a prior crash or force-kill).
    Only touches processes referencing this specific profile dir, so it
    never affects the user's own Chrome windows/profiles."""
    target = str(profile_dir.resolve()).lower()
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            if name not in _CHROME_PROCESS_NAMES:
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
    except SessionNotCreatedException:
        # Cached driver no longer matches the installed Chrome (e.g. Chrome
        # auto-updated since we last cached this path) - retrying with the
        # SAME path would just fail identically forever. Force a fresh
        # ChromeDriverManager().install() and retry once with that instead.
        driver_path = _resolve_chromedriver_path(force_refresh=True)
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


def parse_gmt7_formatted_to_iso(formatted: str):
    """Reverse of format_utc_to_gmt7 - recovers a UTC ISO-8601 timestamp
    from its "YYYY-MM-DD HH:MM:SS GMT+7" display form (returns None if it
    doesn't parse). Used when a record was loaded from a Google Sheet (see
    sheets_export.py's read_*_from_sheet functions) instead of Firestore -
    the sheet only ever stores this formatted display string, never the
    original *_time_iso field, so every consumer that sorts/filters/
    computes elapsed time by *_time_iso (app_gui.py's column sorting,
    Chart/Summary's date-range filtering and cycle-time calculation,
    Buffer's "elapsed" column) would otherwise silently go blank for
    sheet-loaded records. Precision is to the second (the display
    string's own precision) - plenty for all of those, none of which need
    sub-second precision."""
    if not formatted:
        return None
    try:
        naive = dt.datetime.strptime(formatted.replace(" GMT+7", ""), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    aware_gmt7 = naive.replace(tzinfo=dt.timezone(dt.timedelta(hours=7)))
    return aware_gmt7.astimezone(dt.timezone.utc).isoformat()


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


def build_api_session(driver) -> requests.Session:
    """Build a requests session reusing the browser's auth cookies.

    The site's data endpoints are plain JSON REST APIs behind cookie auth -
    calling them directly with requests is dramatically faster than driving
    the React UI (no page render/hydration, no button clicks), matching the
    speed of looking a device up by hand in a real browser.

    Extracting cookies from Selenium is itself a CDP round-trip, so callers
    checking many devices in a row (e.g. a bulk refresh) should build this
    session once and reuse it via check_device_prog_main_session, rather
    than calling check_device_prog_main (which rebuilds it every time) in a
    loop.
    """
    session = requests.Session()
    for cookie in driver.get_cookies():
        session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"])
    session.headers.update({"Accept": "application/json, text/plain, */*"})
    return session


def _get_last_step_info(
    session, api_base: str, qr_code: str, step_name: str, dev_id: str = None, entity: str = "dev"
):
    """Most recent record for a given step (e.g. 'repair') via the
    last_steps API - used to attribute who fixed a device and when.
    This endpoint lives under api/v2, unlike the rest of this module's v1 calls.

    `entity` selects "dev" (Product) or "component" - components have no
    numeric id exposed by general/component/, so `dev_id` is only ever sent
    for the "dev" case.
    """
    api_base_v2 = api_base.replace("/api/v1", "/api/v2")
    try:
        response = session.post(
            f"{api_base_v2}/last_steps/{entity}/",
            json={"ids": [dev_id] if dev_id else [], "qrs": [qr_code], "include_reassigned_qr": True},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException:
        return None
    if not data:
        return None
    for step in data[0].get("last_steps", []):
        if step.get("step") == step_name:
            return {"operator": step.get("operator"), "time": step.get("time")}
    return None


def _log_attempt(diagnostics, endpoint: str, response=None, exc: Exception = None) -> None:
    """Append one sub-endpoint outcome to `diagnostics` (a plain list the
    caller owns) for later logging to Firestore if the overall lookup ends
    up failing - a no-op whenever `diagnostics` is None (the default, so
    normal lookups pay zero cost for this)."""
    if diagnostics is None:
        return
    entry = {"endpoint": endpoint, "time": dt.datetime.now(dt.timezone.utc).isoformat()}
    if exc is not None:
        entry["error"] = f"{type(exc).__name__}: {exc}"
    elif response is not None:
        entry["status"] = response.status_code
    diagnostics.append(entry)


def _lookup_central(session, api_base: str, qr_code: str, diagnostics: list = None):
    """Resolve a "Central" product (e.g. Hub control panels) QR.

    Centrals aren't looked up by their full printed QR like components/
    devices are - WebDB's own UI extracts a short `central_id` (the QR with
    dashes stripped, first 8 hex characters) and matches on that instead,
    falling back to this only when component/dev both 404. Confirmed
    against the site's own network calls for a real Hub QR.

    Some Central records' own "qr" field on WebDB doesn't match the
    printed/scanned QR at all - confirmed live against a real device
    (central_id "004140a5") where WebDB's own record carries
    "00414-0a5**-*****-*1e00" (literal asterisks, not a display mask) as
    its canonical qr, and Prog Main history is only found by querying with
    that exact string - the real scanned QR ("00414-0a528-da875-01e00")
    returns zero Prog Main results even though it resolves to the same
    central_id. So this returns the record's own "qr" field (`resolved_qr`)
    alongside board_name/region/color - callers MUST use it (not the
    original scanned QR) for any further Central API call, the same way
    _lookup_board_once's dash-truncation fallback works for dev/component.
    Returns (board_name, region, color, resolved_qr) - all None on failure.
    """
    central_id = "".join(c for c in qr_code if c.isalnum())[:8]
    if not central_id:
        return None, None, None, None
    try:
        response = session.get(
            f"{api_base}/general/central/", params={"central_id__in": central_id}, timeout=15
        )
        if response.ok:
            results = response.json().get("results", [])
            if results:
                record = results[0]
                return (
                    record.get("board_name"), record.get("region"), record.get("color"),
                    record.get("qr") or qr_code,
                )
        _log_attempt(diagnostics, "central", response=response)
    except requests.RequestException as exc:
        _log_attempt(diagnostics, "central", exc=exc)
    return None, None, None, None


def _lookup_board(session, api_base: str, qr_code: str, diagnostics: list = None):
    """Same as _lookup_board_once, but retries (with increasing pauses) if
    an attempt finds nothing.

    A dropped connection or a momentary 5xx from the site produces the
    exact same "nothing found" result as a QR that's genuinely not
    registered - there's no cheap way to tell those apart from here, and a
    single quick retry wasn't always enough in practice (some outages last
    longer than 1 second), so this tries up to 3 times total (1s, then 2s
    between attempts) before giving up. Adds no delay at all to the common
    case where the first attempt already succeeds.

    `diagnostics`, if given, collects one entry per failed sub-endpoint
    call across every attempt (status code or exception) - callers that
    end up with a None board_name can log this to Firestore (see
    firestore_client.log_lookup_failure) so a failure on someone else's
    machine is visible somewhere everyone can check, not just in whatever
    local console happened to be open when it occurred.
    """
    result = _lookup_board_once(session, api_base, qr_code, diagnostics)
    for pause in (1, 2):
        if result[0] is not None:
            break
        time.sleep(pause)
        result = _lookup_board_once(session, api_base, qr_code, diagnostics)
    return result


def _lookup_component_or_dev(session, api_base: str, qr_code: str, diagnostics: list = None):
    """The "Component or Product?" pair of lookups the site itself asks
    about for a scanned QR - Component is tried first (the QR codes
    tracked here are almost always PCB components, not finished
    products), falling back to Product/device. Split out from
    _lookup_board_once so it can be retried as-is against a shortened QR
    (see there) without duplicating this logic.
    Returns (board_name, entity, dev_id, region, color, resolved_qr) -
    entity/dev_id are None when nothing matched either endpoint.
    `resolved_qr` is always exactly `qr_code` on a match - it exists so
    _lookup_board_once's dash-truncation fallback can tell its own caller
    which QR string actually matched (the truncated form, not the original
    scanned one) - see _lookup_board_once.
    """
    try:
        comp_response = session.get(f"{api_base}/general/component/{qr_code}/", timeout=15)
        if comp_response.ok:
            comp_json = comp_response.json()
            component_version = comp_json.get("component_version")
            if component_version:
                return (
                    component_version, "component", None,
                    comp_json.get("region"), comp_json.get("color"), qr_code,
                )
        _log_attempt(diagnostics, "component", response=comp_response)
    except requests.RequestException as exc:
        _log_attempt(diagnostics, "component", exc=exc)

    try:
        dev_response = session.get(f"{api_base}/general/dev/{qr_code}/", timeout=15)
        if dev_response.ok:
            dev_json = dev_response.json()
            if dev_json.get("board_name"):
                return (
                    dev_json.get("board_name"), "dev", dev_json.get("dev_id"),
                    dev_json.get("region"), dev_json.get("color"), qr_code,
                )
        _log_attempt(diagnostics, "dev", response=dev_response)
    except requests.RequestException as exc:
        _log_attempt(diagnostics, "dev", exc=exc)

    return None, None, None, None, None, None


def _lookup_board_once(session, api_base: str, qr_code: str, diagnostics: list = None):
    """Resolve a QR to its board/model name, region, colour, and which
    entity type it is.

    Tries Component, then Product/device (see _lookup_component_or_dev),
    then Central (Hub control panels - a different QR scheme entirely,
    see _lookup_central), only when the earlier ones find no record for
    that QR.

    Some scanned QRs carry an extra trailing "-something" segment beyond
    the real device QR - confirmed against a real one
    ("318c34dc401-188e9e0b") where WebDB's own site resolves it as just
    "318c34dc401", silently ignoring the rest, while this tool's own API
    calls used the full string verbatim and found nothing. If nothing
    else matches and the QR contains a dash, this retries Component/
    device once more against just the part before the first dash before
    giving up entirely.
    Returns (board_name, entity, dev_id, region, color, resolved_qr).
    `resolved_qr` is normally just `qr_code` itself, but becomes the
    truncated substring when only that last fallback tier matched -
    callers MUST use `resolved_qr` (not the original `qr_code`) for every
    later API call keyed by QR string (Prog Main history, product details,
    last_steps), or those calls keep finding nothing even though
    board_name now resolves - see check_device_prog_main_session /
    check_assembly_rework_buffer / check_assembly_rework_repaired.
    """
    result = _lookup_component_or_dev(session, api_base, qr_code, diagnostics)
    if result[0] is not None:
        return result

    board_name, region, color, central_resolved_qr = _lookup_central(session, api_base, qr_code, diagnostics)
    if board_name:
        return board_name, "central", None, region, color, central_resolved_qr

    if "-" in qr_code:
        truncated = qr_code.split("-", 1)[0]
        if truncated:
            result = _lookup_component_or_dev(session, api_base, truncated, diagnostics)
            if result[0] is not None:
                return result

    return None, "dev", None, None, None, qr_code


# Assembly Rework only cares about these three steps (Prog Main/Test are
# ignored entirely, unlike Debug mode) - checked in this order when more
# than one is currently failing, since they're sequential stages
# (LongTest -> TestRoom -> QC) and a device that reached and failed a
# later stage necessarily passed the earlier ones in that same run, so
# the latest stage's failure is the relevant one.
_AR_STEP_PRIORITY = ("qc", "test_room", "long_test")
_AR_FROM_LABELS = {"qc": "From QC", "test_room": "From Long Test", "long_test": "From Long Test"}


def _fetch_product_details(
    session: requests.Session, api_base: str, entity: str, qr_code: str, diagnostics: list = None,
):
    """Manufacturing Name/Pro Account Name/Spec from the device/component's
    full record - Assembly Rework's Board column is resolved from the
    *whole device* (see device_choice.py), not the raw PCB board_name
    _lookup_board normally returns, so any caller resolving an AR board
    (buffer-income or Online Repair) needs these too. Returns
    (manufacturing_name, pro_account_name, spec); all None/"[]" on failure.

    Centrals (Hub control panels - see _lookup_central) aren't looked up
    by their full printed QR the way components/devices are, so a plain
    .../general/central/{qr}/ call 404s every time - confirmed live
    against a real Hub QR. Reusing the same central_id-based query
    _lookup_board already used to resolve board_name for one instead:
    its response already carries product/product_info in the same shape,
    no separate endpoint needed. Without this, every Central QR silently
    produced a None manufacturing_name, which _lookup_fully_failed then
    treats as a total lookup failure ("please try again") - blocking
    Buffer scan-in/Online Repair/MRB for every Central QR in Assembly
    Rework mode, not just showing a blank Board.

    `diagnostics`, if given, records a non-2xx response or exception here
    the same way _lookup_board does - a bad/expired WebDB session commonly
    shows up as a 401/403 on this specific endpoint even when the board
    lookup above it still succeeds, and without this, that failure was
    completely invisible: no exception, no diagnostics, just a silently
    empty manufacturing_name that went on to produce a "No info" Buffer/
    Online Repair/MRB entry with no warning at all.
    """
    manufacturing_name = None
    pro_account_name = None
    spec = "[]"
    try:
        if entity == "central":
            central_id = "".join(c for c in qr_code if c.isalnum())[:8]
            detail_response = session.get(
                f"{api_base}/general/central/", params={"central_id__in": central_id}, timeout=15
            )
        else:
            detail_response = session.get(f"{api_base}/general/{entity}/{qr_code}/", timeout=15)
        if detail_response.ok:
            detail_json = detail_response.json()
            if entity == "central":
                results = detail_json.get("results") or []
                detail_json = results[0] if results else {}
            product = detail_json.get("product") or {}
            manufacturing_name = product.get("manufacturing_name")
            spec = "[" + ", ".join(product.get("specific_features") or []) + "]"
            product_info = detail_json.get("product_info") or {}
            pro_account_name = product_info.get("pro_account_name")
        else:
            _log_attempt(diagnostics, "product_details", response=detail_response)
    except requests.RequestException as exc:
        _log_attempt(diagnostics, "product_details", exc=exc)
    return manufacturing_name, pro_account_name, spec


def check_assembly_rework_buffer(session: requests.Session, base_url: str, qr_code: str) -> dict:
    """Assembly Rework's buffer-income check.

    Confirmed against real QR step history on the live site: unlike Debug
    mode (gated by Prog Main/Prog Test), Assembly Rework looks at the most
    recent LongTest/TestRoom/QC attempts (via the same "last steps" API the
    device page's step cards use) and only cares whether one of those three
    failed - see _AR_STEP_PRIORITY for which one "wins" if more than one is
    currently failing.
    """
    api_base = _api_base_url(base_url)
    diagnostics = []
    board_name, entity, dev_id, region, color, resolved_qr = _lookup_board(
        session, api_base, qr_code, diagnostics
    )
    if not board_name:
        return {
            "qr": qr_code, "error": "Khong tim thay thiet bi nay tren WebDB.",
            "lookup_diagnostics": diagnostics,
        }

    manufacturing_name, pro_account_name, spec = _fetch_product_details(
        session, api_base, entity, resolved_qr, diagnostics
    )
    # Assembly Rework's Board text is built entirely from manufacturing_name
    # (via device_choice.py) - without it there's nothing usable to show,
    # and this is indistinguishable from any other lookup failure, so it
    # gets the same lookup_diagnostics treatment as _lookup_board coming up
    # empty above: app_gui.py's existing re-login-and-retry / "please try
    # again" handling already reacts to this field, so this is the only
    # change needed to stop a failed product-details fetch from silently
    # producing a "No info" record instead.
    diag_extra = {"lookup_diagnostics": diagnostics} if not manufacturing_name else {}

    api_base_v2 = api_base.replace("/api/v1", "/api/v2")
    try:
        response = session.post(
            f"{api_base_v2}/last_steps/{entity}/",
            json={"ids": [dev_id] if dev_id else [], "qrs": [resolved_qr], "include_reassigned_qr": True},
            timeout=15,
        )
        response.raise_for_status()
        results = response.json()
    except requests.RequestException as exc:
        return {"qr": qr_code, "error": f"Loi goi API: {exc}", **diag_extra}

    last_steps_by_name = {s.get("step"): s for s in ((results[0].get("last_steps") if results else []) or [])}
    from_label = None
    failed_step_time = None
    for step in _AR_STEP_PRIORITY:
        record = last_steps_by_name.get(step)
        if record and not record.get("success"):
            from_label = _AR_FROM_LABELS[step]
            failed_step_time = record.get("time")
            break

    return {
        "qr": qr_code, "board_name": board_name, "region": region, "color": color,
        "manufacturing_name": manufacturing_name, "pro_account_name": pro_account_name, "spec": spec,
        "from_label": from_label, "failed_step_time": failed_step_time,
        **diag_extra,
    }


def _parse_step_time(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def check_assembly_rework_repaired(session: requests.Session, base_url: str, qr_code: str, since_iso):
    """True if this device has a successful Repair *and* a successful ASM
    (assembling) step logged after `since_iso` - the time it originally
    failed LongTest/TestRoom/QC and entered Assembly Rework's buffer (see
    check_assembly_rework_buffer's failed_step_time). Refresh uses this to
    decide whether a buffered device has since been fixed and reassembled,
    and should move to Completed.

    Re-resolves entity/dev_id fresh (the Buffer entry doesn't store them)
    rather than trusting anything cached from when it first failed.

    Returns (repaired: bool, lookup_diagnostics: list | None) - the second
    element is populated whenever this couldn't actually be determined
    (either the board lookup itself came up empty after every retry, see
    _lookup_board, or the last_steps call below failed on every attempt),
    so a caller can tell "genuinely not repaired yet" apart from "couldn't
    even check" and react (e.g. re-login and retry) instead of silently
    treating both the same way. Refresh runs this across the *entire*
    Buffer list at once - with hundreds of devices, a bare unretried
    last_steps call here previously meant any transient timeout/connection
    reset under that load silently came back as "not repaired" (False,
    None), indistinguishable from a genuine negative - the retry below
    closes that gap the same way _lookup_board already covers the board
    lookup itself.
    """
    since_dt = _parse_step_time(since_iso)
    if since_dt is None:
        return False, None

    api_base = _api_base_url(base_url)
    diagnostics = []
    board_name, entity, dev_id, _region, _color, resolved_qr = _lookup_board(
        session, api_base, qr_code, diagnostics
    )
    if board_name is None:
        return False, diagnostics

    api_base_v2 = api_base.replace("/api/v1", "/api/v2")
    results = None
    for pause in (0, 1, 2):
        if pause:
            time.sleep(pause)
        try:
            response = session.post(
                f"{api_base_v2}/last_steps/{entity}/",
                json={"ids": [dev_id] if dev_id else [], "qrs": [resolved_qr], "include_reassigned_qr": True},
                timeout=15,
            )
            response.raise_for_status()
            results = response.json()
            break
        except requests.RequestException as exc:
            _log_attempt(diagnostics, "last_steps", exc=exc)
    if results is None:
        return False, diagnostics

    last_steps_by_name = {s.get("step"): s for s in ((results[0].get("last_steps") if results else []) or [])}

    def _passed_after(step_name):
        record = last_steps_by_name.get(step_name)
        if not record or not record.get("success"):
            return False
        record_dt = _parse_step_time(record.get("time"))
        return record_dt is not None and record_dt > since_dt

    return _passed_after("repair") and _passed_after("assembling"), None


def _fetch_pcb_qr_info_attempt(session, api_base: str, qr_code: str, diagnostics: list = None):
    """The component-then-dev pair of lookups for one exact qr_code string -
    split out from _fetch_pcb_qr_info_once so it can be retried as-is
    against a shortened QR (see there) without duplicating this logic.
    Returns (pcb_qr, model), both None if this exact qr_code matched
    nothing on either endpoint."""
    for entity in ("component", "dev"):
        try:
            response = session.get(f"{api_base}/general/{entity}/{qr_code}/", timeout=15)
            if response.ok:
                data = response.json()
                pcb_qr = data.get("pcb_qr")
                product = data.get("product") or {}
                model = product.get("manufacturing_name") or data.get("board_name")
                if pcb_qr:
                    return pcb_qr, model
                continue  # a real record, just nothing to report - not a failure
            _log_attempt(diagnostics, entity, response=response)
        except requests.RequestException as exc:
            _log_attempt(diagnostics, entity, exc=exc)
    return None, None


def _fetch_pcb_qr_info_once(session, api_base: str, qr_code: str, diagnostics: list = None):
    """One attempt at resolving a paper QR to its permanent PCB QR (see
    fetch_pcb_qr_info) plus a human-readable model name, via the same
    component-then-dev fallback _lookup_board_once uses - including the
    same dash-truncation last resort (see _lookup_board_once's docstring
    for the confirmed real QR this was needed for) if the exact scanned QR
    matches nothing on either endpoint. This was missing here even after
    fixing it in _lookup_board_once, since that fix only covered Debug's
    Prog Main check and Assembly Rework's product-details lookup, not QR
    Change - confirmed live against a real QR ("3170f884402-4d0a00ed")
    that resolves fine everywhere else but kept failing QR Change's own
    lookup until this same fallback was added here too.
    Returns (pcb_qr, model) - either can be None (record found but that
    particular field wasn't set, e.g. not every product line prints a
    pcb_qr)."""
    pcb_qr, model = _fetch_pcb_qr_info_attempt(session, api_base, qr_code, diagnostics)
    if pcb_qr is not None:
        return pcb_qr, model

    if "-" in qr_code:
        truncated = qr_code.split("-", 1)[0]
        if truncated:
            pcb_qr, model = _fetch_pcb_qr_info_attempt(session, api_base, truncated, diagnostics)
            if pcb_qr is not None:
                return pcb_qr, model

    return None, None


def fetch_pcb_qr_info(session, base_url: str, qr_code: str, diagnostics: list = None):
    """Resolve a paper QR to its permanent PCB QR (engraved on the board
    itself, unlike the paper sticker QR which can be reassigned) plus a
    human-readable model name - confirmed against the site's own
    general/{component,dev}/{qr}/ records, which carry a "pcb_qr" field.
    Retries like _lookup_board (a dropped connection looks identical to a
    genuinely QR-less device from here, so a couple of retries first).
    Returns (pcb_qr, model), both None if nothing was found after retrying.
    """
    api_base = _api_base_url(base_url)
    pcb_qr, model = _fetch_pcb_qr_info_once(session, api_base, qr_code, diagnostics)
    for pause in (1, 2):
        if pcb_qr is not None:
            break
        time.sleep(pause)
        pcb_qr, model = _fetch_pcb_qr_info_once(session, api_base, qr_code, diagnostics)
    return pcb_qr, model


def check_pcb_qr_current(session, base_url: str, pcb_qrs: list) -> dict:
    """Current paper QR for each of `pcb_qrs`, via the same last_steps API
    the WebDB UI's "Product by pcb qr" search option itself calls - POST
    .../last_steps/pcb_qr/, "pcb_qr" being another valid entity type
    alongside the "dev"/"component" ones _get_last_step_info already uses.
    Confirmed against the site's own network calls for a real QR lookup.

    Batches every pcb_qr into one call rather than checking them one at a
    time. Returns {pcb_qr: current_qr} for whichever of `pcb_qrs` WebDB
    still has a record of - a pcb_qr missing from the result just means no
    record (or no current QR) for it, not that the whole call failed.
    Returns {} on a total failure (network error, non-2xx, or `pcb_qrs`
    empty) - callers checking a genuinely non-empty list can treat an
    empty result as "couldn't check any of these, try reconnecting."
    """
    if not pcb_qrs:
        return {}
    api_base = _api_base_url(base_url)
    api_base_v2 = api_base.replace("/api/v1", "/api/v2")
    try:
        response = session.post(
            f"{api_base_v2}/last_steps/pcb_qr/",
            json={"qrs": pcb_qrs, "include_reassigned_qr": True},
            timeout=20,
        )
        response.raise_for_status()
        results = response.json()
    except requests.RequestException:
        return {}
    return {
        entry.get("pcb_qr"): entry.get("qr")
        for entry in results
        if entry.get("pcb_qr") and entry.get("qr")
    }


# Most boards are gated by a "Prog Main" step, but some use a different
# one instead - confirmed against real QR history on the live site (both
# step types can appear in a board's history, but pass/fail must be judged
# by whichever one actually applies to that board).
_PROG_TEST_BOARD_MARKERS = ("MCO.001.OVB.001v8",)
_STEP_TYPE_DISPLAY = {"prog_main": "Prog Main", "prog_test": "Prog Test"}


def _prog_step_type_for_board(board_name) -> str:
    if board_name and any(marker in board_name for marker in _PROG_TEST_BOARD_MARKERS):
        return "prog_test"
    return "prog_main"


def _resolve_prog_main_defect(results):
    """Walk forward through `results` (already time_ordered newest-first)
    to find this device's actual current defect.

    WebDB itself sometimes marks a failed Prog Main attempt with a gate
    code in info.errors instead of a real fail_reasons entry - e.g.
    "permanent_repairs_were_found" when the PCB has been permanently
    repaired (its paper QR effectively reassigned - see the QR Change
    feature) or a "repair in progress" equivalent while a repair is
    still underway. Neither is an actual test failure: "repair in
    progress" should be skipped over entirely to find the last *real*
    defect underneath it, while "permanent repairs found" IS itself the
    answer (there's nothing more current to report).

    Returns (defect_description, permanent_repairs_found, pcb_qr).
    """
    for attempt in results:
        info = attempt.get("info") or {}
        errors = [str(e).lower() for e in (info.get("errors") or [])]
        is_permanent = any("permanent" in e and "repair" in e for e in errors)
        is_in_progress = any("repair" in e and "progress" in e for e in errors)
        if is_permanent:
            return "Permanent repairs found", True, attempt.get("pcb_qr")
        if is_in_progress:
            continue
        if attempt.get("success"):
            return None, False, attempt.get("pcb_qr")
        fail_reasons = info.get("fail_reasons") or []
        if fail_reasons:
            moment = fail_reasons[0].get("moment")
            reason = fail_reasons[0].get("reason")
            defect_description = f"{moment}: {reason}" if moment and reason else (reason or moment)
            return defect_description, False, attempt.get("pcb_qr")
        return None, False, attempt.get("pcb_qr")
    return None, False, None


def check_device_prog_main(driver, base_url: str, qr_code: str) -> dict:
    """Single-device convenience wrapper: builds a fresh API session from
    the driver's cookies, then delegates to check_device_prog_main_session.

    Checking many devices in a row (e.g. a bulk refresh) should instead
    call build_api_session(driver) once and reuse it across all of them via
    check_device_prog_main_session - see that function's docstring.
    """
    return check_device_prog_main_session(build_api_session(driver), base_url, qr_code)


def check_device_prog_main_session(session: requests.Session, base_url: str, qr_code: str) -> dict:
    """Status of the most recent Prog Main attempt for a device or
    component - except for boards in _PROG_TEST_BOARD_MARKERS, which are
    judged by their Prog Test step instead (see _prog_step_type_for_board).

    If the latest attempt failed, walks back through history to find the
    timestamp of the first attempt in the current run of consecutive
    failures - i.e. when this defect first appeared ("import time").

    Calls the site's REST API directly (same one the "Prog Main" panel and
    its "Show all completions" history use) instead of driving the browser
    UI, which is both far faster and immune to the page's own hydration
    quirks. Takes an already-built session (see build_api_session) so a
    caller checking many devices doesn't pay for a fresh Selenium cookie
    round-trip on every single one.
    """
    api_base = _api_base_url(base_url)

    diagnostics = []
    board_name, entity, dev_id, region, color, resolved_qr = _lookup_board(
        session, api_base, qr_code, diagnostics
    )
    # Only relevant to attach when the board lookup itself is what came up
    # empty - a device that resolved fine but genuinely has no Prog Main/
    # Test history isn't a lookup-reliability problem.
    diag_extra = {"lookup_diagnostics": diagnostics} if board_name is None else {}
    step_type = _prog_step_type_for_board(board_name)
    step_label = _STEP_TYPE_DISPLAY.get(step_type, step_type)

    try:
        response = session.get(
            f"{api_base}/prog/{entity}/",
            params={"qr": resolved_qr, "time_ordered": "true", "step_type": step_type},
            timeout=15,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
    except requests.RequestException as exc:
        return {"qr": qr_code, "error": f"Loi goi API: {exc}", **diag_extra}

    if not results:
        return {
            "qr": qr_code, "error": f"Khong tim thay lich su {step_label} cho thiet bi nay.",
            "board_name": board_name, "region": region, "color": color, **diag_extra,
        }

    latest = results[0]
    last_time = latest.get("time")
    passed = bool(latest.get("success"))

    if passed:
        repair_info = _get_last_step_info(session, api_base, resolved_qr, "repair", dev_id=dev_id, entity=entity)
        return {
            "qr": qr_code,
            "passed_last_attempt": True,
            "last_time": last_time,
            "defect_description": None,
            "first_fail_time": None,
            "board_name": board_name,
            "region": region,
            "color": color,
            "debug_operator": (repair_info or {}).get("operator"),
            "complete_time": (repair_info or {}).get("time") or last_time,
            "step_label": step_label,
            **diag_extra,
        }

    defect_description, permanent_repairs_found, pcb_qr = _resolve_prog_main_defect(results)

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
        "region": region,
        "color": color,
        "attempt_count": attempt_count,
        "step_label": step_label,
        "permanent_repairs_found": permanent_repairs_found,
        "pcb_qr": pcb_qr,
        **diag_extra,
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
