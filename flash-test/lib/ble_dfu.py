"""Full Nordic Secure DFU over Web Bluetooth — Python+bleak port.

Mirrors `mini-connection-widget/src/ble-dfu-web.ts` (commit a54f89d), tuned
for the Calliope mini v3 bootloader observed in this session:

  * ATT MTU 23 in bootloader → 20-byte writes to the Packet characteristic.
  * PRN=6 (Nordic SDK 17 NRF_DFU_BLE_BUFFERS=8 has headroom for this).
  * NO_VALIDATION init packet (`hash_size=0`) — bootloader skips SHA256.
  * 4 KB chunks (matches `selectObject(Data).maxSize` from the bootloader).

Used by runner.py's phase 2 (forced full DFU when DAL mismatch).
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
from common import (  # noqa: E402
    UUID_BUTTONLESS_WITHOUT_BONDS,
    UUID_BUTTONLESS_WITH_BONDS,
    UUID_DFU_CONTROL_POINT,
    UUID_DFU_PACKET,
    is_calliope_adv,
)

# --- DFU opcodes (Nordic SDK 17 nrf_dfu_op_t) -------------------------------
OP_CREATE = 0x01
OP_SET_PRN = 0x02
OP_CALC_CRC = 0x03
OP_EXECUTE = 0x04
OP_SELECT = 0x06
OP_RESPONSE = 0x60
OBJ_COMMAND = 0x01
OBJ_DATA = 0x02
RES_SUCCESS = 0x01

# --- DFU tunables (must match the bootloader ABI) ---------------------------
PAYLOAD_SIZE = 20       # bootloader ATT MTU is 23
PRN_INTERVAL = 6        # NRF_DFU_BLE_BUFFERS=8 → leaves headroom
CHUNK_SIZE = 4096       # bootloader's data-object max
SETTLE_AFTER_CREATE_S = 0.20   # page-erase async-ack settle


def extract_app_bin(hex_text: str) -> bytes:
    """Extract the V2 application binary region (0x1c000-0x77000) from a hex
    file, zero-padded to the highest populated address and 4-byte aligned.
    Mirrors widget's createAppBin / extractAppBin."""
    APP_START = 0x1C000
    APP_END = 0x77000
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
    max_addr = APP_START
    for a in blocks:
        if APP_START <= a < APP_END:
            max_addr = max(max_addr, a + 1)
    size = max_addr - APP_START
    if size <= 0:
        raise ValueError("hex has no bytes in V2 app region (0x1c000-0x77000)")
    size = (size + 3) & ~3  # 4-byte align
    out = bytearray(b"\x00" * size)
    for a, b in blocks.items():
        if APP_START <= a < APP_START + size:
            out[a - APP_START] = b
    return bytes(out)


def make_init_packet(app_size: int) -> bytes:
    """microbit_dfu_app_t with hash_size=0 (NO_VALIDATION path)."""
    magic = b"microbit_app"           # 12 B
    version = 1
    hash_size = 0
    hash_bytes = b"\x00" * 32
    return magic + struct.pack("<III", version, app_size, hash_size) + hash_bytes


class _DfuClient:
    """Thin wrapper around BleakClient with control-point response routing."""

    def __init__(self, client: BleakClient, log):
        self.client = client
        self.log = log
        self._waiters: dict[int, asyncio.Future[bytes]] = {}
        self._loop = asyncio.get_event_loop()

    def _on_ctrl(self, _, data: bytearray) -> None:
        if not data or data[0] != OP_RESPONSE:
            return
        op, res = data[1], data[2]
        fut = self._waiters.pop(op, None)
        if not fut or fut.done():
            return
        if res == RES_SUCCESS:
            fut.set_result(bytes(data[3:]))
        else:
            fut.set_exception(RuntimeError(f"op 0x{op:02x} res=0x{res:02x}"))

    async def setup(self) -> None:
        ctrl = self.client.services.get_characteristic(UUID_DFU_CONTROL_POINT)
        if ctrl is None:
            raise RuntimeError("DFU Control Point characteristic missing")
        await self.client.start_notify(ctrl, self._on_ctrl)

    async def cmd(self, op: int, body: bytes = b"", *, timeout: float = 30.0) -> bytes:
        f: asyncio.Future[bytes] = self._loop.create_future()
        self._waiters[op] = f
        await self.client.write_gatt_char(
            UUID_DFU_CONTROL_POINT, bytes([op]) + body, response=True
        )
        return await asyncio.wait_for(f, timeout=timeout)

    async def write_packet(self, data: bytes) -> None:
        await self.client.write_gatt_char(UUID_DFU_PACKET, data, response=False)

    def register_waiter(self, op: int) -> asyncio.Future[bytes]:
        f: asyncio.Future[bytes] = self._loop.create_future()
        self._waiters[op] = f
        return f


async def _scan(want_name_substring: str = "calliope", timeout: float = 10.0):
    found: dict[str, tuple[BLEDevice, AdvertisementData]] = {}

    def cb(d: BLEDevice, adv: AdvertisementData) -> None:
        if d.address in found:
            return
        nm = (d.name or adv.local_name or "").lower()
        if want_name_substring in nm or is_calliope_adv(d, adv):
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


async def _stream_chunk_with_prn(d: _DfuClient, chunk: bytes,
                                 cumulative_crc_in: int, cumulative_bytes_before: int) -> int:
    """Stream one chunk, returning the new cumulative CRC (pre-XOR)."""
    written = 0
    pkts = 0
    crc = cumulative_crc_in
    while written < len(chunk):
        end = min(written + PAYLOAD_SIZE, len(chunk))
        slice_ = chunk[written:end]
        is_prn = PRN_INTERVAL > 0 and (pkts + 1) == PRN_INTERVAL
        prn_fut: asyncio.Future[bytes] | None = None
        if is_prn:
            prn_fut = d.register_waiter(OP_CALC_CRC)
        await d.write_packet(slice_)
        # CRC32 incremental (zlib is matches the bootloader's zlib-CRC).
        crc = zlib.crc32(slice_, crc) & 0xFFFFFFFF
        written += len(slice_)
        pkts += 1
        if prn_fut is not None:
            payload = await asyncio.wait_for(prn_fut, timeout=15.0)
            dev_off, dev_crc = struct.unpack("<II", payload[:8])
            exp_off = cumulative_bytes_before + written
            exp_crc = crc  # device CRC matches zlib.crc32 directly (post-XOR semantics align)
            if dev_off != exp_off:
                raise RuntimeError(f"PRN offset mismatch at chunk-internal offset {written}: dev={dev_off} exp={exp_off}")
            if dev_crc != exp_crc:
                raise RuntimeError(f"PRN CRC mismatch at offset {exp_off}: dev=0x{dev_crc:08x} exp=0x{exp_crc:08x}")
            pkts = 0
    return crc


async def _send_data_object_stream(d: _DfuClient, data: bytes, on_progress) -> None:
    """Stream the full firmware in 4 KB chunks, with full per-chunk verify."""
    await d.cmd(OP_SELECT, bytes([OBJ_DATA]))
    sent = 0
    cumulative_crc = 0
    chunk_index = 0
    total_chunks = (len(data) + CHUNK_SIZE - 1) // CHUNK_SIZE
    while sent < len(data):
        chunk_size = min(CHUNK_SIZE, len(data) - sent)
        chunk = data[sent:sent + chunk_size]
        chunk_index += 1
        d.log(f"  chunk {chunk_index}/{total_chunks} ({chunk_size} B) at offset {sent}")
        await d.cmd(OP_CREATE, struct.pack("<BI", OBJ_DATA, chunk_size), timeout=30.0)
        await asyncio.sleep(SETTLE_AFTER_CREATE_S)
        new_crc = await _stream_chunk_with_prn(d, chunk, cumulative_crc, sent)
        cs = await d.cmd(OP_CALC_CRC)
        dev_off, dev_crc = struct.unpack("<II", cs[:8])
        if dev_off != sent + chunk_size:
            raise RuntimeError(f"data offset mismatch at chunk boundary: dev={dev_off} exp={sent+chunk_size}")
        if dev_crc != new_crc:
            raise RuntimeError(f"data CRC mismatch at offset {sent+chunk_size}: dev=0x{dev_crc:08x} exp=0x{new_crc:08x}")
        await d.cmd(OP_EXECUTE, timeout=30.0)
        cumulative_crc = new_crc
        sent += chunk_size
        on_progress(sent, len(data))


async def _send_init_packet(d: _DfuClient, app_size: int) -> None:
    await d.cmd(OP_SET_PRN, struct.pack("<H", 0))
    pkt = make_init_packet(app_size)
    d.log(f"  init packet: {len(pkt)} B (NO_VALIDATION)")
    await d.cmd(OP_SELECT, bytes([OBJ_COMMAND]))
    await d.cmd(OP_CREATE, struct.pack("<BI", OBJ_COMMAND, len(pkt)))
    for i in range(0, len(pkt), PAYLOAD_SIZE):
        await d.write_packet(pkt[i:i + PAYLOAD_SIZE])
    cs = await d.cmd(OP_CALC_CRC)
    ip_off, ip_crc = struct.unpack("<II", cs[:8])
    exp = zlib.crc32(pkt) & 0xFFFFFFFF
    if ip_off != len(pkt) or ip_crc != exp:
        raise RuntimeError(f"init-packet CRC mismatch (dev={ip_off}/{ip_crc:08x} exp={len(pkt)}/{exp:08x})")
    await d.cmd(OP_EXECUTE, timeout=30.0)
    await d.cmd(OP_SET_PRN, struct.pack("<H", PRN_INTERVAL))


async def _trigger_bootloader(device: BLEDevice, log) -> None:
    """Connect to the app, write 0x01 to the buttonless DFU char, wait for
    disconnect. Both UUID variants supported."""
    log(f"  connecting to app at {device.address}…")
    async with BleakClient(device, timeout=20.0) as client:
        bl_char = None
        for uuid, label in (
            (UUID_BUTTONLESS_WITHOUT_BONDS, "without-bonds (8EC90003)"),
            (UUID_BUTTONLESS_WITH_BONDS, "with-bonds (8EC90004)"),
        ):
            c = client.services.get_characteristic(uuid)
            if c:
                bl_char = c
                log(f"  buttonless char: {label}")
                break
        if not bl_char:
            raise RuntimeError("no buttonless DFU characteristic on this device")

        ack = asyncio.get_event_loop().create_future()

        def on_notify(_, data: bytearray) -> None:
            if not ack.done():
                ack.set_result(bytes(data))

        await client.start_notify(bl_char, on_notify)
        await client.write_gatt_char(bl_char, bytes([0x01]), response=True)
        try:
            await asyncio.wait_for(ack, timeout=3.0)
        except asyncio.TimeoutError:
            pass
        log("  buttonless write OK — waiting for device disconnect")
        # The device disconnects after a short delay.
        for _ in range(20):
            if not client.is_connected:
                return
            await asyncio.sleep(0.2)


async def _await_bootloader(timeout: float = 10.0):
    """Scan for DfuTarg after the buttonless reboot."""
    deadline = time.monotonic() + timeout

    def cb(d, adv):  # noqa: ARG001
        pass

    while time.monotonic() < deadline:
        pair = await _scan(want_name_substring="dfutarg", timeout=3.0)
        if pair and "dfutarg" in (pair[0].name or "").lower():
            return pair[0]
    return None


async def full_dfu_flash(hex_text: str, *, log=print) -> dict:
    """End-to-end: trigger bootloader → connect → init packet → stream data.
    Returns timing/result dict.
    """
    started = time.monotonic()
    result = {
        "ok": False,
        "elapsed_s": 0.0,
        "stage": "scan-app",
        "error": None,
        "app_bin_size": 0,
        "chunks": 0,
    }
    try:
        app_bin = extract_app_bin(hex_text)
        result["app_bin_size"] = len(app_bin)
        log(f"  app bin extracted: {len(app_bin)} bytes")

        pair = await _scan()
        if not pair:
            raise RuntimeError("no Calliope advertising — A+B+Reset needed?")
        device, adv = pair
        name = (device.name or adv.local_name or "")
        log(f"  found {device.address} ({name}) rssi={adv.rssi}")

        if "dfutarg" in name.lower():
            log("  device already in bootloader (DfuTarg) — skipping buttonless")
            target = device
        else:
            result["stage"] = "trigger-bootloader"
            await _trigger_bootloader(device, log)
            result["stage"] = "await-bootloader"
            log("  scanning for DfuTarg…")
            target = await _await_bootloader(timeout=12.0)
            if not target:
                raise RuntimeError("did not see DfuTarg advertise after buttonless reboot")

        result["stage"] = "dfu-connect"
        log(f"  connecting to bootloader at {target.address}…")
        async with BleakClient(target, timeout=20.0) as client:
            d = _DfuClient(client, log)
            await d.setup()

            result["stage"] = "init-packet"
            await _send_init_packet(d, app_size=len(app_bin))
            log("  init packet committed ✓")

            result["stage"] = "data-phase"

            def on_prog(sent: int, total: int) -> None:
                pass  # progress comes through `log` per chunk

            await _send_data_object_stream(d, app_bin, on_prog)
            result["chunks"] = (len(app_bin) + CHUNK_SIZE - 1) // CHUNK_SIZE
            log("  data phase complete ✓")
        result["ok"] = True
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        result["elapsed_s"] = time.monotonic() - started
    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("usage: ble_dfu.py <hex>")
        sys.exit(2)
    text = Path(sys.argv[1]).read_text()
    r = asyncio.run(full_dfu_flash(text))
    print(f"\nResult: ok={r['ok']} elapsed={r['elapsed_s']:.1f}s stage={r['stage']} chunks={r['chunks']}")
    if r["error"]:
        print(f"Error: {r['error']}")
