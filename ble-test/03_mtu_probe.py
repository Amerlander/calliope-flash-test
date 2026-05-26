"""03_mtu_probe: find the max writable payload on the bootloader Packet
characteristic via incremental sizes.

We don't actually want to commit any of these writes — the bootloader
expects a Create Data Object before Packet writes. So we use
writeWithoutResponse on the Control Point characteristic instead, which
takes opcode-prefixed commands the bootloader rejects with a clean error
if invalid. The point is just to see at what byte count the *write
itself* fails — that tells us the ATT MTU.

If the device is currently in app mode (no bootloader running), we fall
back to probing the partial-flashing characteristic — same purpose,
same ATT MTU.
"""

from __future__ import annotations

import asyncio

from bleak.exc import BleakError

from common import (
    UUID_DFU_CONTROL_POINT,
    UUID_NORDIC_DFU,
    UUID_PARTIAL_FLASH,
    connect,
    find_calliope,
    log,
)


PROBE_SIZES = [20, 50, 100, 150, 182, 200, 220, 244, 300, 400, 500]


async def probe(client, target_char_uuid: str) -> None:
    char = client.services.get_characteristic(target_char_uuid)
    if char is None:
        log(f"  {target_char_uuid} not present on device — skipping")
        return
    mwwr = getattr(char, "max_write_without_response_size", None)
    log(f"  target char: {target_char_uuid}")
    log(f"  max_write_without_response_size reported by stack: {mwwr}")
    for size in PROBE_SIZES:
        if mwwr is not None and size > mwwr:
            log(f"  size={size:4d}  SKIP (> stack max {mwwr})")
            continue
        # Construct a payload that LOOKS like a valid op byte to avoid
        # tripping any bootloader-side malformed-packet handling: opcode
        # 0xEE = "no-op probe" on the control point (Nordic DFU has no
        # explicit no-op; we pick an unallocated opcode and accept the
        # bootloader's "Op code not supported" response — what we care
        # about is whether the WRITE got through, not the bootloader's
        # reply).
        payload = bytes([0xEE]) + bytes(size - 1)
        try:
            await client.write_gatt_char(char, payload, response=False)
            log(f"  size={size:4d}  WROTE OK")
        except BleakError as e:
            log(f"  size={size:4d}  WRITE FAILED: {e}")
            break
        except Exception as e:
            log(f"  size={size:4d}  unexpected: {type(e).__name__}: {e}")
            break


async def main() -> None:
    pair = await find_calliope()
    if not pair:
        return
    device, adv = pair
    name = device.name or adv.local_name or ""
    log(f"target: {device.address}  name={name!r}")

    async with connect(device) as client:
        # Prefer the bootloader's control point if we're in DfuTarg, else
        # use the partial-flashing characteristic in app mode.
        if "dfutarg" in name.lower() or client.services.get_service(UUID_NORDIC_DFU):
            log("device looks like bootloader / has Nordic DFU service")
            await probe(client, UUID_DFU_CONTROL_POINT)
        else:
            log("device looks like app — probing partial-flash characteristic")
            # Partial-flash service has its own characteristic; UUID derivation
            # mirrors src/ble-state.ts. The actual write char is the same as
            # service base + 0x3b10.
            pf_char_uuid = "e97d3b10-251d-470a-a062-fa1922dfa9a8"
            await probe(client, pf_char_uuid)


if __name__ == "__main__":
    asyncio.run(main())
