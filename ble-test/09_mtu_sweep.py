"""09_mtu_sweep: single DFU session, send an init packet first, then probe
many single-packet write sizes in sequence to find the actual on-wire MTU.

Procedure:
  1. Connect to DfuTarg
  2. SetPRN(0), Select+Create+Stream+Execute Init Packet (NO_VALIDATION)
  3. For each candidate size:
     - Select(Data), CreateObject(Data, 4096)
     - Send ONE Packet write at candidate size
     - CalcChecksum — read bootloader's reported offset
     - If offset matches → that size LANDS; record + continue
     - If offset=0 or mismatch → that size is DROPPED; continue
  4. Disconnect

This stays in a single DFU session so init packet validation isn't lost.
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import zlib

from bleak.exc import BleakError

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
    magic = b"microbit_app"
    return magic + struct.pack("<III", 1, app_size, 0) + b"\x00" * 32


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--sizes", default="20,30,50,100,150,182,200,220,244",
                   help="comma-separated single-packet sizes to probe")
    args = p.parse_args()
    sizes = [int(s.strip()) for s in args.sizes.split(",") if s.strip()]
    log(f"MTU sweep — sizes: {sizes}")

    pair = await find_calliope()
    if not pair:
        log("no device"); return
    device, _ = pair
    if "dfutarg" not in (device.name or "").lower():
        log(f"device not in DfuTarg (name={device.name!r}). Trigger DFU first."); return

    async with connect(device) as client:
        ctrl = client.services.get_characteristic(UUID_DFU_CONTROL_POINT)
        pkt = client.services.get_characteristic(UUID_DFU_PACKET)
        mwwr = getattr(pkt, "max_write_without_response_size", None)
        log(f"  packet char mwwr (OS-reported): {mwwr}")

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

        async def cmd(op: int, body: bytes = b"", timeout: float = 10.0) -> bytes:
            f: asyncio.Future[bytes] = loop.create_future()
            waiters[op] = f
            await client.write_gatt_char(ctrl, bytes([op]) + body, response=True)
            return await asyncio.wait_for(f, timeout=timeout)

        # ---- One-time init packet ----
        log("  sending init packet (NO_VALIDATION)…")
        await cmd(OP_SET_PRN, struct.pack("<H", 0))
        init = make_init_packet(app_size=4096)
        await cmd(OP_SELECT, bytes([OBJ_COMMAND]))
        await cmd(OP_CREATE, struct.pack("<BI", OBJ_COMMAND, len(init)))
        # send init at safe size 20 since this works always
        for i in range(0, len(init), 20):
            await client.write_gatt_char(pkt, init[i:i + 20], response=False)
        cs = await cmd(OP_CALC_CRC)
        ip_off, _ = struct.unpack("<II", cs[:8])
        if ip_off != len(init):
            log(f"  init packet incomplete (dev offset={ip_off}, want={len(init)}) — aborting")
            return
        await cmd(OP_EXECUTE, timeout=30.0)
        log("  init committed ✓")

        # ---- Per-size single-packet probe loop ----
        print()
        print(f"  {'size':>4} | {'dev offset':>10} | {'crc':>10} | result")
        print(f"  {'-' * 4} | {'-' * 10} | {'-' * 10} | {'-' * 50}")

        results = []
        for size in sizes:
            try:
                await cmd(OP_SELECT, bytes([OBJ_DATA]))
                await cmd(OP_CREATE, struct.pack("<BI", OBJ_DATA, 4096), timeout=10.0)
            except Exception as e:
                print(f"  {size:>4} | {'—':>10} | {'—':>10} | Create(Data) failed: {e}")
                results.append((size, None, str(e)))
                continue

            payload = bytes((i % 256) for i in range(size))
            write_err = None
            try:
                await client.write_gatt_char(pkt, payload, response=False)
            except Exception as e:
                write_err = f"write threw: {e}"

            try:
                p = await cmd(OP_CALC_CRC, timeout=8.0)
            except asyncio.TimeoutError:
                print(f"  {size:>4} | {'—':>10} | {'—':>10} | CalcChecksum TIMEOUT" + (f" (write said {write_err})" if write_err else ""))
                results.append((size, None, f"CalcChecksum timeout{' / ' + write_err if write_err else ''}"))
                # Try to recover by disconnecting + reconnecting? Or just continue
                # — bootloader's state is "data object created, partial data" but
                # next CreateObject(Data) resets per-object state.
                continue
            except Exception as e:
                print(f"  {size:>4} | {'—':>10} | {'—':>10} | CalcChecksum {type(e).__name__}: {e}")
                results.append((size, None, str(e)))
                continue

            dev_off, dev_crc = struct.unpack("<II", p[:8])
            expected_crc = zlib.crc32(payload) & 0xFFFFFFFF
            if dev_off == size and dev_crc == expected_crc:
                tag = f"✓ landed ({size}B)"
            elif dev_off == 0:
                tag = "DROPPED (offset stayed 0)"
            elif dev_off < size:
                tag = f"truncated ({dev_off}/{size})"
            else:
                tag = f"odd: dev_off > sent ({dev_off} vs sent {size})"
            print(f"  {size:>4} | {dev_off:>10} | {dev_crc:>10} | {tag}")
            results.append((size, dev_off, tag))

        await client.stop_notify(ctrl)

        # Summary
        print()
        print("=== summary ===")
        landed = [r for r in results if r[1] == r[0]]
        if landed:
            max_landed = max(s for s, _, _ in landed)
            print(f"  max packet size the bootloader actually receives: {max_landed} bytes")
            print(f"  → that implies ATT MTU = {max_landed + 3} on this connection")
        else:
            print("  no packet size landed cleanly — all were dropped or errored")


if __name__ == "__main__":
    asyncio.run(main())
