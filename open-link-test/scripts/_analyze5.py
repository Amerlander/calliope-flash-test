#!/usr/bin/env python3
"""Correct scan: look for BOTH PXT magic and uPy magic.

Codal's MicroBitMemoryMap::findHashes() scans the post-app flash pages
for either:
  - PXT/MakeCode magic: 0x923B8E70, 0x41A815C6, 0xC96698C4, 0x9751EE75
    (4 consecutive 32-bit words; hashes follow)
  - MicroPython magic: header containing 0x597F30FE + 0xC1B1D79D in a
    fixed 16-byte struct (records precede the header).

If either magic is present, partial flashing works because the codal
runtime reports valid region info over BLE.
"""
import struct
from pathlib import Path
from _analyze import parse_ihex, coalesce


PXT_MAGIC = struct.pack('<IIII', 0x923B8E70, 0x41A815C6, 0xC96698C4, 0x9751EE75)
UPY_MAGIC1 = struct.pack('<I', 0x597F30FE)
UPY_MAGIC2 = struct.pack('<I', 0xC1B1D79D)


def find_pxt_magic(coal):
    """Search for the 16-byte PXT magic blob."""
    hits = []
    for s, d in coal:
        i = 0
        while True:
            i = d.find(PXT_MAGIC, i)
            if i < 0:
                break
            addr = s + i
            # PXT format: 4 magic words (16 B), then hash blobs follow.
            # codal copies memcpy(memoryMap[1].hash, magicAddress + 4, 8)
            # so DAL hash is at magicAddress[4..6], MakeCode hash is at
            # magicAddress[6..8].
            if i + 32 <= len(d):
                dal_hash = d[i + 16 : i + 24]
                pxt_hash = d[i + 24 : i + 32]
            else:
                dal_hash = pxt_hash = b''
            hits.append((addr, dal_hash, pxt_hash))
            i += 1
    return hits


def find_upy_magic(coal):
    """Search for valid uPy magic pair (MAGIC1 at +0, MAGIC2 at +12)."""
    hits = []
    for s, d in coal:
        i = 0
        while True:
            i = d.find(UPY_MAGIC1, i)
            if i < 0:
                break
            if i + 16 <= len(d) and d[i + 12 : i + 16] == UPY_MAGIC2:
                ver, tlen, nreg, psize = struct.unpack_from('<HHHH', d, i + 4)
                hits.append((s + i, ver, tlen, nreg, psize))
            i += 1
    return hits


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
    coal = coalesce(parse_ihex(path.read_text()))
    print(f"\n=== {path.name} ===")
    pxt = find_pxt_magic(coal)
    upy = find_upy_magic(coal)
    if pxt:
        for addr, dal_h, pxt_h in pxt:
            print(f"  PXT magic @0x{addr:08X}")
            print(f"    DAL hash bytes:      {dal_h.hex()}")
            print(f"    MakeCode hash bytes: {pxt_h.hex()}")
    if upy:
        for addr, ver, tlen, nreg, psize in upy:
            print(f"  uPy magic header @0x{addr:08X}")
            print(f"    version={ver} table_len={tlen} num_regions={nreg} page_log2={psize}")
            # Records sit at [addr - tlen, addr) but codal walks
            # *(magicAddress - (2+i)*4) for i in 0..nregions
            for i in range(nreg):
                rec_off = addr - (2 + i) * 4
                rec = get_bytes(coal, rec_off, 16)
                rid, ht, addrpg, rlen = struct.unpack_from('<BBHI', rec, 0)
                rhash = rec[8:16]
                print(f"    region {i}: id={rid} ht={ht} 0x{addrpg*4096:08X}+{rlen} hash={rhash.hex()}")
    if not pxt and not upy:
        print("  NO partial-flash magic found (neither PXT nor uPy)")


def main():
    folder = Path(__file__).parent
    for f in sorted(folder.glob('*.hex')):
        analyze(f)


if __name__ == '__main__':
    main()
