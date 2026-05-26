"""Overlay a freshly-built bootloader.hex on top of an existing MicroPython
MINI hex, producing a single combined Intel HEX file we can drag onto the
DAPLink E:/ drive.

The deployed Calliope mini 3 bootloader gets baked into the codal build via
`FIRMWARE/codal-microbit-v2/lib/bootloader.o`, but that .o is older than the
current `FIRMWARE/v3-bootloader/bootloader/microbit/armgcc/_build/bootloader.hex`
(verified by byte-diff 2026-05-21). The new build has NRF_SDH_BLE_GATT_MAX_MTU_SIZE=247
and a properly-wired MTU exchange handler; we want it on the device.

Approach:
- Read both hex files into a flat memory map.
- Remove any bytes from the host hex that live in the bootloader region
  (0x77000-0x80000 on nRF52833).
- Overwrite with the new bootloader's bytes.
- Re-emit as a flat Intel HEX (single-board, NOT universal-hex; DAPLink
  accepts plain Intel HEX too).

Usage:
  python merge-bootloader.py HOST_HEX BOOTLOADER_HEX [-o OUT]

Recovery if something goes wrong: drag any working MINI-derived hex onto
E:/ and DAPLink restores the prior bootloader + app together.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def parse_hex(path: Path) -> dict[int, int]:
    mem: dict[int, int] = {}
    ext = 0
    seg = 0
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line.startswith(":"):
                continue
            n = int(line[1:3], 16)
            addr = int(line[3:7], 16)
            rtype = int(line[7:9], 16)
            data_hex = line[9:9 + n * 2]
            if rtype == 0x04:
                ext = int(data_hex, 16) << 16
                seg = 0
            elif rtype == 0x02:
                seg = int(data_hex, 16) << 4
                ext = 0
            elif rtype == 0x00:
                full = ext + seg + addr
                for i in range(n):
                    mem[full + i] = int(line[9 + i * 2:11 + i * 2], 16)
    return mem


def emit_hex(mem: dict[int, int], out: Path) -> None:
    """Emit a flat Intel HEX with type-04 extended-linear address records.
    Writes 16-byte data records, contiguous within each 64 KB segment."""
    addrs = sorted(mem)
    if not addrs:
        raise ValueError("empty memory")
    last_ext = -1
    pending: list[tuple[int, list[int]]] = []  # (addr_low_16, bytes)

    def flush_records(records, fh):
        for low_addr, bs in records:
            n = len(bs)
            checksum = (
                n + (low_addr >> 8) + (low_addr & 0xFF) + 0x00 + sum(bs)
            ) & 0xFF
            checksum = (0x100 - checksum) & 0xFF
            data_hex = "".join(f"{b:02X}" for b in bs)
            fh.write(f":{n:02X}{low_addr:04X}00{data_hex}{checksum:02X}\n")

    with out.open("w") as fh:
        i = 0
        records: list[tuple[int, list[int]]] = []
        while i < len(addrs):
            base = addrs[i]
            ext = base >> 16
            low = base & 0xFFFF
            if ext != last_ext:
                # Flush pending before emitting new EXT record.
                if records:
                    flush_records(records, fh)
                    records = []
                ext_chk = (0x02 + 0x00 + 0x00 + 0x04 + (ext >> 8) + (ext & 0xFF)) & 0xFF
                ext_chk = (0x100 - ext_chk) & 0xFF
                fh.write(f":02000004{ext:04X}{ext_chk:02X}\n")
                last_ext = ext
            # Pull contiguous run from this address, max 16 bytes per record.
            run: list[int] = [mem[base]]
            j = i + 1
            while j < len(addrs) and addrs[j] == addrs[j - 1] + 1 and len(run) < 16:
                # Stay within the same 64 KB segment.
                if (addrs[j] >> 16) != ext:
                    break
                run.append(mem[addrs[j]])
                j += 1
            records.append((low, run))
            # Flush periodically to keep memory small.
            if len(records) >= 1024:
                flush_records(records, fh)
                records = []
            i = j
        if records:
            flush_records(records, fh)
        # EOF record.
        fh.write(":00000001FF\n")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("host_hex", type=Path, help="App+SoftDevice hex (eg prog-A.hex)")
    p.add_argument("bootloader_hex", type=Path, help="Fresh bootloader.hex to overlay")
    p.add_argument("-o", "--out", type=Path, default=None, help="Output path")
    p.add_argument("--region-start", type=lambda s: int(s, 0), default=0x77000,
                   help="Bootloader CODE region start (default 0x77000)")
    p.add_argument("--region-end", type=lambda s: int(s, 0), default=0x7E000,
                   help="Bootloader CODE region end (exclusive, default 0x7E000). "
                        "Settings page at 0x7E000+ is preserved from host hex so the "
                        "new bootloader inherits valid DFU state on first boot.")
    args = p.parse_args()

    host = parse_hex(args.host_hex)
    boot = parse_hex(args.bootloader_hex)

    # Sanity: new bootloader bytes should be inside [region-start, region-end).
    boot_lo = min(boot)
    boot_hi = max(boot) + 1
    if boot_lo < args.region_start or boot_hi > args.region_end:
        print(f"warning: bootloader bytes 0x{boot_lo:x}-0x{boot_hi:x} extend "
              f"outside region 0x{args.region_start:x}-0x{args.region_end:x}",
              file=sys.stderr)

    # Drop any host bytes in the bootloader region.
    dropped = 0
    new_mem = {}
    for a, b in host.items():
        if args.region_start <= a < args.region_end:
            dropped += 1
        else:
            new_mem[a] = b
    # Overlay bootloader bytes.
    for a, b in boot.items():
        new_mem[a] = b

    # Default output: alongside host_hex.
    out = args.out or args.host_hex.with_name(args.host_hex.stem + "-newbootloader.hex")
    emit_hex(new_mem, out)

    print(f"host hex:       {args.host_hex.name}  ({len(host)} bytes)")
    print(f"bootloader hex: {args.bootloader_hex.name}  ({len(boot)} bytes, 0x{boot_lo:x}-0x{boot_hi:x})")
    print(f"dropped host bytes in region: {dropped}")
    print(f"final memory size: {len(new_mem)} bytes")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
