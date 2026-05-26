"""10_writeresp_probe: try write-WITH-response (ATT_WRITE_REQ + prep_write)
on the Packet characteristic at large sizes.

Background: writeValueWithoutResponse maps to ATT_WRITE_CMD with hard
MTU-3 size limit. writeValue (with response) maps to ATT_WRITE_REQ for
small payloads, or to ATT_PREPARE_WRITE_REQ + ATT_EXECUTE_WRITE_REQ
sequence for "long writes" (>MTU-3 bytes). If the bootloader's ATT
layer supports prepared writes, write-with-response could let us send
>20 bytes per logical packet even at MTU=23.

iOS Nordic DFU probably uses this trick. Worth checking.
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
    return b"microbit_app" + struct.pack("<III", 1, app_size, 0) + b"\x00" * 32


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--sizes", default="20,50,100,200,244,300,500",
                   help="comma-separated single-packet sizes (writeWithResponse)")
    args = p.parse_args()
    sizes = [int(s.strip()) for s in args.sizes.split(",") if s.strip()]

    pair = await find_calliope()
    if not pair:
        log("no device"); return
    device, _ = pair
    if "dfutarg" not in (device.name or "").lower():
        log(f"device not in DfuTarg (name={device.name!r}). Trigger DFU first."); return

    async with connect(device) as client:
        ctrl = client.services.get_characteristic(UUID_DFU_CONTROL_POINT)
        pkt = client.services.get_characteristic(UUID_DFU_PACKET)
        log(f"  Packet char properties: {list(pkt.properties)}")
        log(f"  Packet char mwwr: {getattr(pkt, 'max_write_without_response_size', None)}")

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

        # Init packet
        await cmd(OP_SET_PRN, struct.pack("<H", 0))
        init = make_init_packet(app_size=4096)
        await cmd(OP_SELECT, bytes([OBJ_COMMAND]))
        await cmd(OP_CREATE, struct.pack("<BI", OBJ_COMMAND, len(init)))
        for i in range(0, len(init), 20):
            await client.write_gatt_char(pkt, init[i:i + 20], response=False)
        cs = await cmd(OP_CALC_CRC)
        ip_off, _ = struct.unpack("<II", cs[:8])
        if ip_off != len(init):
            log(f"  init packet incomplete (dev offset={ip_off})"); return
        await cmd(OP_EXECUTE, timeout=30.0)
        log("  init committed ✓")
        print()
        print(f"  {'size':>4} | {'mode':>10} | {'dev offset':>10} | result")
        print(f"  {'-' * 4} | {'-' * 10} | {'-' * 10} | {'-' * 50}")

        for size in sizes:
            await cmd(OP_SELECT, bytes([OBJ_DATA]))
            await cmd(OP_CREATE, struct.pack("<BI", OBJ_DATA, 4096), timeout=10.0)
            payload = bytes((i % 256) for i in range(size))
            try:
                # The key test: writeWithResponse on a write-without-response
                # characteristic. Many BLE stacks reject this; some accept and
                # use ATT_WRITE_REQ / prep_write internally.
                await client.write_gatt_char(pkt, payload, response=True)
                write_status = "OK"
            except Exception as e:
                write_status = f"FAILED: {type(e).__name__}: {e}"

            try:
                p = await cmd(OP_CALC_CRC, timeout=8.0)
            except (asyncio.TimeoutError, BleakError) as e:
                print(f"  {size:>4} | {'w/resp':>10} | {'—':>10} | CalcChecksum {type(e).__name__} (write: {write_status})")
                continue

            dev_off, dev_crc = struct.unpack("<II", p[:8])
            expected_crc = zlib.crc32(payload) & 0xFFFFFFFF
            if dev_off == size and dev_crc == expected_crc:
                tag = f"✓ landed {size}B"
            else:
                tag = f"dev_off={dev_off}, expected={size}"
            print(f"  {size:>4} | {'w/resp':>10} | {dev_off:>10} | {tag} (write API: {write_status})")

        await client.stop_notify(ctrl)


if __name__ == "__main__":
    asyncio.run(main())
