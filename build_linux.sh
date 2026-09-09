#!/usr/bin/env bash
# Build a ready-to-use Linux package for the DRD Accounting Tool.
#
# Run this ON the target Linux machine (tested against Ubuntu 22.04) -
# PyInstaller cannot cross-compile, so this cannot be run from Windows/macOS
# to produce a Linux binary.
#
# Usage:
#   ./build_linux.sh
#
# Produces dist-package/ (a ready-to-copy folder) and
# dist-package.tar.gz (the same folder, zipped for easy transfer).
set -euo pipefail

cd "$(dirname "$0")"

APP_NAME="DRD Accounting Tool"
PACKAGE_DIR="dist-package"
LOG_FILE="build.log"

# Everything below is mirrored to build.log as well as the screen - if this
# was launched by double-clicking (rather than from an already-open
# terminal), the terminal window commonly closes itself the instant the
# script exits, taking any error message with it before anyone can read
# it. The log file survives that. The trap below also pauses before the
# window can close, and says exactly where to find the log if something
# went wrong.
exec > >(tee "$LOG_FILE") 2>&1

_pause_before_exit() {
    local exit_code=$?
    echo
    if [ "$exit_code" -ne 0 ]; then
        echo "BUILD FAILED (exit code $exit_code)."
        echo "Full log saved to: $(pwd)/$LOG_FILE - please share that file."
    fi
    read -r -p "Press Enter to close this window..." _ 2>/dev/null || true
}
trap _pause_before_exit EXIT

echo "==> Setting up a virtual environment (.venv)"
# --clear wipes any existing .venv before creating a fresh one - a venv's
# bin/pip (and other installed scripts) have the exact source path baked
# into their shebang line, so simply copying/moving a project folder that
# already has a .venv (e.g. keeping versioned copies like v8/v9/...)
# leaves it silently broken at the new path ("bad interpreter: No such
# file or directory") until it's recreated here from scratch.
python3 -m venv --clear .venv
# venv's activate script references $PS1, which is often unset in a
# non-interactive script - temporarily relax -u around it so that doesn't
# trip "unbound variable" on stricter bash/Python combinations.
set +u
# shellcheck disable=SC1091
source .venv/bin/activate
set -u
pip install --upgrade pip
# requirements-linux.txt, not requirements.txt - matplotlib's compiled
# dependencies (numpy/contourpy/pillow) don't all publish wheels spanning
# both this project's Windows build machine's Python and an older
# Ubuntu's Python at once, so Linux gets its own otherwise-identical pin
# for just those - see requirements.txt's own comment for the full story.
pip install -r requirements-linux.txt -r requirements-build.txt

echo "==> Building with PyInstaller"
pyinstaller build.spec --noconfirm

echo "==> Assembling the ready-to-use package in $PACKAGE_DIR/"
rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"
cp "dist/$APP_NAME" "$PACKAGE_DIR/"
chmod +x "$PACKAGE_DIR/$APP_NAME"
cp assets/icon.png "$PACKAGE_DIR/icon.png"

# install.sh is the one (and only) thing the end user ever has to run, and
# only once ever - a plain .desktop file sitting in an extracted folder
# gets treated as "untrusted" by GNOME/KDE and still nags for permission
# on every first launch (right-click -> "Allow Launching"). Actually
# *installing* the .desktop entry into the standard per-user application
# directory (~/.local/share/applications) is what makes desktops trust it
# automatically and show it as a normal, searchable, pinnable app icon -
# exactly like anything installed via a package manager - with zero
# terminal/permission friction from then on.
cat > "$PACKAGE_DIR/install.sh" <<EOF
#!/usr/bin/env bash
# Run this ONCE (double-click it, choose "Run" if your file manager asks)
# to install $APP_NAME as a normal application, including the one-time
# "dialout" group setup a serial QR scanner needs. After this, launch it
# from your Activities/Applications menu like anything else - no
# terminal, no permission prompts, nothing to remember.
set -eu
cd "\$(dirname "\$0")"

INSTALL_DIR="\$HOME/.local/share/drd-accounting-tool"
DESKTOP_DIR="\$HOME/.local/share/applications"
mkdir -p "\$INSTALL_DIR" "\$DESKTOP_DIR"

cp -f "$APP_NAME" "\$INSTALL_DIR/"
chmod +x "\$INSTALL_DIR/$APP_NAME"
cp -f icon.png "\$INSTALL_DIR/"
[ -f firebase_key.json ] && cp -f firebase_key.json "\$INSTALL_DIR/"
[ -f .env ] && cp -f .env "\$INSTALL_DIR/"

# Every user needs the serial QR scanner, so this is set up here too, not
# left as a manual follow-up step. usermod alone wouldn't take effect
# until the next full logout/login though - so instead of relying on
# that, the desktop icon launches through launch.sh below, which
# re-applies the "dialout" supplementary group fresh via sg on every
# single run. That means the scanner works the very first time the app
# is opened from the icon, no logout ever required, today or after any
# future reinstall.
if ! id -nG "\$USER" | tr ' ' '\n' | grep -qx dialout; then
    echo "Adding \$USER to the 'dialout' group (needed for a serial QR scanner) -"
    echo "you may be asked for your password."
    sudo usermod -aG dialout "\$USER"
fi

cat > "\$INSTALL_DIR/launch.sh" <<LAUNCH
#!/usr/bin/env bash
exec sg dialout -c "exec '\$INSTALL_DIR/$APP_NAME'"
LAUNCH
chmod +x "\$INSTALL_DIR/launch.sh"

cat > "\$DESKTOP_DIR/drd-accounting-tool.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=$APP_NAME
Comment=Ajax Systems DRD PCB/device buffer tracking tool
Exec="\$INSTALL_DIR/launch.sh"
Icon=\$INSTALL_DIR/icon.png
Terminal=false
Categories=Office;
DESKTOP
chmod +x "\$DESKTOP_DIR/drd-accounting-tool.desktop"
update-desktop-database "\$DESKTOP_DIR" >/dev/null 2>&1 || true

echo "Installed. Open your Activities/Applications menu and search for \"$APP_NAME\"."
echo "The serial QR scanner will work right away - no logout needed."
echo "This window will close in a few seconds..."
sleep 5
EOF
chmod +x "$PACKAGE_DIR/install.sh"

# run.sh is the quick alternative to install.sh - launches straight out of
# this folder with no system install, but still handles the one-time
# "dialout" group setup a serial QR scanner needs (see check_pcb_qr's
# README section below) automatically instead of requiring a manual
# usermod + logout first. sg (not newgrp) is what makes that apply
# immediately in the SAME run, without a fresh login session - newgrp
# replaces the shell with an interactive one, which doesn't work from a
# non-interactive script the way double-clicking this file would launch it.
cat > "$PACKAGE_DIR/run.sh" <<EOF
#!/usr/bin/env bash
# Launches $APP_NAME directly from this folder - no installation needed.
# Also handles the one-time "dialout" group setup a serial QR scanner
# needs (see README.txt) automatically, the first time it's required.
set -eu
cd "\$(dirname "\$0")"

chmod +x "./$APP_NAME" 2>/dev/null || true

if id -nG "\$USER" | tr ' ' '\n' | grep -qx dialout; then
    exec "./$APP_NAME"
fi

echo "Adding \$USER to the 'dialout' group (needed for a serial QR scanner) -"
echo "you may be asked for your password."
sudo usermod -aG dialout "\$USER"
echo "Done - launching with the new group applied for this session (no logout needed)..."
exec sg dialout -c "exec './$APP_NAME'"
EOF
chmod +x "$PACKAGE_DIR/run.sh"

# Convenience: if this machine already has the two secret files this app
# needs (see README below), include them - this script is normally run by
# whoever is setting up a specific factory deployment, not a generic
# public build, so bundling them here (never committed to git - both are
# in .gitignore) saves that person a manual copy step. Left out entirely,
# with a note, if they're not present.
for secret_file in firebase_key.json .env; do
    if [ -f "$secret_file" ]; then
        cp "$secret_file" "$PACKAGE_DIR/"
        echo "==> Included $secret_file in the package"
    fi
done

cat > "$PACKAGE_DIR/README.txt" <<EOF
$APP_NAME - Linux
$(printf '=%.0s' $(seq 1 $((${#APP_NAME} + 8))))

Two ways to run this - pick whichever fits:

OPTION A: install once, then use it like any other app (recommended)
------------------------------------------------------------------
1. If firebase_key.json and .env aren't already in this folder, copy them
   in first - the app won't start without them.
2. Double-click install.sh. If your file manager asks whether to "Run" or
   "Display" it, choose Run (or Execute). A terminal window will flash up
   briefly and close itself - that's it, nothing else to do.
3. From then on, open the Activities/Applications menu (or the app grid)
   and click "$APP_NAME" - exactly like opening any other program. You
   can also right-click its icon there to pin it to the taskbar/favorites.
   No terminal, no files to find. The serial QR scanner is set up
   automatically too - see SERIAL QR SCANNER below.

OPTION B: run directly from this folder, no installation
------------------------------------------------------------------
Double-click run.sh (or run ./run.sh from a terminal). Nothing else to
configure - every time after that it just launches straight away.

SERIAL QR SCANNER
------------------
A scanner connected as a serial device (e.g. /dev/ttyACM0) can be picked
from the "Serial Scanner Port" dropdown inside the app once it's running.

Both Option A and Option B set this up automatically the first time you
run install.sh/run.sh (you may be asked for your password once) - no
logout needed, nothing else to configure. If it still won't open (a
permission error), add yourself to the "dialout" group by hand and log
out/in once:
    sudo usermod -aG dialout \$USER
EOF

echo "==> Creating $PACKAGE_DIR.tar.gz"
tar -czf "$PACKAGE_DIR.tar.gz" "$PACKAGE_DIR"

echo
echo "==> Done."
echo "    Folder:  $PACKAGE_DIR/"
echo "    Archive: $PACKAGE_DIR.tar.gz  (copy this to the target machine and extract it)"
