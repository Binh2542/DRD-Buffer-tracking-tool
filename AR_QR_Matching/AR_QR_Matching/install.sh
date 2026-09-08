#!/usr/bin/env bash
# Run this ONCE (double-click it, choose "Run" if your file manager asks)
# to install AR_QR_Matching as a normal application, including the one-time
# "dialout" group setup a serial QR scanner needs. After this, launch it
# from your Activities/Applications menu like anything else.
set -eu
cd "$(dirname "$0")"

INSTALL_DIR="$HOME/.local/share/ar-qr-matching"
DESKTOP_DIR="$HOME/.local/share/applications"
mkdir -p "$INSTALL_DIR" "$DESKTOP_DIR"

cp -f "AR_QR_Matching" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/AR_QR_Matching"
cp -f icon.png "$INSTALL_DIR/"

# A serial QR scanner needs the "dialout" group, which only takes effect
# after a fresh login - the desktop icon instead launches through
# launch.sh below, which re-applies it fresh via sg on every run, so the
# scanner works the very first time, no logout ever required.
if ! id -nG "$USER" | tr ' ' '\n' | grep -qx dialout; then
    echo "Adding $USER to the 'dialout' group (needed for a serial QR scanner) -"
    echo "you may be asked for your password."
    sudo usermod -aG dialout "$USER"
fi

cat > "$INSTALL_DIR/launch.sh" <<LAUNCH
#!/usr/bin/env bash
exec sg dialout -c "exec '$INSTALL_DIR/AR_QR_Matching'"
LAUNCH
chmod +x "$INSTALL_DIR/launch.sh"

cat > "$DESKTOP_DIR/ar-qr-matching.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=AR_QR_Matching
Comment=Match PCB and plastic case QR codes by scan position
Exec="$INSTALL_DIR/launch.sh"
Icon=$INSTALL_DIR/icon.png
Terminal=false
Categories=Office;
DESKTOP
chmod +x "$DESKTOP_DIR/ar-qr-matching.desktop"
update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true

echo "Installed. Open your Activities/Applications menu and search for \"AR_QR_Matching\"."
echo "The serial QR scanner will work right away - no logout needed."
echo "This window will close in a few seconds..."
sleep 5
