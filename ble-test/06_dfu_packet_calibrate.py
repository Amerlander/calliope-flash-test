"""06_dfu_packet_calibrate: write N packets at varying sizes, then CalcChecksum
to see EXACTLY how many bytes the bootloader received.

If Windows is silently fragmenting our 244-byte writes into 20-byte ATT
chunks, the bootloader will see a different offset than we expect.
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import zlib

from common import (
    UUID_DFU_CONTROL_POINT,
    UUID_DFU_PACKET,
    UUID_NORDIC_DFU,
    connect,
    find_calliope,
    log,
)

OP_CREATE = 0x01
OP_SET_PRN = 0x02
OP_CALC_CRC = 0x03
OP_SELECT = 0x06
OP_RESPONSE = 0x60
OBJ_DATA = 0x02
RES_SUCCESS = 0x01


async def run(packet_size: int, num_packets: int) -> None:
    pair = await find_calliope()
    if not pair:
        return
    device, _ = pair
    if "dfutarg" not in (device.name or "").lower():
        log(f"device is not DfuTarg (name={device.name!r}) — bootloader needed")
        return

    async with connect(device) as client:
        ctrl = client.services.get_characteristic(UUID_DFU_CONTROL_POINT)
        pkt = client.services.get_characteristic(UUID_DFU_PACKET)
        if not ctrl or not pkt:
            log("DFU chars missing"); return

        # Notification handling: single waiter per opcode.
        waiters: dict[int, asyncio.Future[bytes]] = {}
        loop = asyncio.get_event_loop()

        def on_notify(_, data: bytearray) -> None:
            if not data or data[0] != OP_RESPONSE:
                return
            op, res = data[1], data[2]
            fut = waiters.pop(op, None)
            if fut and not fut.done():
                if res != RES_SUCCESS:
                    fut.set_exception(RuntimeError(f"op 0x{op:02x} res=0x{res:02x}"))
                else:
                    fut.set_result(bytes(data[3:]))

        await client.start_notify(ctrl, on_notify)

        async def cmd(op: int, body: bytes = b"", timeout: float = 30.0) -> bytes:
            fut: asyncio.Future[bytes] = loop.create_future()
            waiters[op] = fut
            await client.write_gatt_char(ctrl, bytes([op]) + body, response=True)
            return await asyncio.wait_for(fut, timeout=timeout)

        try:
            log(f"→ SetPRN(0)  (disable auto-notify — we manually CalcChecksum)")
            await cmd(OP_SET_PRN, struct.pack("<H", 0))

            log(f"→ Select(Data)")
            p = await cmd(OP_SELECT, bytes([OBJ_DATA]))
            max_size, _, _ = struct.unpack("<III", p[:12])
            log(f"  device max_size={max_size}")

            chunk_size = packet_size * num_packets
            log(f"→ CreateObject(Data, size={chunk_size})  ({num_packets} packets × {packet_size}B)")
            await cmd(OP_CREATE, struct.pack("<BI", OBJ_DATA, chunk_size), timeout=30.0)

            # Generate deterministic bytes.
            data = bytes((i % 256) for i in range(chunk_size))
            expected_crc = zlib.crc32(data) & 0xFFFFFFFF

            stream_start = loop.time()
            for i in range(num_packets):
                slice_ = data[i * packet_size : (i + 1) * packet_size]
                t0 = loop.time()
                await client.write_gatt_char(pkt, slice_, response=False)
                elapsed_ms = (loop.time() - t0) * 1000
                log(f"  packet #{i+1}/{num_packets} ({len(slice_)}B) write took {elapsed_ms:.1f} ms")
            total_ms = (loop.time() - stream_start) * 1000
            log(f"  total stream: {chunk_size} B in {total_ms:.0f} ms = {chunk_size / total_ms:.1f} KB/s")

            log("→ CalcChecksum (probe what bootloader received)")
            p = await cmd(OP_CALC_CRC, timeout=15.0)
            dev_offset, dev_crc = struct.unpack("<II", p[:8])
            log(f"  device:   offset={dev_offset:5d}  crc=0x{dev_crc:08x}")
            log(f"  expected: offset={chunk_size:5d}  crc=0x{expected_crc:08x}")
            if dev_offset == chunk_size and dev_crc == expected_crc:
                log(f"  ✅ device received all {chunk_size} bytes EXACTLY at payload={packet_size}")
            elif dev_offset == 0:
                log(f"  ❌ device got NOTHING — packets were dropped at the BLE layer")
            else:
                log(f"  ⚠️  device offset diverges from expected — partial loss or fragmentation")
                log(f"      delta = {chunk_size - dev_offset} bytes missing")

        except asyncio.TimeoutError:
            log("  ❌ timeout — bootloader stopped responding")
        except Exception as e:
            log(f"  ❌ {type(e).__name__}: {e}")
        finally:
            try:
                await client.stop_notify(ctrl)
            except Exception:
                pass


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--packet", type=int, default=244)
    p.add_argument("--count", type=int, default=3)
    a = p.parse_args()
    log(f"calibration: {a.count} packet(s) of {a.packet}B = {a.count * a.packet} B chunk")
    await run(a.packet, a.count)


if __name__ == "__main__":
    asyncio.run(main())
