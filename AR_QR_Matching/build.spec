# -*- mode: python ; coding: utf-8 -*-
# Build with: pyinstaller build.spec
# Produces a single portable .exe on Windows, a single portable binary with
# no extension on Linux - PyInstaller doesn't cross-compile, so this must be
# run ON whichever OS you want the output for (see build_linux.sh for the
# Ubuntu build+package flow).
APP_NAME = "AR_QR_Matching"

a = Analysis(
    ["app_gui.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("assets/icon.ico", "assets"),
        ("assets/icon.png", "assets"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.ico",
)
