"""04_dfu_flow: simulate the secure-DFU data transfer that the widget tries.

This is the same protocol as ble-dfu-web.ts:
  - Subscribe to Control Point notifications
  - SetPRN(0)
  - SelectObject(Command) → empty, just probe
  - CreateObject(Data, chunkSize) where chunkSize=4096 (matches widget)
  - Stream chunk bytes to Packet characteristic in N-byte writes
  - On every PRN_INTERVAL (6) packets, expect a PRN notification with offset/CRC
  - After chunkSize bytes, CalcChecksum to verify

We DON'T Execute the chunk — we want to probe the wire mechanics without
actually flashing anything. Aborting mid-chunk just drops the object on
the bootloader side; next CreateObject resets state.

Vary the per-packet payload size to see where Chrome's behavior diverges
from native bleak. Default: 244.
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import zlib
from typing import Awaitable, Callable

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic

from common import (
    UUID_DFU_CONTROL_POINT,
    UUID_DFU_PACKET,
    UUID_NORDIC_DFU,
    connect,
    find_calliope,
    log,
)


# Op codes per Nordic Secure DFU (nrf_dfu_op_t)
OP_CREATE = 0x01
OP_SET_PRN = 0x02
OP_CALC_CRC = 0x03
OP_EXECUTE = 0x04
OP_SELECT = 0x06
OP_RESPONSE = 0x60

OBJ_COMMAND = 0x01
OBJ_DATA = 0x02

RES_SUCCESS = 0x01


class DfuChannel:
    def __init__(self, client: BleakClient, ctrl: BleakGATTCharacteristic, pkt: BleakGATTCharacteristic):
        self.client = client
        self.ctrl = ctrl
        self.pkt = pkt
        self._waiters: dict[int, asyncio.Future[bytes]] = {}
        self._loop = asyncio.get_event_loop()

    async def start(self) -> None:
        await self.client.start_notify(self.ctrl, self._on_notify)
        log(f"  notifications subscribed on Control Point")

    async def stop(self) -> None:
        try:
            await self.client.stop_notify(self.ctrl)
        except Exception:
            pass

    def _on_notify(self, _sender: BleakGATTCharacteristic, data: bytearray) -> None:
        if not data or data[0] != OP_RESPONSE:
            log(f"  unexpected notify: {data.hex()}")
            return
        op = data[1]
        res = data[2]
        payload = bytes(data[3:])
        log(f"  ← notify op=0x{op:02x} res=0x{res:02x} payload={payload.hex()}")
        fut = self._waiters.pop(op, None)
        if fut is None:
            log(f"    (no waiter for op 0x{op:02x})")
            return
        if res != RES_SUCCESS:
            fut.set_exception(RuntimeError(f"DFU op 0x{op:02x} failed: result=0x{res:02x}"))
        else:
            fut.set_result(payload)

    async def cmd(self, op: int, body: bytes = b"", timeout: float = 5.0) -> bytes:
        if op in self._waiters:
            raise RuntimeError(f"already awaiting op 0x{op:02x}")
        fut: asyncio.Future[bytes] = self._loop.create_future()
        self._waiters[op] = fut
        try:
            await self.client.write_gatt_char(self.ctrl, bytes([op]) + body, response=True)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._waiters.pop(op, None)

    async def select(self, obj_type: int) -> tuple[int, int, int]:
        p = await self.cmd(OP_SELECT, bytes([obj_type]))
        max_size, offset, crc = struct.unpack("<III", p[:12])
        return max_size, offset, crc

    async def set_prn(self, n: int) -> None:
        await self.cmd(OP_SET_PRN, struct.pack("<H", n))

    async def create(self, obj_type: int, size: int) -> None:
        await self.cmd(OP_CREATE, struct.pack("<BI", obj_type, size), timeout=10.0)

    async def calc_crc(self) -> tuple[int, int]:
        p = await self.cmd(OP_CALC_CRC)
        offset, crc = struct.unpack("<II", p[:8])
        return offset, crc

    async def write_packet(self, data: bytes) -> None:
        await self.client.write_gatt_char(self.pkt, data, response=False)


async def probe_chunk_payload(payload_size: int) -> None:
    pair = await find_calliope()
    if not pair:
        return
    device, _ = pair
    name = (device.name or "").lower()
    if "dfutarg" not in name:
        log(f"device is not DfuTarg (name={device.name!r}) — needs to be in bootloader for this test")
        return

    async with connect(device) as client:
        svc = client.services.get_service(UUID_NORDIC_DFU)
        if svc is None:
            log("Nordic DFU service not present — bailing")
            return
        ctrl = client.services.get_characteristic(UUID_DFU_CONTROL_POINT)
        pkt = client.services.get_characteristic(UUID_DFU_PACKET)
        if not ctrl or not pkt:
            log("DFU char(s) missing")
            return

        ch = DfuChannel(client, ctrl, pkt)
        await ch.start()

        try:
            log("→ SetPRN(0)  (we'll check CRC manually at end of chunk)")
            await ch.set_prn(0)

            log("→ Select(Data)")
            max_size, off, crc = await ch.select(OBJ_DATA)
            log(f"  Select(Data): max_size={max_size}, current offset={off}, current crc=0x{crc:08x}")

            # Use a chunk size that's the bootloader's max (likely 4096) for
            # realistic test conditions matching the widget.
            chunk_size = min(max_size, 4096)
            log(f"→ CreateObject(Data, size={chunk_size})")
            await ch.create(OBJ_DATA, chunk_size)

            # Generate deterministic dummy data and write at the requested
            # per-packet size.
            data = bytes((i % 256) for i in range(chunk_size))
            offset = 0
            packet_no = 0
            log(f"→ Streaming chunk in payload={payload_size}-byte packets…")
            while offset < len(data):
                end = min(offset + payload_size, len(data))
                slice_ = data[offset:end]
                packet_no += 1
                try:
                    await ch.write_packet(slice_)
                except Exception as e:
                    log(f"  packet #{packet_no} (offset={offset}, size={len(slice_)}) FAILED: {type(e).__name__}: {e}")
                    raise
                if packet_no % 5 == 0 or end == len(data):
                    log(f"  packet #{packet_no} OK, total written={end}/{len(data)}")
                offset = end

            log("→ CalcChecksum  (verify what the bootloader received)")
            dev_offset, dev_crc = await ch.calc_crc()
            expected_crc = zlib.crc32(data) & 0xFFFFFFFF
            log(f"  device: offset={dev_offset}, crc=0x{dev_crc:08x}")
            log(f"  local:  offset={len(data)}, crc=0x{expected_crc:08x}")
            if dev_offset == len(data) and dev_crc == expected_crc:
                log(f"  ✅ MATCH — payload={payload_size} WORKS end-to-end")
            else:
                log(f"  ❌ MISMATCH at payload={payload_size}")

            # Don't Execute — we don't want to actually write garbage to flash.
            # The bootloader's CreateObject(Data) on the next attempt resets
            # this chunk's state.

        except asyncio.TimeoutError:
            log("  ❌ timeout — bootloader stopped responding")
        except Exception as e:
            log(f"  ❌ {type(e).__name__}: {e}")
        finally:
            await ch.stop()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", type=int, default=244)
    args = parser.parse_args()
    log(f"probing DFU data transfer with payload={args.payload}…")
    await probe_chunk_payload(args.payload)


if __name__ == "__main__":
    asyncio.run(main())
