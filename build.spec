# -*- mode: python ; coding: utf-8 -*-
# Build with: pyinstaller build.spec
# Produces a single portable .exe on Windows, a single portable binary with
# no extension on Linux, or a double-clickable .app on macOS - each must be
# built on its own OS (PyInstaller doesn't cross-compile). Windows and Linux
# share the exact same single-file EXE() branch below; only macOS needs its
# own COLLECT/BUNDLE path to produce a proper .app.
import sys

from PyInstaller.utils.hooks import collect_submodules

APP_NAME = "DRD Accounting Tool"
is_mac = sys.platform == "darwin"
icon_file = "assets/icon.icns" if is_mac else "assets/icon.ico"

# selenium and webdriver_manager both load their browser-specific submodules
# (e.g. selenium.webdriver.chrome.webdriver) dynamically at runtime based on
# which browser is requested, which PyInstaller's static import-graph scan
# doesn't see even with their bundled hooks - so pull in every submodule
# explicitly rather than hitting "No module named ..." one at a time. pyserial
# (serial_scanner.py) does the same kind of OS-conditional dynamic import for
# serial.tools.list_ports' platform-specific backend.
hidden_imports = (
    collect_submodules("selenium") + collect_submodules("webdriver_manager") + collect_submodules("serial")
)

a = Analysis(
    ["app_gui.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("assets/icon.ico", "assets"),
        ("assets/icon.png", "assets"),
        ("assets/icon.icns", "assets"),
    ],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

if is_mac:
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        # UPX-compressing the bundled Python DLL/extension modules found
        # live to intermittently break specific stdlib C-extension imports
        # at runtime (e.g. "ModuleNotFoundError: No module named
        # 'unicodedata'" deep inside matplotlib on a build that otherwise
        # worked fine) - reproduced across more than one machine, so this
        # isn't one machine's antivirus/environment being unusual, it's
        # UPX corrupting something in the packed DLL itself. UPX is a
        # pure size optimization (trades a larger file for faster/more
        # reliable startup) - not worth the risk for this app.
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=True,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=icon_file,
    )
    coll = COLLECT(
        exe, a.binaries, a.datas,
        strip=False, upx=False, upx_exclude=[], name=APP_NAME,
    )
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=icon_file,
        bundle_identifier="com.drd.buffertrackingtool",
        info_plist={"NSHighResolutionCapable": True},
    )
else:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        # UPX-compressing the bundled Python DLL/extension modules found
        # live to intermittently break specific stdlib C-extension imports
        # at runtime (e.g. "ModuleNotFoundError: No module named
        # 'unicodedata'" deep inside matplotlib on a build that otherwise
        # worked fine) - reproduced across more than one machine, so this
        # isn't one machine's antivirus/environment being unusual, it's
        # UPX corrupting something in the packed DLL itself. UPX is a
        # pure size optimization (trades a larger file for faster/more
        # reliable startup) - not worth the risk for this app.
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=icon_file,
    )
