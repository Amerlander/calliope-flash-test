#!/usr/bin/env python3
"""Analyze Calliope/micro:bit hex files: detect format, sections, embedded strings."""
import re
import sys
import struct
from pathlib import Path

# Block IDs used by the "universal hex" (micro:bit V1+V2 dual) format
# See https://tech.microbit.org/software/hex-format/
UHEX_BLOCK_OTHER_DATA = 0x0D  # custom data section (e.g. PXT user data)
UHEX_RECORD_BLOCK_START = 0x0A  # block start
UHEX_RECORD_BLOCK_END = 0x0B    # block end
UHEX_RECORD_PADDED_DATA = 0x0C
UHEX_RECORD_CUSTOM_DATA = 0x0D
UHEX_RECORD_OTHER_DATA = 0x0E


def parse_ihex(text):
    """Yield (addr, data) tuples in linear address space.

    Handles standard Intel HEX with type-04 extended-linear-address records.
    Returns a list of (addr32, bytes) chunks in file order.
    """
    seg = 0
    chunks = []
    line_no = 0
    for raw in text.splitlines():
        line_no += 1
        line = raw.strip()
        if not line.startswith(':'):
            continue
        try:
            n = int(line[1:3], 16)
            addr = int(line[3:7], 16)
            rtype = int(line[7:9], 16)
            data_hex = line[9:9 + n * 2]
            data = bytes.fromhex(data_hex)
        except Exception:
            continue
        if rtype == 0x00:  # data
            chunks.append((seg | addr, data))
        elif rtype == 0x04:  # ext linear address
            seg = int(data.hex(), 16) << 16
        elif rtype == 0x02:  # ext segment address
            seg = int(data.hex(), 16) << 4
        elif rtype == 0x05:  # start linear address
            pass
        elif rtype == 0x01:  # EOF
            break
        # custom universal-hex types 0x0A-0x0E are ignored for now (data
        # records within blocks are still type 0x00, so they get captured)
    return chunks


def coalesce(chunks):
    """Sort + merge adjacent chunks into [(start, bytes)]."""
    chunks = sorted(chunks)
    out = []
    for start, data in chunks:
        if out and start == out[-1][0] + len(out[-1][1]):
            out[-1] = (out[-1][0], out[-1][1] + data)
        else:
            out.append((start, data))
    return out


def find_strings(data, min_len=8):
    """Return list of printable ASCII runs >= min_len chars."""
    out = []
    cur = bytearray()
    start = 0
    for i, b in enumerate(data):
        if 0x20 <= b < 0x7f:
            if not cur:
                start = i
            cur.append(b)
        else:
            if len(cur) >= min_len:
                out.append((start, bytes(cur).decode('ascii', 'replace')))
            cur = bytearray()
    if len(cur) >= min_len:
        out.append((start, bytes(cur).decode('ascii', 'replace')))
    return out


# Known address ranges for Calliope mini 3 / micro:bit V2 (nRF52833)
RANGES = [
    ('MBR',                 0x00000000, 0x00001000),
    ('SoftDevice',          0x00001000, 0x0001C000),
    ('App (S140 layout)',   0x0001C000, 0x00073000),
    ('FS / scratch',        0x00073000, 0x00078000),
    ('Bootloader',          0x00078000, 0x0007F000),
    ('MBR settings',        0x0007E000, 0x0007F000),
    ('Bootloader settings', 0x0007F000, 0x00080000),
    ('UICR',                0x10001000, 0x10002000),
]

# Sentinel strings that pin firmware identity
SENTINELS = [
    'MICROBIT_RELEASE',
    'micro:bit', 'microbit', 'micro_bit',
    'CALLIOPE', 'Calliope', 'calliope',
    'codal', 'CODAL',
    'BLE', 'Bluetooth', 'bluetooth',
    'SECURITY_MODE',
    'BONDED', 'JUST_WORKS', 'PASSKEY',
    'OpenLink', 'open link', 'OPEN_LINK',
    'MicroPython', 'micropython',
    'MakeCode', 'makecode', 'pxt',
    'mbit_more', 'MbitMore',
    'radio',
    'DAPLINK', 'DAPLink', 'daplink',
    'campus',
    'gap_device_name', 'BLE_DEVICE_NAME',
    'BootKVM', 'flashlayout', 'flash_layout', 'FlashLayout',
    'partial_flash', 'PartialFlash',
]


def section_summary(coal):
    """Return list of (label, start, end, size) for non-empty hardware sections."""
    out = []
    for label, lo, hi in RANGES:
        size = 0
        s_first, s_last = None, None
        for start, data in coal:
            end = start + len(data)
            if end <= lo or start >= hi:
                continue
            ov_s = max(start, lo)
            ov_e = min(end, hi)
            if ov_e > ov_s:
                size += ov_e - ov_s
                if s_first is None or ov_s < s_first:
                    s_first = ov_s
                if s_last is None or ov_e > s_last:
                    s_last = ov_e
        if size > 0:
            out.append((label, s_first, s_last, size))
    return out


def is_universal_hex(text):
    """A universal hex contains :0Axxxx0A... block-start records."""
    return ':0A' in text and any(
        l[7:9].upper() in ('0A', '0B', '0C', '0D', '0E')
        for l in text.splitlines() if l.startswith(':')
    )


def find_sentinels(coal):
    """Return list of (addr, string) entries matching SENTINELS, with surrounding context."""
    hits = []
    for start, data in coal:
        strs = find_strings(data, min_len=6)
        for off, s in strs:
            for sentinel in SENTINELS:
                if sentinel.lower() in s.lower():
                    hits.append((start + off, s.strip()))
                    break
    return hits


def analyze(path):
    text = path.read_text()
    raw_chunks = parse_ihex(text)
    coal = coalesce(raw_chunks)

    print(f"=== {path.name} ({path.stat().st_size:,} bytes) ===")

    # Format detection: universal hex marker?
    uhex = is_universal_hex(text)
    if uhex:
        print("  Format: Universal HEX (contains universal-hex block records)")
    else:
        print("  Format: Plain Intel HEX")

    # Section coverage
    sect = section_summary(coal)
    print("  Sections present:")
    for label, s, e, sz in sect:
        print(f"    {label:<22} 0x{s:08X}-0x{e:08X}   {sz:>7,} bytes")

    # Total covered
    total = sum(len(d) for _, d in coal)
    print(f"  Total flash bytes covered: {total:,}")

    # Reset vector + start of region at 0
    for start, data in coal[:3]:
        if start <= 0 and start + len(data) > 8:
            off = -start
            sp = struct.unpack_from('<I', data, off)[0]
            pc = struct.unpack_from('<I', data, off + 4)[0]
            print(f"  Initial SP @0x0:        0x{sp:08X}")
            print(f"  Initial PC @0x4:        0x{pc:08X}")
            break

    # App region first 8 bytes (if present)
    for start, data in coal:
        if start <= 0x1C000 < start + len(data):
            off = 0x1C000 - start
            if off + 8 <= len(data):
                sp = struct.unpack_from('<I', data, off)[0]
                pc = struct.unpack_from('<I', data, off + 4)[0]
                print(f"  App SP @0x1C000:        0x{sp:08X}")
                print(f"  App PC @0x1C004:        0x{pc:08X}")
            break

    # Sentinel strings
    hits = find_sentinels(coal)
    # Dedupe within a small window
    seen = set()
    uniq = []
    for addr, s in hits:
        key = s.strip()
        if key in seen:
            continue
        seen.add(key)
        uniq.append((addr, key))
    print(f"  Identifying strings ({len(uniq)} unique):")
    for addr, s in uniq[:40]:
        # truncate
        ss = s if len(s) <= 90 else s[:87] + '...'
        print(f"    0x{addr:08X}  {ss}")
    if len(uniq) > 40:
        print(f"    ... +{len(uniq) - 40} more")
    print()


def main():
    folder = Path(__file__).parent
    if len(sys.argv) > 1:
        files = [Path(p) for p in sys.argv[1:]]
    else:
        files = sorted(folder.glob('*.hex'))
    for f in files:
        analyze(f)


if __name__ == '__main__':
    main()
