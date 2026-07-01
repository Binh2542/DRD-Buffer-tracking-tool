import csv
import datetime as dt
import re
import time
from pathlib import Path
from urllib.parse import quote

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

LOGIN_WAIT_SECONDS = 20
RESULT_WAIT_SECONDS = 20


def build_driver(profile_dir: Path, headless: bool = False) -> webdriver.Chrome:
    options = Options()
    options.add_argument(f"--user-data-dir={profile_dir.resolve()}")
    options.add_argument("--profile-directory=Default")
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--start-maximized")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


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


def ensure_logged_in(driver, base_url: str, username: str, password: str) -> None:
    driver.get(base_url)
    try:
        password_field = WebDriverWait(driver, LOGIN_WAIT_SECONDS).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']"))
        )
    except TimeoutException:
        return  # no login form shown -> session already active

    if not password_field.is_displayed():
        return

    username_field = _find_username_field(driver)
    if username_field is None:
        print("Khong tim thay o username tu dong.")
        print("Hay dang nhap thu cong trong cua so trinh duyet, roi quay lai day va nhan Enter...")
        input()
        return

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
    except TimeoutException:
        print("Khong chac da dang nhap thanh cong.")
        print("Kiem tra cua so trinh duyet, dang nhap thu cong neu can roi nhan Enter...")
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
        time.sleep(0.8)

    close_buttons = modal.find_elements(By.CSS_SELECTOR, "button.sticky")
    if close_buttons:
        close_buttons[0].click()
    else:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    time.sleep(0.3)
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
        time.sleep(1)
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


def fetch_device_data(driver, base_url: str, qr_code: str, include_history: bool = True) -> dict:
    target = quote(f"device:{qr_code}", safe="")
    url = f"{base_url.rstrip('/')}/?target={target}"
    driver.get(url)
    try:
        load_button = WebDriverWait(driver, RESULT_WAIT_SECONDS).until(
            EC.element_to_be_clickable((By.XPATH, "//button[normalize-space(text())='Load']"))
        )
        load_button.click()
        WebDriverWait(driver, RESULT_WAIT_SECONDS).until(
            lambda d: "General Info" in d.find_element(By.TAG_NAME, "body").text
        )
    except TimeoutException:
        pass

    if _click_button_by_text(driver, "expand all"):
        time.sleep(0.5)

    body_text = driver.find_element(By.TAG_NAME, "body").text

    if include_history:
        step_logs = _collect_step_history(driver)
        body_text += _format_step_history(step_logs)

    html = driver.page_source
    return {"url": url, "text": body_text, "html": html}


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
