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
  - Windows locks an .exe file's BYTES while it's running, but - somewhat
    unintuitively - does not stop it being renamed/moved elsewhere; only
    overwriting or deleting it in place is blocked. So this process can
    rename its own running exe out of the way and put the new one in its
    place itself, with no separate helper process needed at all. (An
    earlier version spawned a detached background script to do this after
    the process exited, the way most self-updaters are commonly written -
    found live on a real factory machine that this reliably never ran, an
    outcome consistent with security software blocking a hidden process
    silently replacing another program's binary, exactly the shape of
    thing it exists to catch. Doing the swap in-process before exiting
    removes that failure mode entirely, and os.startfile for the final
    relaunch is the same OS call Explorer itself uses for a double-click.)
  - Linux does NOT lock a running binary's file at all - the file can be
    overwritten in place immediately, and os.execv (replacing this
    process's own image with the new file) restarts straight into the new
    version with no separate helper needed.
"""
import os
import shutil
import sys
import time
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
    restarts into it. Never returns on success - the process exits either
    way (Windows via os._exit after launching the new exe, Linux via
    os.execv replacing this process's own image). Raises on failure (e.g.
    every rename/move retry exhausted) so the caller can show an error
    instead of silently doing nothing.
    """
    if sys.platform == "win32":
        _apply_update_windows(new_file_path, target_exe_path)
    else:
        _apply_update_linux(new_file_path, target_exe_path)


def _retry(fn, attempts=20, delay=0.5):
    """Retries fn() (a rename/move) up to `attempts` times, `delay` seconds
    apart, re-raising the last error if it never succeeds. A just-
    downloaded, unsigned .exe being briefly locked by antivirus/EDR
    scanning is a realistic transient failure on factory Windows machines -
    20x/0.5s (10s total) comfortably outlasts that without the operator
    noticing more than a brief pause."""
    last_err = None
    for _ in range(attempts):
        try:
            fn()
            return
        except OSError as exc:
            last_err = exc
            time.sleep(delay)
    raise last_err


def _apply_update_windows(new_file_path: Path, target_exe_path: Path) -> None:
    """Renames the currently-running exe aside, puts the newly-downloaded
    one in its exact original place (same name AND path, so an existing
    Start Menu/taskbar pin still resolves correctly), then launches it and
    exits. The old exe is kept as "<name>.exe.bak" (one generation only)
    rather than deleted, so a corrupt/failed download still leaves a
    working copy to fall back to by hand. Raises on failure (e.g. every
    retry exhausted) rather than silently leaving the operator with
    nothing - see this module's docstring for why this doesn't need (and
    deliberately avoids) a separate helper process."""
    backup_path = target_exe_path.with_name(target_exe_path.name + ".bak")
    if backup_path.exists():
        try:
            backup_path.unlink()
        except OSError:
            pass
    if target_exe_path.exists():
        _retry(lambda: target_exe_path.rename(backup_path))
    _retry(lambda: shutil.move(str(new_file_path), str(target_exe_path)))
    os.startfile(str(target_exe_path))  # noqa: S606 - identical to a normal double-click
    os._exit(0)  # noqa: SLF001 - immediate exit, not a normal Tk shutdown: the
    # replacement above already fully happened and the new process is
    # already launched, so there's nothing left for this process to do.


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
