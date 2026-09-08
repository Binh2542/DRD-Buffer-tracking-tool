"""Self-update against GitHub Releases (public repo, no token needed).

Release/publishing convention this module expects:
  - Tag the release "vX.Y.Z" or "VX.Y.Z" (either case works, see
    _parse_version) - matching APP_VERSION in app_gui.py.
  - Attach exactly one asset whose name contains "win" (case-insensitive)
    for the Windows build, and one whose name contains "linux" for the
    Linux build - e.g. "DRD-Accounting-Tool-windows.exe" and
    "DRD-Accounting-Tool-linux". Only the asset matching the running OS is
    ever downloaded.

Replacing a running executable differs fundamentally by OS, which is why
this is split into two very different code paths in apply_update_and_restart:
  - Windows locks an .exe file while it's running, so this process can't
    overwrite its own file - a small detached batch script has to wait for
    this process to exit first, then swap the file in and relaunch it.
  - Linux does NOT lock a running binary's file - the file can be
    overwritten in place immediately, and os.execv (replacing this
    process's own image with the new file) restarts straight into the new
    version with no separate helper needed.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

_API_URL = "https://api.github.com/repos/{repo}/releases/latest"
_REQUEST_TIMEOUT = 10
_DOWNLOAD_TIMEOUT = 120


def _parse_version(v: str):
    """'V1.3.0' / 'v1.3.0' / '1.3.0' -> (1, 3, 0), for numeric comparison
    (not string comparison - "V1.10.0" must sort after "V1.9.0")."""
    v = v.strip().lstrip("vV")
    parts = []
    for piece in v.split("."):
        digits = "".join(c for c in piece if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def check_for_update(current_version: str, repo: str):
    """Best-effort - returns None on ANY failure (network down, GitHub
    unreachable, no releases published yet, malformed response, etc.), so
    a caller can always treat this as "no update available right now"
    without special-casing errors. Never raises.

    Returns {"version": "V1.3.1", "asset_url": ..., "asset_name": ...} if
    a newer release with a matching-OS asset exists, else None.
    """
    try:
        response = requests.get(_API_URL.format(repo=repo), timeout=_REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        remote_version = data.get("tag_name") or ""
        if not remote_version:
            return None
        if _parse_version(remote_version) <= _parse_version(current_version):
            return None
        os_key = "win" if sys.platform == "win32" else "linux"
        for asset in data.get("assets", []):
            name = asset.get("name", "")
            if os_key in name.lower():
                return {
                    "version": remote_version,
                    "asset_url": asset["browser_download_url"],
                    "asset_name": name,
                }
        return None  # newer release exists but has no asset for this OS
    except Exception:  # noqa: BLE001 - update-check is strictly best-effort
        return None


def download_asset(url: str, dest_path: Path, on_progress=None) -> None:
    """Streams the download to dest_path. Raises on any failure (caller's
    responsibility to show that to the operator - unlike check_for_update,
    a failure here is after the operator already said "yes, update me",
    so it should be visible, not silently swallowed)."""
    response = requests.get(url, stream=True, timeout=_DOWNLOAD_TIMEOUT)
    response.raise_for_status()
    total = int(response.headers.get("content-length") or 0)
    written = 0
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
    with open(tmp_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if not chunk:
                continue
            f.write(chunk)
            written += len(chunk)
            if on_progress and total:
                on_progress(written, total)
    if total and written != total:
        tmp_path.unlink(missing_ok=True)
        raise IOError(f"Download incomplete: got {written} of {total} bytes")
    tmp_path.replace(dest_path)


def apply_update_and_restart(new_file_path: Path, target_exe_path: Path) -> None:
    """Replaces the currently-running executable with new_file_path and
    restarts into it. Never returns on success - the process either exits
    (Windows, handing off to the helper script) or is replaced in place
    (Linux, via os.execv). Raises on failure before that point (e.g. can't
    write the helper script) so the caller can show an error instead of
    silently doing nothing.
    """
    if sys.platform == "win32":
        _apply_update_windows(new_file_path, target_exe_path)
    else:
        _apply_update_linux(new_file_path, target_exe_path)


def _apply_update_windows(new_file_path: Path, target_exe_path: Path) -> None:
    """Windows keeps an .exe file locked while it's running, so this
    process cannot overwrite its own file - a detached helper script has
    to wait for this process to fully exit first. The old exe is kept as
    "<name>.exe.bak" (one generation only) rather than deleted, so a
    corrupt/failed download still leaves a working copy to fall back to
    by hand."""
    backup_path = target_exe_path.with_name(target_exe_path.name + ".bak")
    log_path = Path(tempfile.gettempdir()) / "drd_accounting_tool_update.log"
    script_path = Path(tempfile.gettempdir()) / "drd_accounting_tool_update.bat"
    # The two `move`s below retry (20x, 500ms apart - 10s total) instead of
    # trying once: found live that an antivirus/EDR agent briefly locking a
    # just-downloaded, unsigned .exe while it scans it is a real, common
    # failure mode on factory Windows machines, and a plain `move` gives no
    # error output under CREATE_NO_WINDOW - it would silently leave the old
    # exe in place with no indication anything went wrong. On persistent
    # failure this logs to %TEMP%\drd_accounting_tool_update.log and still
    # starts whatever ended up at target_exe_path (old build, but a working
    # one) rather than leaving the operator with nothing to launch.
    script = f"""@echo off
:wait
tasklist /FI "IMAGENAME eq {target_exe_path.name}" 2>NUL | find /I "{target_exe_path.name}" >NUL
if "%ERRORLEVEL%"=="0" (
    timeout /t 1 /nobreak > nul
    goto wait
)
if exist "{backup_path}" del /F /Q "{backup_path}"
set RETRIES=0
:move_old
if not exist "{target_exe_path}" goto move_new
move /Y "{target_exe_path}" "{backup_path}" >NUL 2>&1
if not exist "{target_exe_path}" goto move_new
set /a RETRIES+=1
if %RETRIES% GEQ 20 (
    echo %DATE% %TIME% - failed to move old exe aside after 20 attempts >> "{log_path}"
    goto launch
)
timeout /t 1 /nobreak > nul
goto move_old
:move_new
set RETRIES=0
:move_new_retry
move /Y "{new_file_path}" "{target_exe_path}" >NUL 2>&1
if exist "{target_exe_path}" goto launch
set /a RETRIES+=1
if %RETRIES% GEQ 20 (
    echo %DATE% %TIME% - failed to move new exe into place after 20 attempts, restoring backup >> "{log_path}"
    if exist "{backup_path}" move /Y "{backup_path}" "{target_exe_path}" >NUL 2>&1
    goto launch
)
timeout /t 1 /nobreak > nul
goto move_new_retry
:launch
if exist "{target_exe_path}" start "" "{target_exe_path}"
del "%~f0"
"""
    script_path.write_text(script, encoding="utf-8")
    # A windowed (console=False) PyInstaller build has no valid inherited
    # stdin/stdout/stderr - found live that spawning the helper without
    # explicitly redirecting all three here made the whole Popen call
    # silently fail (or the child die immediately) under
    # DETACHED_PROCESS: no helper process ever actually ran, the exe was
    # never swapped, and nothing surfaced an error anywhere since this
    # runs after the GUI has already committed to exiting. DEVNULL on all
    # three avoids inheriting whatever (possibly invalid) handles this
    # frozen process has.
    subprocess.Popen(
        ["cmd", "/c", str(script_path)],
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    os._exit(0)  # noqa: SLF001 - immediate exit, not a normal Tk shutdown: the helper
    # script above is already waiting on this process disappearing from
    # tasklist, so there's nothing left for this process to clean up.


def _apply_update_linux(new_file_path: Path, target_exe_path: Path) -> None:
    """Linux does not lock a running binary's file, so the new one can be
    swapped in immediately - os.execv then replaces this process's own
    image with it, "restarting" with no separate process/helper needed
    and no window ever closing and reopening."""
    backup_path = target_exe_path.with_name(target_exe_path.name + ".bak")
    if target_exe_path.exists():
        shutil.copy2(target_exe_path, backup_path)
    shutil.move(str(new_file_path), str(target_exe_path))
    os.chmod(target_exe_path, 0o755)
    os.execv(str(target_exe_path), [str(target_exe_path)])
