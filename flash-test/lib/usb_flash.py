"""USB drag-flash via DAPLink mass-storage mount.

Copies a hex file to the mini's MSC drive (E: on this rig) and waits for
the bootloader to program + reboot. No DAPLink-command-level interaction
needed — drag-flash works at the filesystem level.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

MINI_DRIVE = "E:/"
HEX_NAME = "flash.hex"

# DAPLink takes ~3 s to write + reboot. After that the drive remounts.
POST_FLASH_SETTLE_S = 5.0


def usb_flash(hex_path: str | Path) -> float:
    """Drag-flash the given hex onto MINI:. Return elapsed seconds.

    Raises FileNotFoundError if MINI: isn't mounted.
    """
    src = Path(hex_path)
    if not src.is_file():
        raise FileNotFoundError(f"hex not found: {src}")
    if not Path(MINI_DRIVE).is_dir():
        raise FileNotFoundError(
            f"{MINI_DRIVE} not mounted — is the mini plugged in (and not in DfuTarg)?"
        )

    t0 = time.monotonic()
    dst = Path(MINI_DRIVE) / HEX_NAME
    # Use shutil.copyfile (no metadata) — DAPLink only cares about the bytes.
    shutil.copyfile(src, dst)

    # Wait for the drive to disappear (bootloader programming) then reappear
    # (reboot complete). On Windows this is observable via os.path.exists.
    # If the bootloader is fast we may never see "disappear"; that's fine.
    deadline = t0 + 20.0
    saw_disappear = False
    while time.monotonic() < deadline:
        if not Path(MINI_DRIVE).is_dir():
            saw_disappear = True
            time.sleep(0.2)
            continue
        if saw_disappear:
            break
        time.sleep(0.2)

    # Settle: bootloader needs a beat after re-mount before BLE/REPL is up.
    time.sleep(POST_FLASH_SETTLE_S)
    return time.monotonic() - t0


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("usage: usb_flash.py <hex>")
        raise SystemExit(2)
    elapsed = usb_flash(sys.argv[1])
    print(f"flashed in {elapsed:.1f} s")
