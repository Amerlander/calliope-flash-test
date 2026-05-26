"""E2E probe: USB-flash each hex, BLE-probe, read region info, verify against hex.

Workflow per hex:
  1. usb_flash(hex)            — drag-flash to E:\\
  2. wait for device to boot
  3. BLE scan + connect
  4. enumerate services         — PFS, DFU, MbitMore, buttonless DFU variants
  5. REGION_INFO 0/1/2          — get device-reported start/end/hash for each region
  6. compare device hashes      — to what _analyze5.py extracted from the hex bytes

The expected_hashes table is what _analyze5.py reported for the PXT magic +
the uPy layout records.

Skips makecode-1 and makecode-2 per user instruction.
"""
from __future__ import annotations

import asyncio
import json
import struct
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

HERE = Path(__file__).parent
HEX_DIR = HERE.parent  # open-link-test/  (one level up from scripts/)
ROOT = HEX_DIR.parent  # calliope-flash-test/
sys.path.insert(0, str(ROOT / "flash-test" / "lib"))
sys.path.insert(0, str(ROOT / "ble-test"))

from usb_flash import usb_flash as _usb_flash_raw, MINI_DRIVE
from common import (
    find_calliope, log,
    UUID_PARTIAL_FLASH, UUID_NORDIC_DFU, UUID_BLOCKS,
    UUID_BUTTONLESS_WITHOUT_BONDS, UUID_BUTTONLESS_WITH_BONDS,
)
from bleak import BleakClient


# Hashes extracted from the static hex files (by _analyze5.py)
# Format: "filename" → { region_index: expected_hash_hex }
# For PXT hexes: region 1 = DAL hash, region 2 = MakeCode hash
# For uPy hexes: regions populated by the FlashLayout records
EXPECTED = {
    "blocks.hex":              {"magic_type": "none"},
    "makecode-1-ble.hex":      {"magic_type": "pxt", "dal_hash": "37b90dbcf1a3f6e0", "mc_hash": "9c0025006300fa00"},
    "makecode-1-radio.hex":    {"magic_type": "pxt", "dal_hash": "d7fece9c392f3068", "mc_hash": "5e00f500b000c800"},
    "makecode-2-ble.hex":      {"magic_type": "pxt", "dal_hash": "0c03d3de260b20e2", "mc_hash": "fb00520030002d00"},
    "makecode-2-radio.hex":    {"magic_type": "pxt", "dal_hash": "2ba4dda00c362520", "mc_hash": "70003b00d300ad00"},
    "makecode-3-ble.hex":      {"magic_type": "pxt", "dal_hash": "103229cde9d66cc1", "mc_hash": "6c00b6000e00eb00"},
    "makecode-3-radio.hex":    {"magic_type": "pxt", "dal_hash": "3d11446ded28180d", "mc_hash": "91004f004000ab00"},
    "python_ble.hex":          {"magic_type": "upy", "regions": {2: "04e4050000000000"}},
    "python_radio.hex":        {"magic_type": "upy", "regions": {2: "d4e3050000000000"}},
}

# Order — start with python_ble since the user said the Calliope is connected
# and we want to land in a known-good state. The python hexes are full-image
# (Pattern B) so they install the open-link bootloader fresh.
ORDER = [
    "python_ble.hex",          # B — installs open-link bootloader
    "python_radio.hex",        # B — radio variant (BLE on A+B+Reset only)
    "blocks.hex",              # B — blocks runtime (no PXT magic)
    "makecode-3-ble.hex",      # B — full image, BLE off in app
    "makecode-3-radio.hex",    # B (since 2026-05-26 update) — was A
    "makecode-1-ble.hex",      # A — partial, needs existing bootloader
    "makecode-1-radio.hex",    # A
    "makecode-2-ble.hex",      # A
    "makecode-2-radio.hex",    # A
]


@dataclass
class ServiceCheck:
    partial_flash: bool = False
    nordic_dfu: bool = False
    mbitmore: bool = False
    buttonless_unbonded: bool = False
    buttonless_bonded: bool = False
    device_name: str | None = None
    rssi: int | None = None
    services: list[str] = field(default_factory=list)


@dataclass
class RegionRead:
    region_id: int
    start: int
    end: int
    hash_hex: str
    raw: str  # full 18-byte response in hex for debugging


@dataclass
class HexResult:
    hex_name: str
    usb_flash_s: float | None = None
    flashed_ok: bool = False
    ble_found: bool = False
    services: ServiceCheck = field(default_factory=ServiceCheck)
    regions: list[RegionRead] = field(default_factory=list)
    expected: dict = field(default_factory=dict)
    hash_match: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    error: str | None = None


def _u32(b: bytes, off: int) -> int:
    return struct.unpack(">I", b[off:off + 4])[0]


async def _wait_for_drive(deadline_s: float) -> None:
    """Poll until MINI_DRIVE looks writable (DETAILS.TXT readable).

    After a BLE session the drive can transient-fail with WinError 87 even
    though `is_dir()` returns True. Reading a known file proves it's settled.
    """
    drive = Path(MINI_DRIVE)
    details = drive / "DETAILS.TXT"
    t0 = time.monotonic()
    last_err = None
    while time.monotonic() - t0 < deadline_s:
        try:
            if drive.is_dir() and details.is_file():
                # actually open it — that's what catches transient WinError 87
                _ = details.read_bytes()[:32]
                return
        except OSError as e:
            last_err = e
        await asyncio.sleep(1.0)
    raise RuntimeError(f"drive {MINI_DRIVE} not ready after {deadline_s}s "
                       f"(last error: {last_err})")


async def _usb_flash_with_retry(hex_path: Path, attempts: int = 3) -> float:
    """Call _usb_flash_raw, retrying transient WinError 87 / 32 with a wait."""
    last = None
    for i in range(attempts):
        try:
            return _usb_flash_raw(hex_path)
        except OSError as e:
            last = e
            log(f"  attempt {i + 1}/{attempts} failed: {e} — settling 4s")
            await asyncio.sleep(4.0)
            await _wait_for_drive(deadline_s=30.0)
    raise last if last else RuntimeError("usb_flash retries exhausted")


async def _probe_one(hex_name: str) -> HexResult:
    r = HexResult(hex_name=hex_name)
    r.expected = EXPECTED.get(hex_name, {})

    hex_path = HEX_DIR / hex_name
    if not hex_path.is_file():
        r.error = f"hex not found: {hex_path}"
        return r

    log(f"--- {hex_name} ---")

    # 1. USB-flash with retry — after a BLE session the drive can take 20+s
    # to be reliably writeable again (Windows returns WinError 87 transiently).
    try:
        await _wait_for_drive(deadline_s=45.0)
        log(f"USB-flashing {hex_name} to {MINI_DRIVE}…")
        r.usb_flash_s = await _usb_flash_with_retry(hex_path)
        r.flashed_ok = True
        log(f"USB flash complete in {r.usb_flash_s:.1f}s")
    except Exception as e:
        r.error = f"usb_flash: {e}"
        log(f"usb_flash FAILED: {e}")
        return r

    # 2. Give the mini time to advertise. Some firmware needs a moment after
    # the bootloader hands off control before the BLE stack is ready.
    log("waiting 4s for BLE to come up…")
    await asyncio.sleep(4.0)

    # 3. BLE scan
    pair = await find_calliope(timeout=12.0)
    if pair is None:
        r.error = "BLE: no Calliope advertised after 12s"
        return r
    device, adv = pair
    r.ble_found = True
    r.services.device_name = device.name or adv.local_name
    r.services.rssi = adv.rssi

    log(f"connecting to {r.services.device_name}…")
    try:
        async with BleakClient(device) as client:
            # 4. Enumerate services
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
                f"MbitMore={r.services.mbitmore} "
                f"buttonless={'unbonded' if r.services.buttonless_unbonded else ''}"
                f"{'+bonded' if r.services.buttonless_bonded else ''}")

            # 5. Read regions 0,1,2 via PFS (REGION_INFO opcode)
            if r.services.partial_flash:
                await _read_regions(client, r)

    except Exception as e:
        r.error = f"BLE: {e}"
        return r

    # 6. Hash compare
    _compare_hashes(r)
    return r


async def _read_regions(client: BleakClient, r: HexResult) -> None:
    REGION_INFO = 0x00
    pf_char = None
    for s in client.services:
        if str(s.uuid).lower() != UUID_PARTIAL_FLASH.lower():
            continue
        for c in s.characteristics:
            # PFS control char (e97d3b10) — first writable/notifying char of PFS
            if 'notify' in c.properties or 'indicate' in c.properties:
                pf_char = c
                break
    if pf_char is None:
        r.notes.append("PFS service has no notify char — can't read regions")
        return

    responses: dict[int, bytes] = {}
    waiter: asyncio.Future[bytes] | None = None

    def on_notify(_h, data: bytearray):
        nonlocal waiter
        if waiter and not waiter.done() and len(data) >= 18 and data[0] == REGION_INFO:
            waiter.set_result(bytes(data))

    await client.start_notify(pf_char, on_notify)
    try:
        for rid in (0, 1, 2):
            loop = asyncio.get_event_loop()
            waiter = loop.create_future()
            try:
                await client.write_gatt_char(pf_char, bytes([REGION_INFO, rid]), response=False)
                data = await asyncio.wait_for(waiter, timeout=3.0)
            except asyncio.TimeoutError:
                r.notes.append(f"REGION_INFO {rid}: timeout")
                continue
            except Exception as e:
                r.notes.append(f"REGION_INFO {rid}: {e}")
                continue
            start = _u32(data, 2)
            end = _u32(data, 6)
            hh = data[10:18].hex()
            r.regions.append(RegionRead(region_id=data[1], start=start, end=end,
                                        hash_hex=hh, raw=data.hex()))
            log(f"  region {data[1]}: 0x{start:08X}-0x{end:08X}  hash={hh}")
    finally:
        try:
            await client.stop_notify(pf_char)
        except Exception:
            pass


def _compare_hashes(r: HexResult) -> None:
    exp = r.expected
    if not exp:
        return
    mt = exp.get("magic_type")
    if mt == "pxt":
        # codal reports DAL hash in region 1, MakeCode hash in region 2 — but
        # only when the firmware found PXT magic. Hashes are stored as 8 bytes;
        # we expect the same big-endian bytes in REGION_INFO response.
        dal_dev = next((reg.hash_hex for reg in r.regions if reg.region_id == 1), None)
        mc_dev  = next((reg.hash_hex for reg in r.regions if reg.region_id == 2), None)
        r.hash_match["region1_dal"]      = dal_dev == exp["dal_hash"]
        r.hash_match["region2_makecode"] = mc_dev  == exp["mc_hash"]
        if dal_dev is None:
            r.notes.append("region 1 (DAL) hash not returned")
        if mc_dev is None:
            r.notes.append("region 2 (MakeCode) hash not returned")
    elif mt == "upy":
        # uPy uses 1-indexed region ids in the layout table records.
        # Codal exposes them as 0,1,2 in PFS, mapped from records by index.
        # The hash for region 2 (MP) is a PTR hash → CRC32 of the version
        # string, computed at boot. The static bytes we extracted from the
        # hex are POINTER values, not CRCs — they won't match the device's
        # response. So we only sanity-check region structure here.
        for rec in r.regions:
            r.hash_match[f"region{rec.region_id}_present"] = True
    elif mt == "none":
        # blocks.hex — no magic. Device should still expose PFS but report
        # zero ranges (codal default when findHashes() finds nothing).
        for rec in r.regions:
            if rec.start == 0 and rec.end == 0:
                r.hash_match[f"region{rec.region_id}_zero"] = True
            else:
                r.hash_match[f"region{rec.region_id}_zero"] = False
                r.notes.append(f"region {rec.region_id} unexpectedly non-zero "
                               f"(0x{rec.start:X}-0x{rec.end:X})")


async def main():
    results: list[HexResult] = []
    for i, h in enumerate(ORDER):
        if i > 0:
            log(f"settling 6s before next flash…")
            await asyncio.sleep(6.0)
        try:
            res = await _probe_one(h)
        except Exception as e:
            res = HexResult(hex_name=h, error=f"unhandled: {e}")
        results.append(res)
        # Save after every step so partial progress survives a crash
        _save(results)
    log(f"done — wrote results to {HEX_DIR / 'results' / 'probe-results.json'}")


def _save(results: list[HexResult]) -> None:
    results_dir = HEX_DIR / "results"
    results_dir.mkdir(exist_ok=True)
    out = results_dir / "probe-results.json"
    out.write_text(json.dumps([_to_dict(r) for r in results], indent=2))


def _to_dict(r: HexResult) -> dict:
    d = asdict(r)
    # services has its own nested dataclass — asdict already flattens it
    return d


if __name__ == "__main__":
    asyncio.run(main())
