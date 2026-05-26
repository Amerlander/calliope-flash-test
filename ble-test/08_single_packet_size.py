"""08_single_packet_size: single-packet write at various sizes, then ask the
bootloader what offset it sees. If size N writes return OK but the bootloader's
offset stays at 0, the ATT layer is silently dropping the packet (negotiated
MTU on the bootloader side is < N+3).

Procedure for each candidate size:
  1. Connect to DfuTarg
  2. SetPRN(0), Select(Data), CreateObject(Data, page-aligned size)
  3. Send ONE Packet write at the candidate size
  4. CalcChecksum — read bootloader's reported offset
  5. Disconnect
  6. Move to next size

Doesn't actually Execute. The CreateObject on the next attempt resets state.
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import zlib

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
OP_SELECT = 0x06
OP_RESPONSE = 0x60
OBJ_DATA = 0x02
RES_SUCCESS = 0x01

# Try a wide range. 244 is the max we'd see at MTU 247.
SIZES_TO_TRY = [20, 50, 100, 200, 244]


async def probe_one_size(size: int) -> tuple[str, int | None, str | None]:
    """Returns (size, device_offset, error). Device offset is the byte count
    the bootloader reports after we sent a single Packet write of `size` bytes."""
    pair = await find_calliope(timeout=4.0)
    if not pair:
        return (f"{size}", None, "device not found")
    device, _ = pair
    if "dfutarg" not in (device.name or "").lower():
        return (f"{size}", None, "device not in DfuTarg")

    async with connect(device) as client:
        ctrl = client.services.get_characteristic(UUID_DFU_CONTROL_POINT)
        pkt = client.services.get_characteristic(UUID_DFU_PACKET)
        if not ctrl or not pkt:
            return (f"{size}", None, "DFU chars missing")

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

        try:
            await cmd(OP_SET_PRN, struct.pack("<H", 0))
            await cmd(OP_SELECT, bytes([OBJ_DATA]))
            # Create a 4096-byte data object (page-aligned), then send only ONE
            # packet of `size`. The bootloader resets per-object state on
            # CreateObject so previous tests don't leak.
            await cmd(OP_CREATE, struct.pack("<BI", OBJ_DATA, 4096), timeout=10.0)

            # Send one packet of `size` deterministic bytes.
            payload = bytes((i % 256) for i in range(size))
            try:
                await client.write_gatt_char(pkt, payload, response=False)
            except Exception as e:
                return (f"{size}", None, f"writeWithoutResponse threw: {e}")

            # CalcChecksum — see what the bootloader received.
            try:
                p = await cmd(OP_CALC_CRC, timeout=10.0)
            except asyncio.TimeoutError:
                return (f"{size}", None, "CalcChecksum timed out (bootloader silent)")

            dev_off, dev_crc = struct.unpack("<II", p[:8])
            expected_crc = zlib.crc32(payload) & 0xFFFFFFFF
            if dev_off == size and dev_crc == expected_crc:
                return (f"{size}", dev_off, None)
            else:
                hint = ""
                if dev_off == 0:
                    hint = " (packet dropped)"
                elif dev_off < size:
                    hint = " (truncated)"
                elif dev_off > size:
                    hint = " (??? more than sent)"
                return (f"{size}", dev_off, f"CRC=0x{dev_crc:08x} expected=0x{expected_crc:08x}{hint}")

        except Exception as e:
            return (f"{size}", None, f"{type(e).__name__}: {e}")
        finally:
            try:
                await client.stop_notify(ctrl)
            except Exception:
                pass


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--sizes", default="20,50,100,200,244",
                   help="comma-separated payload sizes to try (single packet each)")
    args = p.parse_args()
    sizes = [int(s.strip()) for s in args.sizes.split(",") if s.strip()]

    log(f"single-packet probe — sizes: {sizes}")
    log("(device must be in DfuTarg state; reset between groups if needed)")
    print()

    results = []
    for sz in sizes:
        log(f"--- probing size={sz} ---")
        size_label, dev_off, err = await probe_one_size(sz)
        results.append((sz, dev_off, err))
        if err:
            log(f"  size={sz}: FAIL — {err}")
        else:
            log(f"  size={sz}: OK — bootloader received {dev_off}/{sz} bytes")
        # Brief pause to let the bootloader settle between connections.
        await asyncio.sleep(1.5)

    print()
    print("=== single-packet size results ===")
    print(f"  {'size':>6} | {'dev offset':>10} | result")
    print(f"  {'-' * 6} | {'-' * 10} | {'-' * 40}")
    for sz, off, err in results:
        off_str = "—" if off is None else str(off)
        status = err if err else f"OK ({off} bytes received)"
        print(f"  {sz:>6} | {off_str:>10} | {status}")


if __name__ == "__main__":
    asyncio.run(main())
