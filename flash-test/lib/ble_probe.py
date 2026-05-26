"""Lightweight BLE inspection — services + DAL/MakeCode region hashes.

Mirrors the assertions the test runner needs after each flash phase:

  - "Is the mini advertising at all?"     (scan)
  - "Are we in app mode or pairing mode?" (service set)
  - "What runtime hash does it expose?"   (partial-flash region 1 read)
  - "What does the filesystem hash look like?" (region 2)

The actual scan/connect logic is identical to ble-test/02_inspect.py — we
reuse the same `common.py` helpers.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

# Reuse the existing helpers — same workspace, no need to vendor them.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ble-test"))
from common import find_calliope, connect, log  # noqa: E402

PF_SERVICE = "e97dd91d-251d-470a-a062-fa1922dfa9a8"
PF_CHAR = "e97d3b10-251d-470a-a062-fa1922dfa9a8"
MBITMORE_SERVICE = "0b50f3e4-607f-4151-9091-7d008d6ffc5c"
DFU_SERVICE = "0000fe59-0000-1000-8000-00805f9b34fb"
BUTTONLESS_UNBONDED_CHAR = "8ec90003-f315-4f60-9fb8-838830daea50"
BUTTONLESS_BONDED_CHAR = "8ec90004-f315-4f60-9fb8-838830daea50"


@dataclass
class BleProbe:
    found: bool
    name: str | None = None
    rssi: int | None = None
    has_partial_flash: bool = False
    has_mbitmore: bool = False
    has_dfu: bool = False
    has_unbonded_dfu: bool = False
    has_bonded_dfu: bool = False
    runtime_hash_hex: str | None = None  # device-side hash for region 1 (DAL slot)
    makecode_hash_hex: str | None = None  # region 2 (MakeCode/FS slot)
    error: str | None = None


async def _read_region_hash(pf_char_handle, region_id: int, timeout_s: float = 3.0) -> bytes | None:
    """Issue a REGION_INFO request and parse the 8-byte hash from the
    notification. Implements the same wire protocol the widget uses
    (ble-flash-web.ts requestRegion()).

    Returns None on timeout.
    """
    REGION_INFO = 0x00  # opcode in the partial flashing protocol

    response_future: asyncio.Future[bytes] = asyncio.get_event_loop().create_future()

    def _on_notify(_h, data: bytearray):
        if response_future.done():
            return
        # buffer[0] = REGION_INFO, [1] = region id, [2..6] = start, [6..10] = end, [10..18] = hash
        if len(data) >= 18 and data[0] == REGION_INFO and data[1] == region_id:
            response_future.set_result(bytes(data[10:18]))

    await pf_char_handle.start_notify(_on_notify)
    try:
        await pf_char_handle.write_gatt_char(
            None, bytes([REGION_INFO, region_id]), response=False
        )
        return await asyncio.wait_for(response_future, timeout=timeout_s)
    except (asyncio.TimeoutError, Exception):
        return None
    finally:
        try:
            await pf_char_handle.stop_notify()
        except Exception:
            pass


async def _probe() -> BleProbe:
    pair = await find_calliope()
    if not pair:
        return BleProbe(found=False, error="no Calliope in scan")
    device, adv = pair

    p = BleProbe(found=True, name=device.name or adv.local_name, rssi=adv.rssi)

    try:
        async with connect(device) as client:
            services = {str(s.uuid).lower() for s in client.services}
            p.has_partial_flash = PF_SERVICE in services
            p.has_mbitmore = MBITMORE_SERVICE in services
            p.has_dfu = DFU_SERVICE in services
            # buttonless variants live inside the DFU service
            for s in client.services:
                for c in s.characteristics:
                    cu = str(c.uuid).lower()
                    if cu == BUTTONLESS_UNBONDED_CHAR:
                        p.has_unbonded_dfu = True
                    elif cu == BUTTONLESS_BONDED_CHAR:
                        p.has_bonded_dfu = True
            # Region hash reads are deferred — they need a more involved
            # notify-and-write dance via the PF control characteristic. The
            # runner uses ble_partial.py for that. ble_probe just gives a
            # quick "is the device in the expected mode?" answer.
    except Exception as e:
        p.error = f"connect/probe failed: {e}"

    return p


def probe_sync() -> BleProbe:
    return asyncio.run(_probe())


if __name__ == "__main__":
    p = probe_sync()
    if not p.found:
        print(f"NOT FOUND: {p.error}")
        sys.exit(1)
    print(f"found {p.name} rssi={p.rssi}")
    print(f"  partial_flash={p.has_partial_flash}  mbitmore={p.has_mbitmore}")
    print(f"  unbonded_dfu={p.has_unbonded_dfu}  bonded_dfu={p.has_bonded_dfu}")
    # Heuristic mode classification
    if p.has_mbitmore:
        print("  mode: app-mode running Blocks/scratch runtime")
    elif p.has_partial_flash and not p.has_mbitmore:
        print("  mode: app-mode (MicroPython or empty Blocks) — pairing mode after A+B+Reset")
    elif p.has_dfu and not p.has_partial_flash:
        print("  mode: probably bootloader (DfuTarg)")
    else:
        print("  mode: unknown profile")
