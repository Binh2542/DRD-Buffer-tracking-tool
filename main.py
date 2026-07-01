import os
from pathlib import Path

from dotenv import load_dotenv

from webdb_client import build_driver, ensure_logged_in, fetch_device_data, save_result

BASE_DIR = Path(__file__).resolve().parent


def main():
    load_dotenv(BASE_DIR / ".env")

    base_url = os.environ.get("WEBDB_BASE_URL", "https://main.prod.m11g.ajax.systems/webaut/webdb/")
    username = os.environ.get("WEBDB_USERNAME")
    password = os.environ.get("WEBDB_PASSWORD")
    headless = os.environ.get("HEADLESS", "false").lower() == "true"

    if not username or not password:
        raise SystemExit("Thieu WEBDB_USERNAME / WEBDB_PASSWORD. Hay tao file .env (xem .env.example).")

    profile_dir = BASE_DIR / "chrome_profile"
    output_dir = BASE_DIR / "output"

    driver = build_driver(profile_dir, headless=headless)
    try:
        print("Dang mo trang va kiem tra dang nhap...")
        ensure_logged_in(driver, base_url, username, password)
        print("San sang. Quet ma QR (hoac go 'exit' de thoat):")

        while True:
            qr_code = input("> ").strip()
            if not qr_code:
                continue
            if qr_code.lower() in ("exit", "quit"):
                break

            print(f"Dang truy van thiet bi: {qr_code} ...")
            result = fetch_device_data(driver, base_url, qr_code)
            saved_path = save_result(output_dir, qr_code, result)
            preview = result["text"][:300].replace("\n", " ")
            print(f"  -> Da luu {saved_path.name}. Noi dung (preview): {preview}")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
