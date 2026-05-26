#!/usr/bin/env python3
"""Re-run the full static analysis on all 9 hexes and emit a JSON summary.

Used to refresh ANALYSIS.md/html data after a hex update.
"""
from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from _analyze import parse_ihex, coalesce, find_strings

HEX_DIR = HERE.parent
HEXES = [
    "blocks.hex",
    "makecode-1-ble.hex",
    "makecode-1-radio.hex",
    "makecode-2-ble.hex",
    "makecode-2-radio.hex",
    "makecode-3-ble.hex",
    "makecode-3-radio.hex",
    "python_ble.hex",
    "python_radio.hex",
]

PXT_MAGIC = struct.pack("<IIII", 0x923B8E70, 0x41A815C6, 0xC96698C4, 0x9751EE75)
UPY_M1 = struct.pack("<I", 0x597F30FE)
UPY_M2 = struct.pack("<I", 0xC1B1D79D)


def get_bytes(coal, lo, length):
    out = bytearray(b"\xff" * length)
    for s, d in coal:
        e = s + len(d)
        if e <= lo or s >= lo + length:
            continue
        a = max(lo, s)
        b = min(lo + length, e)
        out[a - lo : b - lo] = d[a - s : b - s]
    return bytes(out)


def find_pxt(coal):
    for s, d in coal:
        i = d.find(PXT_MAGIC)
        if i >= 0:
            dal = d[i + 16 : i + 24].hex()
            mc = d[i + 24 : i + 32].hex()
            return {"addr": s + i, "dal_hash": dal, "mc_hash": mc}
    return None


def find_upy(coal):
    for s, d in coal:
        i = 0
        while True:
            i = d.find(UPY_M1, i)
            if i < 0:
                break
            if i + 16 <= len(d) and d[i + 12 : i + 16] == UPY_M2:
                ver, tlen, nreg, psize = struct.unpack_from("<HHHH", d, i + 4)
                if 1 <= ver <= 10 and nreg < 8 and psize == 12:
                    regions = []
                    for r in range(nreg):
                        rec = i - tlen + r * 16
                        if 0 <= rec and rec + 16 <= len(d):
                            rid, ht, addrpg, rlen = struct.unpack_from("<BBHI", d, rec)
                            rhash = d[rec + 8 : rec + 16].hex()
                            regions.append({
                                "id": rid, "ht": ht,
                                "start": f"0x{addrpg * 4096:08X}",
                                "len": rlen, "hash": rhash,
                            })
                    return {"addr": s + i, "version": ver, "table_len": tlen,
                            "num_regions": nreg, "regions": regions}
            i += 1
    return None


def analyze(name):
    path = HEX_DIR / name
    if not path.is_file():
        return {"name": name, "error": "missing"}

    text = path.read_text()
    coal = coalesce(parse_ihex(text))
    total = sum(len(d) for _, d in coal)

    # Vector table at 0x0 + 0x1C000
    head = get_bytes(coal, 0, 16)
    sp0 = struct.unpack_from("<I", head, 0)[0]
    pc0 = struct.unpack_from("<I", head, 4)[0]
    app = get_bytes(coal, 0x1C000, 16)
    sp_app = struct.unpack_from("<I", app, 0)[0]
    pc_app = struct.unpack_from("<I", app, 4)[0]

    # UICR
    uicr = get_bytes(coal, 0x10001014, 4)
    uicr_bl = struct.unpack_from("<I", uicr, 0)[0]

    # Section presence
    sd_present = any(s <= 0x1000 < s + len(d) for s, d in coal)
    bootloader_present = any(s <= 0x78000 < s + len(d) for s, d in coal)
    bootloader_bytes = get_bytes(coal, 0x78000, 16).hex() if bootloader_present else None

    # Identity strings
    identity = []
    sentinels = ["v2.1.2", "with nRF", "MakeCode", "Calliope mini", "microbit_app",
                 "calliope.rc", "campus"]
    for s, d in coal:
        if s >= 0x80000:
            continue
        strs = find_strings(d, min_len=8)
        for off, line in strs:
            if any(k in line for k in sentinels):
                identity.append({"addr": f"0x{s + off:08X}", "text": line[:100]})

    return {
        "name": name,
        "file_size": os.path.getsize(path),
        "flash_bytes": total,
        "pattern": "B (full image)" if bootloader_present else "A (partial)",
        "mbr": "codal (0xB00)" if sp0 == 0x20000400 else f"stock Nordic (SP=0x{sp0:X})",
        "vector_0x0": {"SP": f"0x{sp0:08X}", "PC": f"0x{pc0:08X}"},
        "vector_0x1C000": {"SP": f"0x{sp_app:08X}", "PC": f"0x{pc_app:08X}"},
        "uicr_bootloader_addr": f"0x{uicr_bl:08X}",
        "softdevice_present": sd_present,
        "bootloader_at_0x78000": bootloader_present,
        "bootloader_first_bytes": bootloader_bytes,
        "pxt_magic": find_pxt(coal),
        "upy_layout": find_upy(coal),
        "identity_strings": identity[:8],
    }


def main():
    results = [analyze(h) for h in HEXES]
    out = HEX_DIR / "results" / "static-analysis.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    # Print summary table
    print(f"{'file':<24} {'size':>8} {'pattern':<14} {'partial-flash magic':<26} {'identity':<40}")
    print("-" * 120)
    for r in results:
        if "error" in r:
            print(f"{r['name']:<24} MISSING")
            continue
        magic_summary = (
            f"PXT @{r['pxt_magic']['addr']:#X}"
            if r["pxt_magic"]
            else f"uPy @{r['upy_layout']['addr']:#X}"
            if r["upy_layout"]
            else "none"
        )
        identity_one = r["identity_strings"][0]["text"] if r["identity_strings"] else "?"
        print(f"{r['name']:<24} {r['file_size']:>8,} {r['pattern']:<14} {magic_summary:<26} {identity_one[:40]}")
    print(f"\nfull JSON: {out}")


if __name__ == "__main__":
    main()
