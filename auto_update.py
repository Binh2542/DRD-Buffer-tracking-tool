"""Self-update against GitHub Releases (public repo, no token needed).

Release/publishing convention this module expects:
  - Tag the release "vX.Y.Z" or "VX.Y.Z" (either case works, see
    _parse_version) - matching APP_VERSION in app_gui.py.
  - Attach exactly one asset whose name contains "win" (case-insensitive)
    for the Windows build, and one whose name contains "linux" for the
    Linux build - e.g. "DRD-Accounting-Tool-windows.exe" and
    "DRD-Accounting-Tool-linux". Only the asset matching the running OS is
    ever downloaded.
  - A release can freely be platform-specific (e.g. a Windows-only point
    release with no Linux asset at all, because only Windows changed) -
    check_for_update walks every release itself (newest first) looking
    for the newest one that both beats the running version AND actually
    has an asset for this OS, rather than trusting GitHub's own single
    repo-wide "latest release" pointer. That pointer has no concept of
    per-platform versions, so relying on it directly made a Windows-only
    release silently look like "nothing newer" to every Linux machine,
    even with an older release sitting right there with a perfectly good
    Linux build newer than what they had. Do NOT try to route around
    that by re-uploading an unchanged binary onto a same-platform-only
    release just to keep the "latest" pointer satisfied for both - that
    creates a worse bug: the binary itself still reports its own real,
    unchanged APP_VERSION, so a Windows-tagged release carrying, say,
    "V1.3.1"'s actual Linux build would make that Linux machine update
    to a file that then identifies as V1.3.1 again next launch, forever
    treating the newer Windows-only tag as "an update" it can never
    actually reach.
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

# /releases (plural, newest-first) rather than /releases/latest (singular) -
# GitHub's own "latest" is a single repo-wide pointer with no concept of
# per-platform versions, so a Windows-only point release makes it (wrongly)
# look like there's nothing newer for Linux at all, even when an older
# release still has a perfectly good, newer-than-current Linux build (see
# this module's docstring). Walking the list ourselves and picking the
# newest release that both (a) beats current_version and (b) actually has
# an asset for this OS means Windows and Linux can advance independently
# without ever needing to re-upload an unchanged binary onto every release
# just to keep GitHub's single "latest" pointer satisfied for both.
_API_URL = "https://api.github.com/repos/{repo}/releases?per_page=20"
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


def check_for_update(current_version: str, repo: str, allow_prerelease: bool = False):
    """Best-effort - returns None on ANY failure (network down, GitHub
    unreachable, no releases published yet, malformed response, etc.), so
    a caller can always treat this as "no update available right now"
    without special-casing errors. Never raises.

    Returns {"version": "V1.3.1", "asset_url": ..., "asset_name": ...} if
    a newer release with a matching-OS asset exists, else None. Walks
    every release (newest tag first) rather than trusting GitHub's own
    "latest" pointer - see the module docstring and this file's comment
    on _API_URL for why: that pointer is repo-wide, not per-platform.

    allow_prerelease=False (the default, and what every machine other
    than a developer's own test one should ever pass) skips releases
    published as a GitHub "pre-release" entirely - this is the test-
    before-rollout mechanism: publish a build as a pre-release, and it's
    invisible to every real machine until it's confirmed good and
    promoted (flip the pre-release flag off, no rebuild needed) - only a
    machine that's deliberately opted in via the local .test_channel
    marker (see app_gui.py) ever sees it early, exactly like the incident
    this exists to prevent: a broken Linux build reached every machine at
    once with no way to have caught it on just one first.
    """
    try:
        _debug_log(f"check_for_update: current={current_version!r} repo={repo!r} allow_prerelease={allow_prerelease}")
        response = requests.get(_API_URL.format(repo=repo), timeout=_REQUEST_TIMEOUT)
        response.raise_for_status()
        releases = response.json()
        os_key = "win" if sys.platform == "win32" else "linux"
        current = _parse_version(current_version)
        best = None  # (version_tuple, remote_version_str, asset, assets) of the best match so far
        for release in releases:
            if release.get("prerelease") and not allow_prerelease:
                continue
            remote_version = release.get("tag_name") or ""
            if not remote_version:
                continue
            version_tuple = _parse_version(remote_version)
            if version_tuple <= current:
                continue  # not newer than what we're already running
            if best is not None and version_tuple <= best[0]:
                continue  # already found a newer release than this one for this OS
            assets = release.get("assets", [])
            for asset in assets:
                name = asset.get("name", "")
                if os_key in name.lower() and not name.lower().endswith(".sha256"):
                    best = (version_tuple, remote_version, asset, assets)
                    break
        if best is None:
            _debug_log(f"check_for_update: no release newer than {current_version!r} has a {os_key} asset")
            return None
        _version_tuple, remote_version, asset, assets = best
        name = asset["name"]
        checksum_asset = next((a for a in assets if a.get("name") == name + ".sha256"), None)
        sha256 = _fetch_expected_sha256(checksum_asset) if checksum_asset else None
        _debug_log(f"check_for_update: found update {remote_version!r} asset={name!r} sha256={sha256!r}")
        return {
            "version": remote_version,
            "asset_url": asset["browser_download_url"],
            "asset_name": name,
            "sha256": sha256,
        }
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
