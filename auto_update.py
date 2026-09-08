"""Self-update against GitHub Releases (public repo, no token needed).

Release/publishing convention this module expects:
  - Tag the release "vX.Y.Z" or "VX.Y.Z" (either case works, see
    _parse_version) - matching APP_VERSION in app_gui.py.
  - Attach exactly one asset whose name contains "win" (case-insensitive)
    for the Windows build, and one whose name contains "linux" for the
    Linux build - e.g. "DRD-Accounting-Tool-windows.exe" and
    "DRD-Accounting-Tool-linux". Only the asset matching the running OS is
    ever downloaded.
  - EVERY release needs a working asset for BOTH win and linux, even one
    that only changed on one platform - check_for_update always reads
    GitHub's own "latest release" (by publish time, not per-OS), so a
    Windows-only release with no Linux asset makes every Linux machine's
    check_for_update return None (a newer release exists, but "has no
    asset for this OS" - see below) even though an OLDER release further
    back still has a perfectly good, newer-than-what-they-have Linux
    build. Found live: two Windows-only point releases in a row made
    Linux machines stop seeing updates entirely, silently, with no error
    anywhere - simplest fix is to always re-upload the last known-good
    Linux (or Windows) asset unchanged onto a same-platform-only release
    rather than ever leaving one missing.
  - Also attach a "<asset name>.sha256" text file (just the hex digest)
    for each binary asset - found live that a download can land with the
    exact right byte count yet still be unable to run (a Linux machine's
    update produced a same-size file PyInstaller's own bootloader could
    not read as a valid archive; sha256sum of the source build, the
    upload, and a fresh re-download from GitHub all matched each other,
    so whatever corrupted it was specific to that one machine, never
    conclusively identified). check_for_update looks for this file
    automatically and, if present, download_asset verifies against it
    before the caller is ever allowed to treat the download as good -
    catching this (from any cause) BEFORE the working binary gets
    touched, rather than after, since a Linux update that gets this far
    has no way back (os.execv either starts the new image or the old
    process is simply gone).

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
import datetime as dt
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

_API_URL = "https://api.github.com/repos/{repo}/releases/latest"
_REQUEST_TIMEOUT = 10
_DOWNLOAD_TIMEOUT = 120

# TEMPORARY diagnostic log - the update mechanism has failed silently on at
# least one real machine (app just disappears mid-update, no error dialog,
# no file changes) with no clue why anything this module itself could catch
# would explain it. Appending a line after every single step, flushed
# immediately, means whatever DOES get written before the process
# disappears (if anything does) is the best lead available - remove once
# the real cause is found and fixed.
_DEBUG_LOG = Path(tempfile.gettempdir()) / "drd_update_debug.log"


def _debug_log(message: str) -> None:
    try:
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now().isoformat()} - {message}\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass  # best-effort - never let logging itself be the reason this breaks


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
        _debug_log(f"check_for_update: current={current_version!r} repo={repo!r}")
        response = requests.get(_API_URL.format(repo=repo), timeout=_REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        remote_version = data.get("tag_name") or ""
        if not remote_version:
            return None
        if _parse_version(remote_version) <= _parse_version(current_version):
            _debug_log(f"check_for_update: remote={remote_version!r} not newer, nothing to do")
            return None
        os_key = "win" if sys.platform == "win32" else "linux"
        assets = data.get("assets", [])
        for asset in assets:
            name = asset.get("name", "")
            if os_key in name.lower() and not name.lower().endswith(".sha256"):
                checksum_asset = next(
                    (a for a in assets if a.get("name") == name + ".sha256"), None
                )
                sha256 = _fetch_expected_sha256(checksum_asset) if checksum_asset else None
                _debug_log(
                    f"check_for_update: found update {remote_version!r} asset={name!r} sha256={sha256!r}"
                )
                return {
                    "version": remote_version,
                    "asset_url": asset["browser_download_url"],
                    "asset_name": name,
                    "sha256": sha256,
                }
        _debug_log(f"check_for_update: remote={remote_version!r} is newer but has no {os_key} asset")
        return None  # newer release exists but has no asset for this OS
    except Exception as exc:  # noqa: BLE001 - update-check is strictly best-effort
        _debug_log(f"check_for_update: FAILED (treated as no-update-available) - {exc!r}")
        return None


def _fetch_expected_sha256(checksum_asset: dict):
    """Downloads a "<asset>.sha256" companion file's content (just the hex
    digest, optionally in "sha256sum <filename>" format) and returns the
    lowercase hex digest, or None on any failure - a missing/unreachable
    checksum file should never block an update that would otherwise have
    worked before this verification existed, only a *mismatching* one
    should (see download_asset)."""
    try:
        response = requests.get(checksum_asset["browser_download_url"], timeout=_REQUEST_TIMEOUT)
        response.raise_for_status()
        first_token = response.text.strip().split()[0]
        return first_token.lower()
    except Exception as exc:  # noqa: BLE001 - best-effort, see docstring
        _debug_log(f"_fetch_expected_sha256: could not fetch/parse checksum file - {exc!r}")
        return None


def download_asset(url: str, dest_path: Path, on_progress=None, expected_sha256: str = None) -> None:
    """Streams the download to dest_path. Raises on any failure (caller's
    responsibility to show that to the operator - unlike check_for_update,
    a failure here is after the operator already said "yes, update me",
    so it should be visible, not silently swallowed).

    If expected_sha256 is given, the fully-downloaded file is hashed and
    compared before being moved to dest_path - a mismatch raises instead
    of ever letting the caller treat a bad download as good (see this
    module's docstring for why this exists: a same-byte-count-but-broken
    download that reached this point undetected, before this check
    existed, corrupted a Linux machine's own running installation with no
    way back)."""
    _debug_log(f"download_asset: starting {url!r} -> {dest_path} (expected_sha256={expected_sha256!r})")
    try:
        response = requests.get(url, stream=True, timeout=_DOWNLOAD_TIMEOUT)
        response.raise_for_status()
        total = int(response.headers.get("content-length") or 0)
        written = 0
        hasher = hashlib.sha256()
        tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
        with open(tmp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                hasher.update(chunk)
                written += len(chunk)
                if on_progress and total:
                    on_progress(written, total)
        if total and written != total:
            tmp_path.unlink(missing_ok=True)
            raise IOError(f"Download incomplete: got {written} of {total} bytes")
        if expected_sha256:
            actual = hasher.hexdigest()
            if actual != expected_sha256:
                tmp_path.unlink(missing_ok=True)
                raise IOError(
                    f"Download failed checksum verification: expected {expected_sha256}, got {actual} "
                    f"({written} bytes) - not applying this update"
                )
            _debug_log(f"download_asset: sha256 verified ({actual})")
        tmp_path.replace(dest_path)
        _debug_log(f"download_asset: done, {written} bytes written to {dest_path}")
    except Exception as exc:
        _debug_log(f"download_asset: FAILED - {exc!r}")
        raise


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


def _retry(fn, label, attempts=20, delay=0.5):
    """Retries fn() (a rename/move) up to `attempts` times, `delay` seconds
    apart, re-raising the last error if it never succeeds. A just-
    downloaded, unsigned .exe being briefly locked by antivirus/EDR
    scanning is a realistic transient failure on factory Windows machines -
    20x/0.5s (10s total) comfortably outlasts that without the operator
    noticing more than a brief pause. Logs every failed attempt's exact
    exception (label identifies which step, since this is shared by both)."""
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            fn()
            _debug_log(f"{label}: succeeded on attempt {attempt}")
            return
        except OSError as exc:
            last_err = exc
            _debug_log(f"{label}: attempt {attempt}/{attempts} failed - {exc!r} (winerror={getattr(exc, 'winerror', None)})")
            time.sleep(delay)
    _debug_log(f"{label}: giving up after {attempts} attempts - {last_err!r}")
    raise last_err


def _is_running_windows(exe_name: str) -> bool:
    """True if a process with this exact image name is currently running,
    via `tasklist` (no extra dependency needed for one check)."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}"],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return exe_name.lower() in result.stdout.lower()
    except Exception as exc:  # noqa: BLE001 - best-effort check, see caller
        _debug_log(f"_is_running_windows: check failed - {exc!r}")
        return False


def _launch_and_confirm_windows(target_exe_path: Path, attempts: int = 2) -> None:
    """Starts target_exe_path (os.startfile, the same call Explorer itself
    uses for a double-click) and confirms it actually stayed running,
    retrying once if not. Found live that a freshly-downloaded exe's very
    first launch can fail this way (a bootloader import error deep in a
    bundled dependency) while every launch after the first succeeds -
    consistent with antivirus real-time scanning racing PyInstaller's
    onefile self-extraction on a file it hasn't seen before, rather than
    anything actually wrong with the build. Not a fix for that race (it
    resolves itself by the next launch either way) - just automates the
    "close the error and reopen it" workaround an operator would
    otherwise have to discover and do by hand."""
    exe_name = target_exe_path.name
    for attempt in range(1, attempts + 1):
        _debug_log(f"_launch_and_confirm_windows: os.startfile attempt {attempt}/{attempts}")
        os.startfile(str(target_exe_path))  # noqa: S606
        time.sleep(2.5)
        if _is_running_windows(exe_name):
            _debug_log(f"_launch_and_confirm_windows: confirmed running on attempt {attempt}")
            return
        _debug_log(f"_launch_and_confirm_windows: not running after attempt {attempt} - likely crashed on launch")
    _debug_log("_launch_and_confirm_windows: gave up confirming - exiting anyway, nothing more this process can do")


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
    _debug_log(f"_apply_update_windows: start new={new_file_path} target={target_exe_path} pid={os.getpid()}")
    try:
        backup_path = target_exe_path.with_name(target_exe_path.name + ".bak")
        if backup_path.exists():
            try:
                backup_path.unlink()
                _debug_log(f"_apply_update_windows: removed stale {backup_path}")
            except OSError as exc:
                _debug_log(f"_apply_update_windows: could not remove stale {backup_path} - {exc!r} (non-fatal)")
        if target_exe_path.exists():
            _retry(lambda: target_exe_path.rename(backup_path), "rename old exe aside")
        else:
            _debug_log(f"_apply_update_windows: {target_exe_path} does not exist, skipping rename-aside")
        _retry(lambda: shutil.move(str(new_file_path), str(target_exe_path)), "move new exe into place")
        _launch_and_confirm_windows(target_exe_path)
        _debug_log("_apply_update_windows: launch confirmed - about to os._exit(0)")
    except Exception as exc:  # noqa: BLE001 - log absolutely everything before it can propagate/kill us
        _debug_log(f"_apply_update_windows: UNCAUGHT-UNTIL-NOW EXCEPTION - {exc!r}")
        raise
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
