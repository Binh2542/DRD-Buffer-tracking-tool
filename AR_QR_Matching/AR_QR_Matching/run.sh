#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")"
chmod +x "./AR_QR_Matching" 2>/dev/null || true

if id -nG "$USER" | tr ' ' '\n' | grep -qx dialout; then
    exec "./AR_QR_Matching"
fi

echo "Adding $USER to the 'dialout' group (needed for a serial QR scanner) -"
echo "you may be asked for your password."
sudo usermod -aG dialout "$USER"
echo "Done - launching with the new group applied for this session (no logout needed)..."
exec sg dialout -c "exec './AR_QR_Matching'"
