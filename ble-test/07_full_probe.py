"""07_full_probe: run the full BLE-DFU-without-bond probe sequence against
whatever Calliope-like device is currently advertising.

Prints a structured result table at the end:

  CONFIG          | BOOT | BL UUID    | MWWR | BL WRITE | REBOOT | DFU @ p=20 | NOTES
  ----------------+------+------------+------+----------+--------+------------+------------

Run after flashing a test hex. The script auto-discovers the device,
detects whether it's in app mode or bootloader mode, and runs the
appropriate test sequence.
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import zlib
from typing import Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

from common import (
    UUID_BUTTONLESS_WITH_BONDS,
    UUID_BUTTONLESS_WITHOUT_BONDS,
    UUID_DFU_CONTROL_POINT,
    UUID_DFU_PACKET,
    UUID_NORDIC_DFU,
    UUID_PARTIAL_FLASH,
    UUID_UART,
    is_calliope_adv,
    log,
)

OP_CREATE = 0x01
OP_SET_PRN = 0x02
OP_CALC_CRC = 0x03
OP_EXECUTE = 0x04
OP_SELECT = 0x06
OP_RESPONSE = 0x60
OBJ_COMMAND = 0x01
OBJ_DATA = 0x02
RES_SUCCESS = 0x01


def make_init_packet(app_size: int) -> bytes:
    """Build the microbit_dfu_app_t init packet with hash_size=0 so the
    bootloader's NO_VALIDATION path commits without checking the SHA256.
    Mirrors widget's createInitPacketV2 but with the no-validation hash."""
    import struct as _s
    magic = b"microbit_app"  # 12 bytes
    version = 1
    hash_size = 0  # tells bootloader to skip SHA256 verification
    hash_bytes = b"\x00" * 32
    return magic + _s.pack("<III", version, app_size, hash_size) + hash_bytes


async def find(timeout: float = 8.0) -> tuple[BLEDevice, AdvertisementData] | None:
    log(f"  scanning {timeout}s…")
    found: dict[str, tuple[BLEDevice, AdvertisementData]] = {}

    def cb(d: BLEDevice, adv: AdvertisementData) -> None:
        if d.address in found:
            return
        if is_calliope_adv(d, adv):
            found[d.address] = (d, adv)

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    try:
        await asyncio.sleep(timeout)
    finally:
        await scanner.stop()

    if not found:
        return None
    return max(found.values(), key=lambda p: p[1].rssi)


async def probe_in_app(client: BleakClient) -> dict:
    """Device is in app mode. Check what buttonless characteristic is
    exposed + try to write 0x01 to trigger bootloader entry."""
    result = {
        "bl_uuid": None,
        "bl_mwwr": None,
        "bl_write_ok": None,
        "bl_write_error": None,
        "reboot_seen": None,
    }

    nordic_dfu = client.services.get_service(UUID_NORDIC_DFU)
    if nordic_dfu is None:
        result["bl_write_error"] = "Nordic DFU service not present in app"
        return result

    # Find buttonless characteristic — try without-bonds first.
    bl_char: Optional[BleakGATTCharacteristic] = None
    for uuid, label in (
        (UUID_BUTTONLESS_WITHOUT_BONDS, "without-bonds (8EC90003)"),
        (UUID_BUTTONLESS_WITH_BONDS, "with-bonds (8EC90004)"),
    ):
        char = client.services.get_characteristic(uuid)
        if char is not None:
            bl_char = char
            result["bl_uuid"] = uuid
            result["bl_mwwr"] = getattr(char, "max_write_without_response_size", None)
            log(f"  buttonless: {label}, mwwr={result['bl_mwwr']}")
            break

    if bl_char is None:
        result["bl_write_error"] = "no buttonless characteristic found"
        return result

    # Subscribe to indication/notification to receive the bootloader-enter ack.
    got_ack = asyncio.get_event_loop().create_future()

    def on_notify(_: BleakGATTCharacteristic, data: bytearray) -> None:
        log(f"  buttonless notify: {bytes(data).hex()}")
        if not got_ack.done():
            got_ack.set_result(bytes(data))

    try:
        await client.start_notify(bl_char, on_notify)
        log("  buttonless notifications subscribed")
    except BleakError as e:
        result["bl_write_error"] = f"subscribe failed: {e}"
        return result

    # Write 0x01 (Enter Bootloader) — with response, as buttonless is
    # write-with-response.
    log("  writing 0x01 to buttonless…")
    try:
        await client.write_gatt_char(bl_char, bytes([0x01]), response=True)
        result["bl_write_ok"] = True
        log("  write OK")
    except Exception as e:
        result["bl_write_ok"] = False
        result["bl_write_error"] = f"{type(e).__name__}: {e}"
        log(f"  write FAILED: {e}")
        try:
            await client.stop_notify(bl_char)
        except Exception:
            pass
        return result

    # Wait for indication and then the device-side disconnect.
    try:
        await asyncio.wait_for(got_ack, timeout=3.0)
    except asyncio.TimeoutError:
        log("  no buttonless ack within 3s (continuing — some builds don't ack)")

    # The device should disconnect within a couple of seconds.
    log("  waiting for device-side disconnect…")
    for _ in range(20):
        if not client.is_connected:
            result["reboot_seen"] = True
            log("  device disconnected — likely rebooting into bootloader")
            return result
        await asyncio.sleep(0.5)

    result["reboot_seen"] = False
    log("  device did NOT disconnect after buttonless write")
    try:
        await client.stop_notify(bl_char)
    except Exception:
        pass
    return result


async def probe_secure_dfu(device: BLEDevice, payload_size: int = 20, chunk_size: int = 4096) -> dict:
    """Device is in DfuTarg. Try to do a single 4 KB chunk transfer +
    Execute. Reports success / failure with detail."""
    result = {
        "dfu_connected": False,
        "dfu_chunk_xferred": False,
        "dfu_chunk_executed": False,
        "dfu_error": None,
    }

    log(f"  reconnecting to bootloader for Secure DFU transfer test…")
    client = BleakClient(device, timeout=20.0)
    try:
        await client.connect()
    except Exception as e:
        result["dfu_error"] = f"DFU connect failed: {e}"
        return result
    result["dfu_connected"] = True

    try:
        ctrl = client.services.get_characteristic(UUID_DFU_CONTROL_POINT)
        pkt = client.services.get_characteristic(UUID_DFU_PACKET)
        if not ctrl or not pkt:
            result["dfu_error"] = "DFU chars missing"
            return result

        waiters: dict[int, asyncio.Future[bytes]] = {}
        loop = asyncio.get_event_loop()

        def on(_, data: bytearray) -> None:
            if not data or data[0] != OP_RESPONSE:
                return
            op, res = data[1], data[2]
            fut = waiters.pop(op, None)
            if fut and not fut.done():
                if res == RES_SUCCESS:
                    fut.set_result(bytes(data[3:]))
                else:
                    fut.set_exception(RuntimeError(f"op 0x{op:02x} res=0x{res:02x}"))

        await client.start_notify(ctrl, on)

        async def cmd(op: int, body: bytes = b"", timeout: float = 30.0) -> bytes:
            f: asyncio.Future[bytes] = loop.create_future()
            waiters[op] = f
            await client.write_gatt_char(ctrl, bytes([op]) + body, response=True)
            return await asyncio.wait_for(f, timeout=timeout)

        # ---- Init packet phase (small enough to not need PRN) ----
        await cmd(OP_SET_PRN, struct.pack("<H", 0))
        init_packet = make_init_packet(app_size=chunk_size)
        log(f"  init packet: {len(init_packet)} B (NO_VALIDATION hash)")
        await cmd(OP_SELECT, bytes([OBJ_COMMAND]))
        await cmd(OP_CREATE, struct.pack("<BI", OBJ_COMMAND, len(init_packet)))
        for i in range(0, len(init_packet), payload_size):
            await client.write_gatt_char(pkt, init_packet[i:i + payload_size], response=False)
        cs = await cmd(OP_CALC_CRC)
        ip_off, ip_crc = struct.unpack("<II", cs[:8])
        ip_expected_crc = zlib.crc32(init_packet) & 0xFFFFFFFF
        if ip_off != len(init_packet) or ip_crc != ip_expected_crc:
            result["dfu_error"] = f"init packet CRC mismatch (dev={ip_off}/{ip_crc:08x})"
            return result
        await cmd(OP_EXECUTE, timeout=30.0)
        log("  init packet committed ✓")

        # ---- Data phase, with PRN flow control ----
        # PRN=6 keeps us within Nordic SDK's NRF_DFU_BLE_BUFFERS=8 RX window.
        # Without PRN the bootloader silently drops late packets after the
        # buffer fills (seen empirically: 220 bytes lost at PRN=0).
        prn_interval = 6
        await cmd(OP_SET_PRN, struct.pack("<H", prn_interval))
        await cmd(OP_SELECT, bytes([OBJ_DATA]))
        await cmd(OP_CREATE, struct.pack("<BI", OBJ_DATA, chunk_size), timeout=30.0)

        # Stream the chunk in payload_size packets, blocking on PRN every N.
        data = bytes((i % 256) for i in range(chunk_size))
        offset = 0
        packets_since_prn = 0
        log(f"  streaming {chunk_size}B in {payload_size}B packets, PRN every {prn_interval}…")
        while offset < len(data):
            end = min(offset + payload_size, len(data))
            slice_ = data[offset:end]
            packets_since_prn += 1

            # If this packet completes a PRN batch, register the waiter
            # BEFORE writing so the notify can land in it.
            prn_fut: asyncio.Future[bytes] | None = None
            if packets_since_prn == prn_interval:
                prn_fut = loop.create_future()
                waiters[OP_CALC_CRC] = prn_fut

            await client.write_gatt_char(pkt, slice_, response=False)
            offset = end

            if prn_fut is not None:
                try:
                    p = await asyncio.wait_for(prn_fut, timeout=15.0)
                except asyncio.TimeoutError:
                    waiters.pop(OP_CALC_CRC, None)
                    result["dfu_error"] = f"PRN timeout at offset {offset}"
                    return result
                dev_o, dev_c = struct.unpack("<II", p[:8])
                expected = zlib.crc32(data[:offset]) & 0xFFFFFFFF
                if dev_o != offset or dev_c != expected:
                    result["dfu_error"] = f"PRN CRC mismatch at offset {offset} (dev={dev_o}/{dev_c:08x})"
                    return result
                packets_since_prn = 0

        # Verify
        p = await cmd(OP_CALC_CRC, timeout=30.0)
        dev_off, dev_crc = struct.unpack("<II", p[:8])
        expected_crc = zlib.crc32(data) & 0xFFFFFFFF
        if dev_off == chunk_size and dev_crc == expected_crc:
            result["dfu_chunk_xferred"] = True
            log(f"  chunk transferred ✓ (offset={dev_off}, crc match)")
        else:
            result["dfu_error"] = f"CRC/offset mismatch (dev={dev_off}/{dev_crc:08x}, want={chunk_size}/{expected_crc:08x})"
            return result

        # Execute (commit the chunk to flash — non-destructive since we wrote
        # synthetic bytes, the device will likely brick its app but that's OK
        # for probe purposes; user re-flashes RECOVERY next).
        await cmd(OP_EXECUTE, timeout=30.0)
        result["dfu_chunk_executed"] = True
        log(f"  Execute OK — bootloader committed the chunk to flash")

    except Exception as e:
        result["dfu_error"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            await client.stop_notify(UUID_DFU_CONTROL_POINT)
        except Exception:
            pass
        try:
            await client.disconnect()
        except Exception:
            pass

    return result


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--label", default="?", help="config label for the result row")
    p.add_argument("--skip-dfu", action="store_true", help="don't actually attempt the DFU transfer")
    args = p.parse_args()

    log(f"=== probing config={args.label} ===")

    pair = await find()
    if not pair:
        log("  NO DEVICE FOUND — flash and try again")
        print(f"\nRESULT: {args.label} | NO_ADV | - | - | - | - | - | device not found")
        return

    device, adv = pair
    name = (device.name or adv.local_name or "")
    is_bootloader = "dfutarg" in name.lower()
    log(f"  device: {device.address}, name={name!r}, rssi={adv.rssi}, in_bootloader={is_bootloader}")

    if is_bootloader:
        # Already in DfuTarg — skip the in-app probe, just test the DFU
        # transfer. Used for repeated-DFU testing on stuck devices.
        if args.skip_dfu:
            print(f"\nRESULT: {args.label} | DFU_TARG | (bootloader) | - | - | - | SKIPPED")
            return
        dfu_result = await probe_secure_dfu(device)
        print()
        print(f"=== RESULT: {args.label} ===")
        print(f"  device state at start: DfuTarg (bootloader)")
        print(f"  DFU connect:           {'OK' if dfu_result['dfu_connected'] else 'FAIL'}")
        print(f"  DFU chunk transfer:    {'OK' if dfu_result['dfu_chunk_xferred'] else 'FAIL'}")
        print(f"  DFU Execute:           {'OK' if dfu_result['dfu_chunk_executed'] else 'FAIL'}")
        if dfu_result.get("dfu_error"):
            print(f"  error:                 {dfu_result['dfu_error']}")
        return

    # Device is in app mode — probe services + buttonless.
    log("  connecting to app…")
    async with BleakClient(device, timeout=20.0) as client:
        log(f"  connected, services={len(client.services.services)}")
        app_result = await probe_in_app(client)

    # If buttonless succeeded and rebooted, run DFU transfer test.
    dfu_result: dict = {}
    if app_result.get("reboot_seen") and not args.skip_dfu:
        await asyncio.sleep(2.5)  # let bootloader come up
        # Re-scan to confirm it's advertising as DfuTarg.
        log("  scanning for DfuTarg post-reboot…")
        pair2 = await find(timeout=6.0)
        if pair2 and "dfutarg" in (pair2[0].name or "").lower():
            dfu_result = await probe_secure_dfu(pair2[0])
        else:
            dfu_result = {"dfu_error": "device did not advertise DfuTarg post-reboot"}

    # Print structured result
    print()
    print(f"=== RESULT: {args.label} ===")
    print(f"  device:               {device.address} (name={name!r})")
    print(f"  buttonless UUID:      {app_result.get('bl_uuid') or '(none)'}")
    print(f"  buttonless mwwr:      {app_result.get('bl_mwwr')}")
    print(f"  buttonless write OK:  {app_result.get('bl_write_ok')}")
    if app_result.get('bl_write_error'):
        print(f"    error:              {app_result['bl_write_error']}")
    print(f"  device rebooted:      {app_result.get('reboot_seen')}")
    if dfu_result:
        print(f"  DFU re-connect:       {dfu_result.get('dfu_connected')}")
        print(f"  DFU chunk transfer:   {dfu_result.get('dfu_chunk_xferred')}")
        print(f"  DFU Execute:          {dfu_result.get('dfu_chunk_executed')}")
        if dfu_result.get('dfu_error'):
            print(f"    error:              {dfu_result['dfu_error']}")
    elif not args.skip_dfu and app_result.get("reboot_seen"):
        print(f"  DFU step:             skipped (no reboot detected)")


if __name__ == "__main__":
    asyncio.run(main())
