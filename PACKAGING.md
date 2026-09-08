# Building a portable app

`build.spec` produces a self-contained, double-clickable app - no Python
install needed on the machine that runs it. It must be built **on the
target OS**: a Windows build only produces a `.exe`, a macOS build only
produces a `.app` (PyInstaller does not cross-compile).

## Windows -> `.exe`

Already built and tested - `dist/DRD Buffer Tracking Tool.exe` is a single
portable file. To rebuild after code changes:

```
pip install -r requirements.txt -r requirements-build.txt
pyinstaller build.spec --noconfirm
```

Output: `dist/DRD Buffer Tracking Tool.exe`.

## macOS -> `.app`

**This must be run on an actual Mac** - these steps are prepared and the
spec is written to handle macOS automatically, but I can't run or test them
myself from Windows. Someone with a Mac needs to:

1. Install Python 3.11+ (e.g. `brew install python@3.11`) and Google Chrome.
2. Copy this whole project folder to the Mac.
3. In Terminal, from the project folder:
   ```
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt -r requirements-build.txt
   pyinstaller build.spec --noconfirm
   ```
4. Output: `dist/DRD Buffer Tracking Tool.app` - double-click to run, or
   drag it into `/Applications`.

### Distributing to other people's Macs

An unsigned `.app` built this way will be blocked by Gatekeeper on first
launch ("cannot be opened because the developer cannot be verified"). The
person opening it just needs to **right-click the app -> Open** once (not
double-click) and confirm - after that it opens normally. Proper code
signing/notarization needs a paid Apple Developer account and isn't set up
here.

## What goes next to the built app either way

The build only bundles the Python code and the icon - these files must be
placed in the **same folder as the `.exe`/`.app`** (they're user-specific,
not baked into the build):

- `firebase_key.json` (Firebase service account key)
- `.env` (contains `GOOGLE_SHEET_ID`)

`chrome_profile/` (browser session) and `.chromedriver_path` (cached driver
path) are created automatically next to the app on first run.
