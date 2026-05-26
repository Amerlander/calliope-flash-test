#!/usr/bin/env python3
"""Drill into the app region — find true reset vector + check for codal layout magic + dump bootloader bytes."""
import struct
from pathlib import Path
from _analyze import parse_ihex, coalesce


def get_bytes(coal, lo, length):
    out = bytearray(b'\xff' * length)
    for s, d in coal:
        e = s + len(d)
        if e <= lo or s >= lo + length:
            continue
        a = max(lo, s)
        b = min(lo + length, e)
        out[a - lo : b - lo] = d[a - s : b - s]
    return bytes(out)


def analyze(path):
    text = path.read_text()
    coal = coalesce(parse_ihex(text))
    print(f"\n=== {path.name} ===")

    # What's at 0x1C000?
    head = get_bytes(coal, 0x1C000, 256)
    sp = struct.unpack_from('<I', head, 0)[0]
    pc = struct.unpack_from('<I', head, 4)[0]
    nmi = struct.unpack_from('<I', head, 8)[0]
    hf = struct.unpack_from('<I', head, 12)[0]
    print(f"  @0x1C000 first 16 bytes: {head[:16].hex()}")
    print(f"    SP=0x{sp:08X}  PC=0x{pc:08X}  NMI=0x{nmi:08X}  HF=0x{hf:08X}")
    # Check if SP looks like SRAM (0x20000000-0x20040000) and PC looks like flash with Thumb bit
    valid_sp = 0x20000000 <= sp < 0x20040000
    valid_pc = (pc & 1) == 1 and 0x1C000 <= (pc & ~1) < 0x80000
    print(f"    valid_sp={valid_sp} valid_pc={valid_pc}")

    # If invalid, scan a small window for a real reset vector signature
    if not (valid_sp and valid_pc):
        for off in range(0, 0x4000, 0x100):
            test = get_bytes(coal, 0x1C000 + off, 16)
            tsp = struct.unpack_from('<I', test, 0)[0]
            tpc = struct.unpack_from('<I', test, 4)[0]
            if 0x20000000 <= tsp < 0x20040000 and (tpc & 1) == 1 and 0x1C000 <= (tpc & ~1) < 0x80000:
                print(f"    real vector table found @0x{0x1C000 + off:08X}: SP=0x{tsp:08X} PC=0x{tpc:08X}")
                break

    # What's at 0x78000 (bootloader)?
    blhead = get_bytes(coal, 0x78000, 32)
    if blhead != b'\xff' * 32:
        bl_sp = struct.unpack_from('<I', blhead, 0)[0]
        bl_pc = struct.unpack_from('<I', blhead, 4)[0]
        print(f"  Bootloader @0x78000 first 16 bytes: {blhead[:16].hex()}")
        print(f"    SP=0x{bl_sp:08X}  PC=0x{bl_pc:08X}")
    else:
        print(f"  Bootloader @0x78000: ABSENT")

    # UICR — bootloader start address is at 0x10001014
    uicr = get_bytes(coal, 0x10001014, 8)
    bl_addr = struct.unpack_from('<I', uicr, 0)[0]
    nrf_meta = struct.unpack_from('<I', uicr, 4)[0]
    print(f"  UICR[0x14] (NRF_UICR_BOOTLOADER_START_ADDRESS) = 0x{bl_addr:08X}")
    print(f"  UICR[0x18] (NRF_UICR_MBR_PARAMS_PAGE_ADDRESS)  = 0x{nrf_meta:08X}")

    # Look for the codal-microbit-v2 FlashLayout magic (0x597F30FE)
    LAYOUT_MAGIC_LE = struct.pack('<I', 0x597F30FE)
    found_layout = False
    for s, d in coal:
        i = d.find(LAYOUT_MAGIC_LE)
        if i >= 0:
            addr = s + i
            print(f"  FlashLayout magic 0x597F30FE @0x{addr:08X}")
            hdr = d[i : i + 32]
            num = hdr[4]
            print(f"    num_regions={num}")
            off = i + 8
            for r in range(min(num, 5)):
                if off + 12 > len(d):
                    break
                rid = struct.unpack_from('<H', d, off)[0]
                pid = struct.unpack_from('<H', d, off + 2)[0]
                start = struct.unpack_from('<I', d, off + 4)[0]
                end = struct.unpack_from('<I', d, off + 8)[0]
                print(f"    region {r}: id=0x{rid:04X} pid=0x{pid:04X} 0x{start:08X}-0x{end:08X}")
                off += 12
            found_layout = True
            break
    if not found_layout:
        # Try CALLIOPE_FLASH_LAYOUT_TABLE_OFFSET = 0x60 from MBR settings page (0x7E000)
        print("  FlashLayout magic 0x597F30FE: NOT FOUND")


def main():
    folder = Path(__file__).parent
    for f in sorted(folder.glob('*.hex')):
        analyze(f)


if __name__ == '__main__':
    main()
