#!/usr/bin/env bash
# Pre-flight check: run this on the Ubuntu machine BEFORE ./build_linux.sh
# to catch missing pieces early, with the exact command to fix each one -
# instead of finding out halfway through a failed build.
#
# Usage: ./check_linux_requirements.sh
set +e  # keep going even if a check fails, so this reports everything at once

PASS=0
FAIL=0
_ok()   { echo "  [OK]   $1"; PASS=$((PASS + 1)); }
_fail() { echo "  [MISSING] $1"; echo "           Fix: $2"; FAIL=$((FAIL + 1)); }

echo "=== System ==="
if [ -f /etc/os-release ]; then
    . /etc/os-release
    echo "  Detected: ${PRETTY_NAME:-unknown}"
    if [ "${ID:-}" != "ubuntu" ]; then
        echo "  Note: this was written/tested for Ubuntu 22.04 - other distros may work but aren't verified."
    fi
fi
echo

echo "=== Python ==="
if command -v python3 >/dev/null 2>&1; then
    PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
    if [[ "$PY_VER" =~ ^([0-9]+)\.([0-9]+)$ ]]; then
        PY_MAJOR="${BASH_REMATCH[1]}"
        PY_MINOR="${BASH_REMATCH[2]}"
        if [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -ge 10 ]; then
            _ok "python3 found, version $PY_VER (need 3.10+)"
        else
            _fail "python3 found but version $PY_VER is too old (need 3.10+)" \
                  "install a newer Python (Ubuntu 22.04's default python3 is already 3.10, so this is unusual)"
        fi
    else
        # A "python3" command exists but didn't actually run - can happen with
        # a broken alias/shim rather than a real interpreter.
        _fail "python3 command found but didn't run correctly" "sudo apt install --reinstall python3"
    fi
else
    _fail "python3 not found" "sudo apt update && sudo apt install python3"
fi

if python3 -c 'import venv' >/dev/null 2>&1; then
    _ok "python3 venv module available"
else
    _fail "python3 venv module missing" "sudo apt install python3-venv"
fi

if python3 -c 'import tkinter' >/dev/null 2>&1; then
    _ok "tkinter available (this app's entire UI depends on it)"
else
    _fail "tkinter missing - NOT installed by Ubuntu's default python3" "sudo apt install python3-tk"
fi
echo

echo "=== Chrome / Chromium (WebDB automation browser) ==="
CHROME_FOUND=0
for bin in google-chrome google-chrome-stable chromium chromium-browser; do
    if command -v "$bin" >/dev/null 2>&1; then
        VER=$("$bin" --version 2>/dev/null || echo "unknown version")
        _ok "found '$bin' ($VER)"
        CHROME_FOUND=1
        break
    fi
done
if [ "$CHROME_FOUND" -eq 0 ]; then
    _fail "no Chrome/Chromium found" \
          "install Google Chrome from https://www.google.com/chrome/ (download the .deb and: sudo apt install ./google-chrome-stable_current_amd64.deb), or: sudo snap install chromium"
fi
echo

echo "=== Build tools ==="
if command -v tar >/dev/null 2>&1; then
    _ok "tar available"
else
    _fail "tar missing" "sudo apt install tar"
fi

if command -v objdump >/dev/null 2>&1; then
    _ok "objdump available (PyInstaller needs this on Linux to inspect binary dependencies)"
else
    _fail "objdump missing - PyInstaller will fail immediately without it" "sudo apt install binutils"
fi

if command -v gcc >/dev/null 2>&1 || command -v cc >/dev/null 2>&1; then
    _ok "a C compiler is available (only needed as a fallback if pip can't find a prebuilt wheel)"
else
    echo "  [INFO] no C compiler found - usually fine, since all dependencies here ship prebuilt"
    echo "         wheels for standard Ubuntu/x86_64. Only needed as a fallback:"
    echo "         sudo apt install build-essential python3-dev"
fi
echo

echo "=== Network ==="
if command -v curl >/dev/null 2>&1 && curl -s --head --max-time 5 https://pypi.org >/dev/null 2>&1; then
    _ok "internet reachable (pypi.org) - needed to pip install dependencies"
elif command -v wget >/dev/null 2>&1 && wget -q --spider --timeout=5 https://pypi.org 2>/dev/null; then
    _ok "internet reachable (pypi.org) - needed to pip install dependencies"
else
    _fail "could not reach pypi.org" "check this machine's internet connection/proxy settings"
fi
echo

echo "=== Disk space ==="
AVAIL_MB=$(df -Pm . | awk 'NR==2 {print $4}')
if [ "${AVAIL_MB:-0}" -ge 2000 ]; then
    _ok "${AVAIL_MB} MB free in this folder (2000+ MB recommended for the venv + build output)"
else
    _fail "only ${AVAIL_MB} MB free here" "free up disk space (the venv + PyInstaller build needs ~2 GB)"
fi
echo

echo "=== App secrets ==="
cd "$(dirname "$0")"
if [ -f firebase_key.json ]; then
    _ok "firebase_key.json present"
else
    echo "  [INFO] firebase_key.json not here yet - build_linux.sh will still work, but you'll"
    echo "         need to add it to dist-package/ before the app can run. Ask an admin for it."
fi
if [ -f .env ]; then
    _ok ".env present"
else
    echo "  [INFO] .env not here yet - same as above, add it to dist-package/ before running the app."
fi
echo

echo "=================================================="
echo "  $PASS check(s) passed, $FAIL required check(s) missing"
echo "=================================================="
if [ "$FAIL" -eq 0 ]; then
    echo "Everything required is in place - you're clear to run ./build_linux.sh"
else
    echo "Fix the [MISSING] item(s) above, then run this script again."
fi
