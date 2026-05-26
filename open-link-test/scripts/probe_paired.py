"""Pairing-mode probe — split into two phases for interactive driving.

Usage:
  python probe_paired.py flash <hex>             — drag-flash one hex
  python probe_paired.py probe <key>             — BLE-probe + merge under <key>

Run flash, then A+B+Reset the mini manually, then run probe.
"""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).parent
HEX_DIR = HERE.parent  # open-link-test/
ROOT = HEX_DIR.parent  # calliope-flash-test/
sys.path.insert(0, str(ROOT / "flash-test" / "lib"))
sys.path.insert(0, str(ROOT / "ble-test"))

from probe import (  # type: ignore
    _wait_for_drive, _usb_flash_with_retry,
    HexResult, _read_regions,
)
from common import (
    find_calliope, log,
    UUID_PARTIAL_FLASH, UUID_NORDIC_DFU, UUID_BLOCKS,
    UUID_BUTTONLESS_WITHOUT_BONDS, UUID_BUTTONLESS_WITH_BONDS,
)
from bleak import BleakClient


async def cmd_flash(hex_name: str) -> None:
    log(f"=== flashing {hex_name} ===")
    await _wait_for_drive(45.0)
    elapsed = await _usb_flash_with_retry(HEX_DIR / hex_name)
    log(f"flashed in {elapsed:.1f}s — now A+B+Reset the mini, then run `probe`")


async def cmd_probe(key: str) -> HexResult:
    r = HexResult(hex_name=key)
    log(f"=== BLE probe ({key}) ===")
    pair = await find_calliope(timeout=15.0)
    if pair is None:
        r.error = "no BLE device found"
        log(r.error)
        return r
    device, adv = pair
    r.ble_found = True
    r.services.device_name = device.name or adv.local_name
    r.services.rssi = adv.rssi
    log(f"found {r.services.device_name} ({device.address}, rssi {r.services.rssi})")

    try:
        async with BleakClient(device) as client:
            uuids = []
            for s in client.services:
                u = str(s.uuid).lower()
                uuids.append(u)
                for c in s.characteristics:
                    cu = str(c.uuid).lower()
                    if cu == UUID_BUTTONLESS_WITHOUT_BONDS.lower():
                        r.services.buttonless_unbonded = True
                    elif cu == UUID_BUTTONLESS_WITH_BONDS.lower():
                        r.services.buttonless_bonded = True
            r.services.services = uuids
            r.services.partial_flash = UUID_PARTIAL_FLASH.lower() in uuids
            r.services.nordic_dfu = UUID_NORDIC_DFU.lower() in uuids
            r.services.mbitmore = UUID_BLOCKS.lower() in uuids
            log(f"services: PFS={r.services.partial_flash} DFU={r.services.nordic_dfu} "
                f"MbitMore={r.services.mbitmore}")
            log(f"buttonless: unbonded={r.services.buttonless_unbonded} "
                f"bonded={r.services.buttonless_bonded}")
            if r.services.partial_flash:
                await _read_regions(client, r)
    except Exception as e:
        r.error = f"BLE: {e}"
        log(r.error)
    return r


def merge(result: HexResult, key: str) -> None:
    out = HEX_DIR / "results" / "probe-results.json"
    out.parent.mkdir(exist_ok=True)
    if out.exists():
        data = json.loads(out.read_text())
        if isinstance(data, list):
            data = {"app_mode": data}
    else:
        data = {}
    entry = asdict(result)
    entry["__key"] = key
    data.setdefault("paired_mode", []).append(entry)
    out.write_text(json.dumps(data, indent=2))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: probe_paired.py flash <hex>  |  probe <key>")
        sys.exit(2)
    op = sys.argv[1]
    if op == "flash":
        if len(sys.argv) != 3:
            print("usage: probe_paired.py flash <hex>")
            sys.exit(2)
        asyncio.run(cmd_flash(sys.argv[2]))
    elif op == "probe":
        if len(sys.argv) != 3:
            print("usage: probe_paired.py probe <key>")
            sys.exit(2)
        res = asyncio.run(cmd_probe(sys.argv[2]))
        merge(res, sys.argv[2])
        log(f"saved as {sys.argv[2]}")
    else:
        print(f"unknown op: {op}")
        sys.exit(2)
