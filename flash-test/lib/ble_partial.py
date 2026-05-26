"""CODAL partial flashing over BLE — Python+bleak port of
mini-connection-widget/src/ble-flash-web.ts (commit a54f89d).

Supports both MakeCode hexes (MAGIC_MARKER) and MicroPython hexes
(addlayouttable.py UPY_MAGIC1/MAGIC2 trailer). Writes the relevant
region in 64-byte blocks (4 packets of 20 bytes each) over the
partial-flashing characteristic.
"""

from __future__ import annotations

import asyncio
import struct
import sys
import time
import zlib
from pathlib import Path

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ble-test"))
from common import is_calliope_adv  # noqa: E402

PF_SERVICE = "e97dd91d-251d-470a-a062-fa1922dfa9a8"
PF_CHAR = "e97d3b10-251d-470a-a062-fa1922dfa9a8"

CMD_REGION_INFO = 0x00
CMD_FLASH_DATA = 0x01
CMD_END_OF_TX = 0x02
CMD_STATUS = 0xEE
CMD_RESET = 0xFF

REGION_DAL = 0x01
REGION_MAKECODE = 0x02

MODE_PAIRING = 0x00
MODE_APPLICATION = 0x01

ACK_OUT_OF_ORDER = 0xAA
ACK_WRITTEN = 0xFF

# Layout-table magic for MicroPython hexes (addlayouttable.py).
UPY_MAGIC1 = bytes([0xFE, 0x30, 0x7F, 0x59])
UPY_MAGIC2 = bytes([0x9D, 0xD7, 0xB1, 0xC1])
UPY_REGION_RUNTIME = 2
UPY_REGION_FS = 3
UPY_HASH_NONE = 0
UPY_HASH_DATA = 1
UPY_HASH_POINTER = 2

# MakeCode 16-byte magic (08e3b92c615a841c49866c975ee5197 inverted/etc).
MAKECODE_MAGIC = bytes([
    0x70, 0x8E, 0x3B, 0x92, 0xC6, 0x15, 0xA8, 0x41,
    0xC4, 0x98, 0x66, 0xC9, 0x75, 0xEE, 0x51, 0x97,
])


class DalMismatchError(Exception):
    pass


class PartialFlashUnsupportedError(Exception):
    pass


class InvalidHexError(Exception):
    pass


# ---- Intel HEX → memory map ----------------------------------------------

def hex_to_memory(hex_text: str) -> dict[int, int]:
    blocks: dict[int, int] = {}
    ext = 0
    for line in hex_text.splitlines():
        line = line.strip()
        if not line.startswith(":"):
            continue
        n = int(line[1:3], 16)
        addr = int(line[3:7], 16)
        rtype = int(line[7:9], 16)
        data = bytes.fromhex(line[9:9 + n * 2])
        if rtype == 0x04:
            ext = int.from_bytes(data, "big") << 16
        elif rtype == 0x00:
            full = ext + addr
            for i, b in enumerate(data):
                blocks[full + i] = b
    return blocks


def slice_pad(blocks: dict[int, int], start: int, length: int, fill: int = 0xFF) -> bytes:
    """Return `length` bytes starting at `start`, filling gaps with `fill`."""
    out = bytearray(length)
    for i in range(length):
        out[i] = blocks.get(start + i, fill)
    return bytes(out)


def crc32_for_pointer(bytes_at_ptr: bytes) -> int:
    """Polynomial 0xEDB88320, init 0xFFFFFFFF, final XOR — matches codal."""
    crc = 0xFFFFFFFF
    for b in bytes_at_ptr:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 & -(crc & 1) & 0xFFFFFFFF)
    return (~crc) & 0xFFFFFFFF


# ---- Hex parsing ---------------------------------------------------------

class ParsedHex:
    def __init__(self, bin_: bytes, base_addr: int, dal_hash: bytes, makecode_hash: bytes, flavor: str):
        self.bin = bin_
        self.base_addr = base_addr
        self.dal_hash = dal_hash
        self.makecode_hash = makecode_hash
        self.flavor = flavor


def _parse_makecode(blocks: dict[int, int]) -> ParsedHex | None:
    # Build contiguous segment view: simple approach — search blocks where
    # we have data. For MakeCode we expect the marker in the app region.
    # Scan in 16-byte windows from app region start.
    addrs = sorted(blocks)
    if not addrs:
        return None
    APP_START = 0x1C000
    # Find 16-byte-aligned windows starting at APP_START.
    cur = APP_START
    end = max(addrs) + 1
    while cur + 32 <= end:
        # Check if all 16 bytes of the marker are present.
        match = True
        for j in range(16):
            if blocks.get(cur + j) != MAKECODE_MAGIC[j]:
                match = False
                break
        if match:
            # Found marker. Extract dal hash + makecode hash from next 16 bytes.
            dal_hash = bytes(blocks.get(cur + 16 + j, 0xFF) for j in range(8))
            mc_hash = bytes(blocks.get(cur + 24 + j, 0xFF) for j in range(8))
            # `bin` = everything from marker forward.
            bin_end = end
            bin_ = bytes(blocks.get(cur + j, 0xFF) for j in range(bin_end - cur))
            return ParsedHex(bin_, cur, dal_hash, mc_hash, "makecode")
        cur += 16
    return None


def _parse_micropython(blocks: dict[int, int]) -> ParsedHex | None:
    """Search for the layout-table trailer with the 4 invariants the widget
    uses (page-aligned end, bounded sizes, table_len == num_reg * 16)."""
    addrs = sorted(blocks)
    if not addrs:
        return None
    # Walk 16-byte aligned addresses across the populated range. Cheap because
    # we only check 4 + 4 bytes per window.
    lo = (min(addrs) // 16) * 16
    hi = max(addrs) + 1
    for i in range(lo, hi, 16):
        # Need all 16 bytes present for a window check.
        if any((i + j) not in blocks for j in (0, 1, 2, 3, 12, 13, 14, 15)):
            continue
        if (blocks.get(i + 0), blocks.get(i + 1), blocks.get(i + 2), blocks.get(i + 3)) != tuple(UPY_MAGIC1):
            continue
        if (blocks.get(i + 12), blocks.get(i + 13), blocks.get(i + 14), blocks.get(i + 15)) != tuple(UPY_MAGIC2):
            continue
        # Invariant 1: trailer ends on 4-KB page boundary
        if ((i + 16) & 0xFFF) != 0:
            continue
        table_len = blocks.get(i + 6, 0) | (blocks.get(i + 7, 0) << 8)
        num_reg = blocks.get(i + 8, 0) | (blocks.get(i + 9, 0) << 8)
        if not (0 < table_len <= 256 and 0 < num_reg <= 16 and table_len == num_reg * 16):
            continue
        records_start = i - table_len
        if records_start < 0:
            continue
        # Parse records.
        records = []
        for r in range(records_start, i, 16):
            rid = blocks.get(r, 0)
            ht = blocks.get(r + 1, 0)
            reg_page = blocks.get(r + 2, 0) | (blocks.get(r + 3, 0) << 8)
            reg_len = (
                blocks.get(r + 4, 0)
                | (blocks.get(r + 5, 0) << 8)
                | (blocks.get(r + 6, 0) << 16)
                | (blocks.get(r + 7, 0) << 24)
            )
            start = reg_page * 4096
            hash_data = bytes(blocks.get(r + 8 + k, 0) for k in range(8))
            records.append({"id": rid, "ht": ht, "start": start, "end": start + reg_len, "hash": hash_data})

        runtime = next((rec for rec in records if rec["id"] == UPY_REGION_RUNTIME), None)
        fs = next((rec for rec in records if rec["id"] == UPY_REGION_FS), None)
        if runtime is None or fs is None:
            continue

        # Resolve runtime DAL hash (HASH_POINTER → CRC32 of version string).
        if runtime["ht"] == UPY_HASH_POINTER:
            ptr = (
                runtime["hash"][0]
                | (runtime["hash"][1] << 8)
                | (runtime["hash"][2] << 16)
                | (runtime["hash"][3] << 24)
            )
            # Walk up to 256 bytes or to null terminator.
            string = bytearray()
            for k in range(256):
                b = blocks.get(ptr + k, 0xFF)
                if b == 0:
                    break
                string.append(b)
            if not string:
                continue
            crc = crc32_for_pointer(bytes(string))
            runtime_device_hash = struct.pack("<II", crc, 0)
        elif runtime["ht"] in (UPY_HASH_DATA, UPY_HASH_NONE):
            runtime_device_hash = runtime["hash"]
        else:
            continue

        fs_bytes = slice_pad(blocks, fs["start"], fs["end"] - fs["start"], 0xFF)

        # Synthesise non-zero makeCodeHash so the widget shortcut never fires.
        synthetic = bytearray(8)
        struct.pack_into("<I", synthetic, 0, len(fs_bytes))
        for k in range(min(4, len(fs_bytes))):
            synthetic[4 + k] = fs_bytes[k]
        if all(b == 0 for b in synthetic):
            synthetic[0] = 0x01

        return ParsedHex(
            bin_=fs_bytes,
            base_addr=fs["start"],
            dal_hash=runtime_device_hash,
            makecode_hash=bytes(synthetic),
            flavor="micropython",
        )
    return None


def parse_hex_for_partial_flash(hex_text: str) -> ParsedHex:
    blocks = hex_to_memory(hex_text)
    p = _parse_makecode(blocks)
    if p is not None:
        return p
    p = _parse_micropython(blocks)
    if p is not None:
        return p
    raise InvalidHexError("hex has neither MakeCode marker nor MicroPython layout table")


# ---- Wire protocol -------------------------------------------------------

def build_flash_packets(block_addr: int, start_seq: int, block64: bytes) -> list[bytes]:
    """Mirror buildFlashPackets in ble-flash-web.ts."""
    def mk(b1: int, b2: int, seq: int, payload: bytes) -> bytes:
        out = bytearray(20)
        out[0] = CMD_FLASH_DATA
        out[1] = b1 & 0xFF
        out[2] = b2 & 0xFF
        out[3] = seq & 0xFF
        out[4:4 + len(payload)] = payload
        return bytes(out)
    return [
        mk((block_addr >> 8) & 0xFF, block_addr & 0xFF, start_seq, block64[0:16]),
        mk((block_addr >> 24) & 0xFF, (block_addr >> 16) & 0xFF, start_seq + 1, block64[16:32]),
        mk(0, 0, start_seq + 2, block64[32:48]),
        mk(0, 0, start_seq + 3, block64[48:64]),
    ]


class _Session:
    def __init__(self, client: BleakClient, log):
        self.client = client
        self.log = log
        self._loop = asyncio.get_event_loop()
        self._pending: asyncio.Future[bytes] | None = None

    def _on_notify(self, _, data: bytearray) -> None:
        p = self._pending
        if p and not p.done():
            self._pending = None
            p.set_result(bytes(data))

    async def open_char(self) -> None:
        char = self.client.services.get_characteristic(PF_CHAR)
        if char is None:
            raise PartialFlashUnsupportedError("partial flashing service not on this device")
        await self.client.start_notify(char, self._on_notify)

    async def write_no_notify(self, payload: bytes) -> None:
        await self.client.write_gatt_char(PF_CHAR, payload, response=False)

    async def request(self, payload: bytes, timeout: float = 5.0) -> bytes:
        f: asyncio.Future[bytes] = self._loop.create_future()
        self._pending = f
        await self.write_no_notify(payload)
        return await asyncio.wait_for(f, timeout=timeout)

    async def request_status(self) -> tuple[int, int]:
        data = await self.request(bytes([CMD_STATUS]))
        # [0]=CMD_STATUS echo, [1]=version, [2]=mode
        if len(data) >= 3 and data[0] == CMD_STATUS:
            return data[1], data[2]
        raise RuntimeError(f"unexpected STATUS response: {bytes(data).hex()}")

    async def request_region(self, region_id: int) -> dict:
        data = await self.request(bytes([CMD_REGION_INFO, region_id]))
        # [0]=0x00, [1]=region_id, [2..6]=start, [6..10]=end, [10..18]=hash
        if len(data) < 18 or data[0] != 0x00:
            raise RuntimeError(f"unexpected REGION_INFO response: {bytes(data).hex()}")
        start = struct.unpack(">I", data[2:6])[0]
        end = struct.unpack(">I", data[6:10])[0]
        return {"id": data[1], "start": start, "end": end, "hash": bytes(data[10:18])}

    async def wait_for_response(self, timeout: float = 5.0) -> bytes:
        f: asyncio.Future[bytes] = self._loop.create_future()
        self._pending = f
        return await asyncio.wait_for(f, timeout=timeout)


async def _scan_for_calliope(timeout: float = 8.0):
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


async def _try_switch_to_pairing(client: BleakClient, log) -> bool:
    """Issue Reset+Pairing via the partial-flash characteristic and wait for
    the device-side disconnect. Mirrors the widget's 3-attempt loop with
    longer waits, which empirically works against codal's event-bus
    handler (the write returns immediately but the actual reset is
    scheduled via the event bus and takes a few hundred ms).
    """
    for attempt in range(3):
        log(f"  reset-into-pairing write (attempt {attempt + 1})")
        try:
            await client.write_gatt_char(PF_CHAR, bytes([CMD_RESET, MODE_PAIRING]), response=False)
        except Exception as e:
            log(f"    write said: {e}")
        # Wait up to 10 s for the device to disconnect.
        for _ in range(50):
            if not client.is_connected:
                log("    device disconnected")
                return True
            await asyncio.sleep(0.2)
        log("    no disconnect within 10 s — retrying")
    return False


async def _run_flash_protocol(client: BleakClient, parsed: ParsedHex, result: dict, log) -> None:
    """Once the device is in PAIRING mode and we have a fresh GATT, run the
    DAL check + flash data loop. Caller guarantees mode == PAIRING."""
    s = _Session(client, log)
    await s.open_char()

    ver, mode = await s.request_status()
    log(f"  post-reconnect status: version={ver} mode={mode}")
    if mode != MODE_PAIRING:
        raise RuntimeError(f"expected pairing mode after reconnect, got mode={mode}")

    dal = await s.request_region(REGION_DAL)
    result["device_dal_hash"] = dal["hash"].hex()
    log(f"  device DAL: 0x{dal['start']:x}-0x{dal['end']:x} hash={dal['hash'].hex()}")
    if dal["hash"] != parsed.dal_hash:
        raise DalMismatchError(
            f"device DAL hash {dal['hash'].hex()} != hex DAL hash {parsed.dal_hash.hex()}"
        )

    mc = await s.request_region(REGION_MAKECODE)
    result["device_mc_hash"] = mc["hash"].hex()
    log(f"  device MC region: 0x{mc['start']:x}-0x{mc['end']:x} hash={mc['hash'].hex()}")

    if mc["hash"] == parsed.makecode_hash:
        log("  identical hash — resetting into application mode")
        await s.write_no_notify(bytes([CMD_RESET, MODE_APPLICATION]))
        return

    start_addr = mc["start"]
    result["stage"] = "flash-data"
    offset = 0
    packet_num = 0
    chunk_delay_ms = 0
    blocks = 0
    total = len(parsed.bin)
    while offset < total:
        block_addr = start_addr + offset
        block = bytearray(64)
        src = parsed.bin[offset:offset + 64]
        block[: len(src)] = src
        packets = build_flash_packets(block_addr, packet_num, bytes(block))
        f: asyncio.Future[bytes] = s._loop.create_future()
        s._pending = f
        for p in packets:
            if chunk_delay_ms > 0:
                await asyncio.sleep(chunk_delay_ms / 1000)
            await s.write_no_notify(p)
        try:
            ack = await asyncio.wait_for(f, timeout=5.0)
        except asyncio.TimeoutError:
            s._pending = None
            raise RuntimeError(f"flash-block ack timeout at offset {offset}")
        if not ack or ack[0] != CMD_FLASH_DATA:
            raise RuntimeError(f"expected FLASH_DATA ack, got {bytes(ack).hex()}")
        if ack[1] == ACK_OUT_OF_ORDER:
            chunk_delay_ms = min(chunk_delay_ms + 10, 75)
            packet_num = (packet_num + 4) & 0xFF
            continue
        if ack[1] != ACK_WRITTEN:
            raise RuntimeError(f"unexpected ack: {ack[1]:02x}")
        chunk_delay_ms = max(chunk_delay_ms - 1, 0)
        offset += 64
        packet_num = (packet_num + 4) & 0xFF
        blocks += 1
        if blocks % 50 == 0:
            log(f"  blocks={blocks} offset={offset}/{total} ({100*offset//total}%)")
    result["blocks_written"] = blocks
    await s.write_no_notify(bytes([CMD_END_OF_TX]))
    log(f"  EOT written ({blocks} blocks)")


async def partial_flash(hex_text: str, *, log=print) -> dict:
    """End-to-end partial flash. Returns result dict with timing + error."""
    started = time.monotonic()
    result = {
        "ok": False,
        "elapsed_s": 0.0,
        "stage": "parse",
        "error": None,
        "flavor": None,
        "bin_size": 0,
        "base_addr": 0,
        "device_dal_hash": None,
        "hex_dal_hash": None,
        "device_mc_hash": None,
        "hex_mc_hash": None,
        "blocks_written": 0,
    }
    try:
        parsed = parse_hex_for_partial_flash(hex_text)
        result["flavor"] = parsed.flavor
        result["bin_size"] = len(parsed.bin)
        result["base_addr"] = parsed.base_addr
        result["hex_dal_hash"] = parsed.dal_hash.hex()
        result["hex_mc_hash"] = parsed.makecode_hash.hex()
        log(f"  parsed: flavor={parsed.flavor} bin={len(parsed.bin)} B base=0x{parsed.base_addr:x}")
        log(f"  hex DAL hash={parsed.dal_hash.hex()}  MC hash={parsed.makecode_hash.hex()}")

        result["stage"] = "scan"
        pair = await _scan_for_calliope()
        if not pair:
            raise RuntimeError("no Calliope advertising — A+B+Reset?")
        device, adv = pair
        log(f"  found {device.address} ({device.name or adv.local_name}) rssi={adv.rssi}")

        # First connect — check mode. If app-mode, kick to pairing and reconnect.
        result["stage"] = "connect"
        client = BleakClient(device, timeout=20.0)
        await client.connect()
        try:
            s = _Session(client, log)
            await s.open_char()
            ver, mode = await s.request_status()
            log(f"  status: version={ver} mode={mode} ({'PAIRING' if mode == 0 else 'APPLICATION'})")
            if mode != MODE_PAIRING:
                result["stage"] = "switch-to-pairing"
                log("  device in application mode — sending Reset+Pairing")
                ok = await _try_switch_to_pairing(client, log)
                if not ok:
                    raise RuntimeError("device did not disconnect after 3× Reset+Pairing")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

        # Reconnect after pairing-mode boot.
        if result["stage"] == "switch-to-pairing":
            log("  waiting 4 s for pairing-mode boot…")
            await asyncio.sleep(4.0)
            log("  re-scanning for pairing-mode advertise…")
            pair2 = await _scan_for_calliope(timeout=10.0)
            if not pair2:
                raise RuntimeError("device did not re-advertise after pairing-mode reboot")
            device2, _ = pair2
            log(f"  reconnecting to {device2.address}…")
            client2 = BleakClient(device2, timeout=20.0)
            await client2.connect()
            try:
                await _run_flash_protocol(client2, parsed, result, log)
            finally:
                try:
                    await client2.disconnect()
                except Exception:
                    pass
        else:
            # Already in pairing mode — reuse the connection we just had.
            client3 = BleakClient(device, timeout=20.0)
            await client3.connect()
            try:
                await _run_flash_protocol(client3, parsed, result, log)
            finally:
                try:
                    await client3.disconnect()
                except Exception:
                    pass
        result["ok"] = True
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        result["elapsed_s"] = time.monotonic() - started
    return result


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: ble_partial.py <hex>")
        sys.exit(2)
    text = Path(sys.argv[1]).read_text()
    r = asyncio.run(partial_flash(text))
    print(f"\nResult: ok={r['ok']} elapsed={r['elapsed_s']:.1f}s stage={r['stage']} blocks={r['blocks_written']}")
    if r.get("flavor"):
        print(f"  flavor={r['flavor']} bin_size={r['bin_size']} base=0x{r['base_addr']:x}")
    if r["error"]:
        print(f"Error: {r['error']}")
