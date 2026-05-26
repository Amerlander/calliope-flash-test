"""Shared helpers for the Calliope BLE test suite."""

from __future__ import annotations

import asyncio
import io
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator, Iterable

# Force UTF-8 stdout so non-ASCII glyphs (→ ✅ ❌ etc.) survive the
# Windows cp1252 default console encoding. Has to happen before any
# print() runs.
if sys.platform == "win32" and isinstance(sys.stdout, io.TextIOWrapper):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from bleak import BleakScanner, BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData


# ---- Service / characteristic UUIDs ---------------------------------------
# Mirror of mini-connection-widget/src/ble-state.ts SERVICE_UUIDS.

UUID_PARTIAL_FLASH = "e97dd91d-251d-470a-a062-fa1922dfa9a8"
UUID_NORDIC_DFU = "0000fe59-0000-1000-8000-00805f9b34fb"
UUID_UART = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
UUID_DEVICE_INFO = "0000180a-0000-1000-8000-00805f9b34fb"
UUID_BLOCKS = "0b50f3e4-607f-4151-9091-7d008d6ffc5c"

# Nordic Secure DFU in-app buttonless characteristics
UUID_BUTTONLESS_WITH_BONDS = "8ec90004-f315-4f60-9fb8-838830daea50"
UUID_BUTTONLESS_WITHOUT_BONDS = "8ec90003-f315-4f60-9fb8-838830daea50"

# Bootloader DFU service characteristics
UUID_DFU_CONTROL_POINT = "8ec90001-f315-4f60-9fb8-838830daea50"
UUID_DFU_PACKET = "8ec90002-f315-4f60-9fb8-838830daea50"


def ts() -> str:
    """Timestamp prefix matching the widget's `+X.XXs` style for log alignment."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(msg: str) -> None:
    print(f"[{ts()}] {msg}", flush=True)


def is_calliope_adv(d: BLEDevice, adv: AdvertisementData) -> bool:
    """Heuristic: name contains 'Calliope' or 'BBC micro:bit' or 'DfuTarg'."""
    name = (d.name or adv.local_name or "") or ""
    if "calliope" in name.lower():
        return True
    if "micro:bit" in name.lower() or "microbit" in name.lower():
        return True
    if "dfutarg" in name.lower():
        return True
    # Service UUID match — devices in bootloader sometimes advertise only the
    # DFU service UUID with the generic name "DfuTarg".
    if adv.service_uuids:
        suuids = [str(u).lower() for u in adv.service_uuids]
        if any(UUID_NORDIC_DFU.lower() in u for u in suuids):
            return True
        if any(UUID_PARTIAL_FLASH.lower() in u for u in suuids):
            return True
    return False


async def find_calliope(timeout: float = 8.0) -> tuple[BLEDevice, AdvertisementData] | None:
    """Scan for the first Calliope-like device and return it."""
    log(f"scanning for Calliope ({timeout}s)…")
    found: dict[str, tuple[BLEDevice, AdvertisementData]] = {}

    def on_adv(d: BLEDevice, adv: AdvertisementData) -> None:
        if d.address in found:
            return
        if is_calliope_adv(d, adv):
            name = d.name or adv.local_name or "<unnamed>"
            log(f"  found {d.address} — {name} (rssi={adv.rssi})")
            found[d.address] = (d, adv)

    scanner = BleakScanner(detection_callback=on_adv)
    await scanner.start()
    try:
        await asyncio.sleep(timeout)
    finally:
        await scanner.stop()

    if not found:
        log("no Calliope found")
        return None
    # Return the strongest-signal one
    return max(found.values(), key=lambda pair: pair[1].rssi)


@asynccontextmanager
async def connect(device: BLEDevice) -> AsyncIterator[BleakClient]:
    """Connect with a generous timeout + ensure clean disconnect on exit."""
    log(f"connecting to {device.address} ({device.name})…")
    client = BleakClient(device, timeout=20.0)
    await client.connect()
    log(f"  connected (services: {len(client.services.services)})")
    try:
        yield client
    finally:
        try:
            await client.disconnect()
            log("  disconnected")
        except Exception as e:
            log(f"  disconnect threw: {e}")


def fmt_uuid(uuid_str: str) -> str:
    """Shorten 128-bit UUIDs to their Nordic/CODAL-known short forms."""
    s = uuid_str.lower()
    known = {
        UUID_PARTIAL_FLASH.lower(): "Partial Flashing",
        UUID_NORDIC_DFU.lower(): "Nordic DFU",
        UUID_UART.lower(): "UART (Nordic)",
        UUID_DEVICE_INFO.lower(): "Device Information (SIG)",
        UUID_BLOCKS.lower(): "Blocks (MbitMore)",
        UUID_BUTTONLESS_WITH_BONDS.lower(): "Buttonless DFU (with-bonds)",
        UUID_BUTTONLESS_WITHOUT_BONDS.lower(): "Buttonless DFU (without-bonds)",
        UUID_DFU_CONTROL_POINT.lower(): "Secure DFU Control Point",
        UUID_DFU_PACKET.lower(): "Secure DFU Packet",
    }
    return known.get(s, uuid_str)


def props_short(props: Iterable[str]) -> str:
    """Compact property list: 'read|write|notify' etc."""
    return "|".join(props)
