#!/usr/bin/env bash
# Build a ready-to-use Linux package for AR_QR_Matching.
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

APP_NAME="AR_QR_Matching"
PACKAGE_DIR="dist-package"
LOG_FILE="build.log"

# Mirrored to build.log as well as the screen - if this was launched by
# double-clicking (rather than from an already-open terminal), the terminal
# window commonly closes itself the instant the script exits, taking any
# error message with it before anyone can read it. The log file survives
# that, and the trap below pauses before the window can close.
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
# bin/pip has the exact source path baked into their shebang line, so
# copying/moving this project folder (e.g. between machines) leaves an
# existing .venv silently broken at the new path until it's recreated here.
python3 -m venv --clear .venv
set +u
# shellcheck disable=SC1091
source .venv/bin/activate
set -u
pip install --upgrade pip
pip install -r requirements.txt -r requirements-build.txt

echo "==> Building with PyInstaller"
pyinstaller build.spec --noconfirm

echo "==> Assembling the ready-to-use package in $PACKAGE_DIR/"
rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"
cp "dist/$APP_NAME" "$PACKAGE_DIR/"
chmod +x "$PACKAGE_DIR/$APP_NAME"
cp assets/icon.png "$PACKAGE_DIR/icon.png"

# install.sh is the one (and only) thing the end user ever has to run, and
# only once ever - a plain .desktop file sitting in an extracted folder gets
# treated as "untrusted" by GNOME/KDE and still nags for permission on every
# first launch. Actually *installing* the .desktop entry into the standard
# per-user application directory (~/.local/share/applications) is what
# makes desktops trust it automatically and show it as a normal,
# searchable, pinnable app icon - exactly like DRD Accounting Tool.
cat > "$PACKAGE_DIR/install.sh" <<EOF
#!/usr/bin/env bash
# Run this ONCE (double-click it, choose "Run" if your file manager asks)
# to install $APP_NAME as a normal application, including the one-time
# "dialout" group setup a serial QR scanner needs. After this, launch it
# from your Activities/Applications menu like anything else.
set -eu
cd "\$(dirname "\$0")"

INSTALL_DIR="\$HOME/.local/share/ar-qr-matching"
DESKTOP_DIR="\$HOME/.local/share/applications"
mkdir -p "\$INSTALL_DIR" "\$DESKTOP_DIR"

cp -f "$APP_NAME" "\$INSTALL_DIR/"
chmod +x "\$INSTALL_DIR/$APP_NAME"
cp -f icon.png "\$INSTALL_DIR/"

# A serial QR scanner needs the "dialout" group, which only takes effect
# after a fresh login - the desktop icon instead launches through
# launch.sh below, which re-applies it fresh via sg on every run, so the
# scanner works the very first time, no logout ever required.
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

cat > "\$DESKTOP_DIR/ar-qr-matching.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=$APP_NAME
Comment=Match PCB and plastic case QR codes by scan position
Exec="\$INSTALL_DIR/launch.sh"
Icon=\$INSTALL_DIR/icon.png
Terminal=false
Categories=Office;
DESKTOP
chmod +x "\$DESKTOP_DIR/ar-qr-matching.desktop"
update-desktop-database "\$DESKTOP_DIR" >/dev/null 2>&1 || true

echo "Installed. Open your Activities/Applications menu and search for \"$APP_NAME\"."
echo "The serial QR scanner will work right away - no logout needed."
echo "This window will close in a few seconds..."
sleep 5
EOF
chmod +x "$PACKAGE_DIR/install.sh"

# run.sh is the quick alternative to install.sh - launches straight out of
# this folder with no system install, but still handles the one-time
# "dialout" group setup automatically.
cat > "$PACKAGE_DIR/run.sh" <<EOF
#!/usr/bin/env bash
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

cat > "$PACKAGE_DIR/README.txt" <<EOF
$APP_NAME - Linux
$(printf '=%.0s' $(seq 1 $((${#APP_NAME} + 8))))

Two ways to run this - pick whichever fits:

OPTION A: install once, then use it like any other app (recommended)
------------------------------------------------------------------
Double-click install.sh. If your file manager asks whether to "Run" or
"Display" it, choose Run (or Execute). A terminal window will flash up
briefly and close itself - that's it. From then on, open the
Activities/Applications menu (or the app grid) and click "$APP_NAME" -
exactly like opening DRD Accounting Tool or any other program.

OPTION B: run directly from this folder, no installation
------------------------------------------------------------------
Double-click run.sh (or run ./run.sh from a terminal).

HOW TO USE
----------
Scan tab:  scan every QR (PCB and case) in physical order. Each one gets a
           position number.
Match tab: click "Complete Scanning" (or the Match tab directly) once
           you're done. Scan a QR there and its position number appears in
           big text - find the matching part at that same position. Once
           scanned here, that QR's row is marked Checked so you always
           know what's left.

SERIAL QR SCANNER
------------------
A scanner connected as a serial device (e.g. /dev/ttyACM0) can be picked
from the "Serial Scanner Port" dropdown inside the app - a scan lands in
whichever tab (Scan or Match) is currently open.

Both Option A and Option B set up the one-time "dialout" group permission
automatically the first time you run install.sh/run.sh (you may be asked
for your password once) - no logout needed. If it still won't open (a
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
