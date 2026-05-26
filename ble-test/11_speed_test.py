"""11_speed_test: time a full 4 KB chunk transfer two ways:

  A. writeWithoutResponse, 20 B per packet, PRN=6  (= current widget)
  B. writeWithResponse,   244 B per packet, PRN=6 (= iOS-style)

Report wire speed in B/s for each.
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import time
import zlib

from bleak import BleakClient
from common import (
    UUID_DFU_CONTROL_POINT,
    UUID_DFU_PACKET,
    connect,
    find_calliope,
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
    return b"microbit_app" + struct.pack("<III", 1, app_size, 0) + b"\x00" * 32


async def stream_chunk(client, ctrl, pkt, cmd, chunk_data: bytes, *, payload: int, prn: int, with_response: bool) -> tuple[float, bool]:
    """Stream one chunk; return (elapsed_sec, success)."""
    waiters_local = {}
    loop = asyncio.get_event_loop()

    # Need to share waiters with main `cmd` closure — we use the same waiters
    # the caller passes in. Pass through here for clarity.
    await cmd(OP_SET_PRN, struct.pack("<H", prn))
    await cmd(OP_SELECT, bytes([OBJ_DATA]))
    await cmd(OP_CREATE, struct.pack("<BI", OBJ_DATA, len(chunk_data)), timeout=15.0)

    t0 = time.perf_counter()
    offset = 0
    packets_since_prn = 0

    while offset < len(chunk_data):
        end = min(offset + payload, len(chunk_data))
        slice_ = chunk_data[offset:end]
        packets_since_prn += 1

        # If PRN enabled and this packet completes a batch, set up the
        # response waiter in the shared dict BEFORE writing.
        prn_fut = None
        if prn > 0 and packets_since_prn == prn:
            # Setup will be done via cmd() pattern after write — but PRN comes
            # as a CalcChecksum response opcode, so we need to register a
            # waiter. The caller's `cmd` framework expects a write-then-await
            # pattern; PRN is the device push-init notification using the same
            # opcode. Reuse: register a waiter on OP_CALC_CRC opcode, wait for
            # it after the write completes.
            prn_fut = loop.create_future()
            # Inject into the cmd waiter dict — see main()
            _shared_waiters[OP_CALC_CRC] = prn_fut

        try:
            await client.write_gatt_char(pkt, slice_, response=with_response)
        except Exception as e:
            log(f"  write failed at offset {offset}: {e}")
            if prn_fut is not None:
                _shared_waiters.pop(OP_CALC_CRC, None)
            return (time.perf_counter() - t0, False)

        offset = end

        if prn_fut is not None:
            try:
                p = await asyncio.wait_for(prn_fut, timeout=15.0)
            except asyncio.TimeoutError:
                _shared_waiters.pop(OP_CALC_CRC, None)
                log(f"  PRN timeout at offset {offset}")
                return (time.perf_counter() - t0, False)
            dev_o, dev_c = struct.unpack("<II", p[:8])
            expected = zlib.crc32(chunk_data[:offset]) & 0xFFFFFFFF
            if dev_o != offset or dev_c != expected:
                log(f"  PRN CRC mismatch at offset {offset}: dev={dev_o}/{dev_c:08x}")
                return (time.perf_counter() - t0, False)
            packets_since_prn = 0

    # Final CalcChecksum to confirm full chunk landed
    cs = await cmd(OP_CALC_CRC, timeout=15.0)
    dev_off, dev_crc = struct.unpack("<II", cs[:8])
    expected_crc = zlib.crc32(chunk_data) & 0xFFFFFFFF
    elapsed = time.perf_counter() - t0
    ok = (dev_off == len(chunk_data) and dev_crc == expected_crc)
    if not ok:
        log(f"  final CRC mismatch: dev_off={dev_off}, dev_crc=0x{dev_crc:08x}, expected_off={len(chunk_data)}, expected_crc=0x{expected_crc:08x}")
    return (elapsed, ok)


# Shared waiter dict — needs to be module-level so stream_chunk can inject.
_shared_waiters: dict[int, asyncio.Future] = {}


async def main() -> None:
    pair = await find_calliope()
    if not pair:
        log("no device"); return
    device, _ = pair
    if "dfutarg" not in (device.name or "").lower():
        log(f"device not in DfuTarg (name={device.name!r}). Trigger DFU first."); return

    async with connect(device) as client:
        ctrl = client.services.get_characteristic(UUID_DFU_CONTROL_POINT)
        pkt = client.services.get_characteristic(UUID_DFU_PACKET)

        loop = asyncio.get_event_loop()

        def on(_, data: bytearray) -> None:
            if not data or data[0] != OP_RESPONSE:
                return
            op, res = data[1], data[2]
            fut = _shared_waiters.pop(op, None)
            if fut and not fut.done():
                if res == RES_SUCCESS:
                    fut.set_result(bytes(data[3:]))
                else:
                    fut.set_exception(RuntimeError(f"op 0x{op:02x} res=0x{res:02x}"))

        await client.start_notify(ctrl, on)

        async def cmd(op: int, body: bytes = b"", timeout: float = 10.0) -> bytes:
            f = loop.create_future()
            _shared_waiters[op] = f
            await client.write_gatt_char(ctrl, bytes([op]) + body, response=True)
            return await asyncio.wait_for(f, timeout=timeout)

        # One-time init
        await cmd(OP_SET_PRN, struct.pack("<H", 0))
        init = make_init_packet(app_size=4096)
        await cmd(OP_SELECT, bytes([OBJ_COMMAND]))
        await cmd(OP_CREATE, struct.pack("<BI", OBJ_COMMAND, len(init)))
        for i in range(0, len(init), 20):
            await client.write_gatt_char(pkt, init[i:i + 20], response=False)
        cs = await cmd(OP_CALC_CRC)
        if struct.unpack("<II", cs[:8])[0] != len(init):
            log("init incomplete"); return
        await cmd(OP_EXECUTE, timeout=30.0)
        log("init committed ✓")

        chunk = bytes((i % 256) for i in range(4096))

        # ---- TEST A: writeWithoutResponse + 20 B + PRN=6 ----
        log("")
        log("TEST A: writeWithoutResponse, 20 B/pkt, PRN=6")
        elapsed_a, ok_a = await stream_chunk(client, ctrl, pkt, cmd, chunk, payload=20, prn=6, with_response=False)
        log(f"  → {'OK' if ok_a else 'FAIL'}  {len(chunk)} B in {elapsed_a*1000:.0f} ms  ({len(chunk)/elapsed_a:.0f} B/s)")
        if ok_a:
            await cmd(OP_EXECUTE, timeout=30.0)
            log("  chunk A executed")

        # ---- TEST B: writeWithResponse + 244 B + PRN=6 ----
        log("")
        log("TEST B: writeWithResponse, 244 B/pkt, PRN=6")
        elapsed_b, ok_b = await stream_chunk(client, ctrl, pkt, cmd, chunk, payload=244, prn=6, with_response=True)
        log(f"  → {'OK' if ok_b else 'FAIL'}  {len(chunk)} B in {elapsed_b*1000:.0f} ms  ({len(chunk)/elapsed_b:.0f} B/s)")
        if ok_b:
            await cmd(OP_EXECUTE, timeout=30.0)
            log("  chunk B executed")

        # ---- TEST C: writeWithResponse + 244 B + PRN=0 (no flow control) ----
        log("")
        log("TEST C: writeWithResponse, 244 B/pkt, PRN=0 (no flow control)")
        elapsed_c, ok_c = await stream_chunk(client, ctrl, pkt, cmd, chunk, payload=244, prn=0, with_response=True)
        log(f"  → {'OK' if ok_c else 'FAIL'}  {len(chunk)} B in {elapsed_c*1000:.0f} ms  ({len(chunk)/elapsed_c:.0f} B/s)")
        if ok_c:
            await cmd(OP_EXECUTE, timeout=30.0)
            log("  chunk C executed")

        await client.stop_notify(ctrl)

        # Summary projection to 180 KB
        print()
        print("=== speed test summary ===")
        for label, elapsed, ok in [
            ("A (writeWithoutResponse, 20B, PRN=6)", elapsed_a, ok_a),
            ("B (writeWithResponse,   244B, PRN=6)", elapsed_b, ok_b),
            ("C (writeWithResponse,   244B, PRN=0)", elapsed_c, ok_c),
        ]:
            if not ok:
                print(f"  {label}: FAILED")
                continue
            rate = 4096 / elapsed
            est_180k = (180 * 1024) / rate
            print(f"  {label}: {4096/elapsed:.0f} B/s — 180 KB hex would take ~{est_180k:.0f}s")


if __name__ == "__main__":
    asyncio.run(main())
