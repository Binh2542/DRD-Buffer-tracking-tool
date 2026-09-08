AR_QR_Matching - Linux
======================

Two ways to run this - pick whichever fits:

OPTION A: install once, then use it like any other app (recommended)
------------------------------------------------------------------
Double-click install.sh. If your file manager asks whether to "Run" or
"Display" it, choose Run (or Execute). A terminal window will flash up
briefly and close itself - that's it. From then on, open the
Activities/Applications menu (or the app grid) and click "AR_QR_Matching" -
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
    sudo usermod -aG dialout $USER
