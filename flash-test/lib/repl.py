"""USB CDC serial REPL probe for the MicroPython firmware on the mini.

Used by the test runner to verify "MicroPython is running" without
needing BLE. The mini's DAPLink interface chip exposes a USB serial
endpoint on COM8 (on this rig) at 115200 baud.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import serial


COM_PORT = "COM8"
BAUD = 115200


@dataclass
class ReplProbe:
    alive: bool
    implementation: str | None = None
    main_py_content: str | None = None
    error: str | None = None


def probe(timeout_s: float = 3.0) -> ReplProbe:
    """Open COM8, Ctrl-C the REPL, query sys.implementation. Optionally read
    /main.py via os.listdir + open. Return what we found.
    """
    try:
        s = serial.Serial(COM_PORT, BAUD, timeout=timeout_s)
    except serial.SerialException as e:
        return ReplProbe(alive=False, error=f"open {COM_PORT} failed: {e}")
    try:
        time.sleep(0.5)
        s.reset_input_buffer()
        # Ctrl-C + newline to drop any running program and get a fresh prompt.
        s.write(b"\x03\r\n")
        time.sleep(0.5)
        banner = s.read(512)
        if b">>>" not in banner:
            return ReplProbe(alive=False, error=f"no REPL prompt: {banner!r}")

        s.write(b"import sys;print(sys.implementation)\r\n")
        time.sleep(0.8)
        out = s.read(2048).decode(errors="replace")
        impl_line = next(
            (line for line in out.splitlines() if line.startswith("(name=")),
            None,
        )

        # Try to read main.py if present. Will fail silently if not — that's
        # informational, not a hard error.
        main_py = None
        s.write(b"try:\r\n  print('==MAIN_PY_START==\\n' + open('main.py').read() + '\\n==MAIN_PY_END==')\r\nexcept Exception as e:\r\n  print('main.py read failed:', e)\r\n\r\n")
        time.sleep(1.0)
        out2 = s.read(4096).decode(errors="replace")
        if "==MAIN_PY_START==" in out2 and "==MAIN_PY_END==" in out2:
            start = out2.index("==MAIN_PY_START==") + len("==MAIN_PY_START==\r\n")
            end = out2.index("==MAIN_PY_END==")
            main_py = out2[start:end].strip()

        return ReplProbe(alive=True, implementation=impl_line, main_py_content=main_py)
    finally:
        try:
            s.close()
        except Exception:
            pass


if __name__ == "__main__":
    p = probe()
    print(f"alive: {p.alive}")
    if p.implementation:
        print(f"impl : {p.implementation}")
    if p.main_py_content is not None:
        print(f"main.py:\n{p.main_py_content}")
    if p.error:
        print(f"error: {p.error}")
