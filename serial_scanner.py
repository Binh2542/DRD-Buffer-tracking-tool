import threading

import serial
import serial.tools.list_ports

# Most USB-serial ("TTL"/CDC-ACM) barcode scanners default to 9600 baud -
# not configurable from the UI for now since every scanner encountered so
# far uses this default; revisit if a scanner needs a different rate.
_DEFAULT_BAUDRATE = 9600

# A real scan arrives from the scanner as one fast, uninterrupted burst -
# a handful of milliseconds at typical UART speeds - so "no new bytes
# right now" is *itself* the end-of-scan signal, not a timeout/failure
# condition. This also covers scanners (some GM65 units, depending on how
# the suffix/terminator is configured on that specific unit) that never
# send a trailing newline at all - waiting for a "\n" that will never
# arrive was the actual cause of scans failing, not the scanner or the
# machine. This value is both the per-read poll window AND, since a real
# transmission keeps returning bytes well before it elapses, the
# effective "the scanner has gone quiet" gap - ~100x longer than one
# byte's transmission time at 9600 baud, so normal USB/driver scheduling
# jitter mid-transmission can never trigger it by mistake.
_POLL_TIMEOUT_SECONDS = 0.1

# How long to back off after a read error before retrying - keeps a
# persistent failure (device genuinely gone) from spinning the loop, but
# short enough that a transient hiccup recovers quickly.
_ERROR_RETRY_SECONDS = 0.5


def list_serial_ports() -> list[str]:
    """Device paths of every serial port currently visible to the OS (e.g.
    "/dev/ttyACM0" on Linux, "COM3" on Windows) - just a suggestion list
    for the port combobox, which also accepts free text, since a scanner
    can be plugged in after this is called or not enumerate cleanly."""
    return sorted(p.device for p in serial.tools.list_ports.comports())


class SerialScanner:
    """Reads QR/barcode scans from a serial-connected scanner (e.g. a
    handheld scanner wired up as /dev/ttyACM0 rather than a USB-HID
    "keyboard wedge" device) instead of a webcam or physical keyboard
    input.

    A scan is framed by *silence*, not a required terminator character:
    whatever accumulates between two quiet gaps of _POLL_TIMEOUT_SECONDS
    is treated as one complete scan. A trailing "\\n" (if the scanner
    does send one) still splits and emits immediately without waiting for
    that gap, so scanners that do send a terminator are unaffected - this
    is purely an addition for the ones that don't.

    Runs its own background reader thread; every decoded line is handed to
    `on_scan` via `root.after(0, ...)` so it always runs on the Tk main
    thread - Tkinter isn't thread-safe, so nothing here may touch widgets
    directly from the reader thread.

    Never disconnects itself on a read error - a flaky USB-serial link can
    throw transient I/O hiccups on a port that's still genuinely there,
    and treating those as fatal used to drop the connection and force a
    manual reconnect for what was really just noise. Only an explicit
    stop() (the user clicking Disconnect, or logging out) ever ends this
    connection now - a read error just backs off briefly, tries to reopen
    the port if it got closed, and keeps going.
    """

    def __init__(self, root, port: str, on_scan, baudrate: int = _DEFAULT_BAUDRATE):
        self.root = root
        self.port_name = port
        self.on_scan = on_scan
        self.baudrate = baudrate
        self._serial = None
        self._stop_event = threading.Event()
        self._thread = None

    @property
    def is_connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def start(self) -> None:
        """Open the port and start reading. Raises serial.SerialException
        on failure (bad/missing port, already in use, no permission, etc.)
        - synchronous and on the caller's thread so the caller can show an
        immediate error, unlike the background read loop's errors."""
        self._serial = serial.Serial(self.port_name, baudrate=self.baudrate, timeout=_POLL_TIMEOUT_SECONDS)
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _emit(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="ignore").strip()
        if text:
            self.root.after(0, lambda t=text: self.on_scan(t))

    def _read_loop(self) -> None:
        buffer = bytearray()
        while not self._stop_event.is_set():
            try:
                chunk = self._serial.read(4096)
            except (serial.SerialException, OSError):
                # Never fatal - see the class docstring. Try to reopen if
                # the port dropped, then back off briefly and keep going;
                # a truly-gone device just keeps failing quietly here
                # rather than forcing the operator through a manual
                # reconnect for what might be a one-off glitch.
                try:
                    if not self._serial.is_open:
                        self._serial.open()
                except Exception:  # noqa: BLE001 - still recoverable, retried next loop
                    pass
                if self._stop_event.wait(_ERROR_RETRY_SECONDS):
                    return
                continue
            if self._stop_event.is_set():
                return
            if chunk:
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, rest = buffer.partition(b"\n")
                    buffer = bytearray(rest)
                    self._emit(line)
                continue
            # No new bytes within this poll window - the scanner has gone
            # quiet (see _POLL_TIMEOUT_SECONDS). Whatever's sitting in the
            # buffer is a complete scan either way, terminator or not.
            if buffer:
                self._emit(buffer)
                buffer = bytearray()

    def stop(self) -> None:
        self._stop_event.set()
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:  # noqa: BLE001
                pass
            self._serial = None
