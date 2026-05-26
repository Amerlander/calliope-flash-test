"""05_dfu_prn: full DFU chunk transfer with PRN throttling enabled.

Uses PRN=6 (same as the widget) so the bootloader sends a checksum
notification every 6 packets. This:
  1. Confirms each batch arrived intact (offset+crc match)
  2. Keeps the link "active" from the bootloader's POV — no long silences
  3. Mirrors what iOS Nordic DFU does and what the widget does

Try various payload sizes and report at what point things break.
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import zlib

from bleak.backends.characteristic import BleakGATTCharacteristic

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
OP_EXECUTE = 0x04
OP_SELECT = 0x06
OP_RESPONSE = 0x60

OBJ_DATA = 0x02

RES_SUCCESS = 0x01


class DfuChannel:
    def __init__(self, client, ctrl, pkt):
        self.client = client
        self.ctrl = ctrl
        self.pkt = pkt
        self._waiters: dict[int, asyncio.Future[bytes]] = {}
        self._loop = asyncio.get_event_loop()
        self._all_notifications: list[tuple[int, int, bytes]] = []

    async def start(self):
        await self.client.start_notify(self.ctrl, self._on_notify)

    async def stop(self):
        try:
            await self.client.stop_notify(self.ctrl)
        except Exception:
            pass

    def _on_notify(self, _sender: BleakGATTCharacteristic, data: bytearray) -> None:
        if not data:
            return
        if data[0] != OP_RESPONSE:
            log(f"  [notify, non-response] {data.hex()}")
            return
        op, res = data[1], data[2]
        payload = bytes(data[3:])
        self._all_notifications.append((op, res, payload))
        fut = self._waiters.pop(op, None)
        if fut and not fut.done():
            if res != RES_SUCCESS:
                fut.set_exception(RuntimeError(f"op 0x{op:02x} failed: result=0x{res:02x}"))
            else:
                fut.set_result(payload)

    async def cmd(self, op: int, body: bytes = b"", timeout: float = 30.0) -> bytes:
        fut: asyncio.Future[bytes] = self._loop.create_future()
        self._waiters[op] = fut
        await self.client.write_gatt_char(self.ctrl, bytes([op]) + body, response=True)
        return await asyncio.wait_for(fut, timeout=timeout)


async def run(payload_size: int, prn: int, chunk_size: int, do_execute: bool) -> None:
    pair = await find_calliope()
    if not pair:
        return
    device, _ = pair
    if "dfutarg" not in (device.name or "").lower():
        log(f"device is not DfuTarg (name={device.name!r}) — needs to be in bootloader")
        return

    async with connect(device) as client:
        if not client.services.get_service(UUID_NORDIC_DFU):
            log("Nordic DFU service not present"); return
        ctrl = client.services.get_characteristic(UUID_DFU_CONTROL_POINT)
        pkt = client.services.get_characteristic(UUID_DFU_PACKET)
        if not ctrl or not pkt:
            log("DFU char(s) missing"); return

        ch = DfuChannel(client, ctrl, pkt)
        await ch.start()
        log(f"  notifications subscribed")

        try:
            log(f"→ SetPRN({prn})")
            await ch.cmd(OP_SET_PRN, struct.pack("<H", prn))

            log("→ Select(Data)")
            p = await ch.cmd(OP_SELECT, bytes([OBJ_DATA]))
            max_size, cur_off, cur_crc = struct.unpack("<III", p[:12])
            log(f"  device max_size={max_size}, cur_offset={cur_off}, cur_crc=0x{cur_crc:08x}")

            log(f"→ CreateObject(Data, size={chunk_size})")
            await ch.cmd(OP_CREATE, struct.pack("<BI", OBJ_DATA, chunk_size), timeout=30.0)

            # Stream chunk_size bytes at payload_size per write.
            data = bytes((i % 256) for i in range(chunk_size))
            offset = 0
            packet_no = 0
            packets_since_prn = 0
            stream_start = asyncio.get_event_loop().time()

            while offset < len(data):
                end = min(offset + payload_size, len(data))
                slice_ = data[offset:end]
                packet_no += 1
                packets_since_prn += 1

                # Set up the PRN waiter BEFORE writing the N-th packet, so the
                # bootloader's auto-notification finds a slot to land in.
                prn_pending: asyncio.Future[bytes] | None = None
                if prn > 0 and packets_since_prn == prn:
                    prn_pending = ch._loop.create_future()
                    ch._waiters[OP_CALC_CRC] = prn_pending

                try:
                    await client.write_gatt_char(pkt, slice_, response=False)
                except Exception as e:
                    log(f"  packet #{packet_no} (offset={offset}, size={len(slice_)}) FAILED: {e}")
                    raise

                offset = end

                if prn_pending is not None:
                    try:
                        p = await asyncio.wait_for(prn_pending, timeout=15.0)
                        dev_offset, dev_crc = struct.unpack("<II", p[:8])
                        expected_crc = zlib.crc32(data[:offset]) & 0xFFFFFFFF
                        ok = "✅" if (dev_offset == offset and dev_crc == expected_crc) else "❌"
                        log(f"  PRN @ pkt#{packet_no} ({offset}/{len(data)} B): device offset={dev_offset} crc=0x{dev_crc:08x} {ok}")
                        packets_since_prn = 0
                    except asyncio.TimeoutError:
                        log(f"  ❌ PRN timeout at packet #{packet_no} (offset={offset})")
                        ch._waiters.pop(OP_CALC_CRC, None)
                        raise

            elapsed_ms = (asyncio.get_event_loop().time() - stream_start) * 1000
            log(f"  stream done: {len(data)} B in {packet_no} packets, {elapsed_ms:.0f} ms")

            log("→ CalcChecksum (final)")
            p = await ch.cmd(OP_CALC_CRC, timeout=30.0)
            dev_offset, dev_crc = struct.unpack("<II", p[:8])
            expected_crc = zlib.crc32(data) & 0xFFFFFFFF
            ok = "✅ MATCH" if (dev_offset == len(data) and dev_crc == expected_crc) else "❌ MISMATCH"
            log(f"  device: offset={dev_offset}, crc=0x{dev_crc:08x}")
            log(f"  local:  offset={len(data)}, crc=0x{expected_crc:08x}")
            log(f"  {ok}")

            if do_execute:
                log("→ Execute (commit chunk to flash)")
                await ch.cmd(OP_EXECUTE, timeout=30.0)
                log("  executed ✅")
            else:
                log("(skipping Execute — probe only)")

        except asyncio.TimeoutError:
            log("  ❌ timeout — bootloader stopped responding")
        except Exception as e:
            log(f"  ❌ {type(e).__name__}: {e}")
        finally:
            log(f"  all notifications received this session: {len(ch._all_notifications)}")
            await ch.stop()


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--payload", type=int, default=244, help="bytes per Packet write")
    p.add_argument("--prn", type=int, default=6, help="Packet Receipt Notification interval (0 disables)")
    p.add_argument("--chunk", type=int, default=4096, help="chunk size in bytes (Create Data Object size)")
    p.add_argument("--execute", action="store_true", help="commit the chunk to flash (DESTRUCTIVE)")
    args = p.parse_args()
    log(f"DFU test: payload={args.payload}, PRN={args.prn}, chunk={args.chunk}, execute={args.execute}")
    await run(args.payload, args.prn, args.chunk, args.execute)


if __name__ == "__main__":
    asyncio.run(main())
