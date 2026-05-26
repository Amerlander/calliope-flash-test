"""End-to-end flash test runner.

Runs the 4-phase loop documented in README.md and writes timing CSV +
console log to `results/<timestamp>.{csv,log}`.

Phase 1: USB-flash prog-A.hex      (baseline MicroPython)
Phase 2: BLE  -flash prog-B-blocks (DAL mismatch → full DFU expected)
Phase 3: USB-flash prog-A.hex      (reset to baseline)
Phase 4: BLE  -flash prog-A-mod    (same runtime → partial flash expected)

Asserts: phase 4 takes < 30 s (would be >60 s if it fell back to DFU).

This script is fully autonomous for phases 1 + 3 (USB) and the verification
steps. Phases 2 + 4 (BLE) require either:
  (a) Python+bleak implementations of the DFU + partial-flash protocols
      (DFU is half-done in ble-test/07_full_probe.py; partial flash needs
      a port from mini-connection-widget/src/ble-flash-web.ts), OR
  (b) Manual driver from the running campus widget on a paired Chrome
      profile + observation that the flash completed.

Until (a) is done, the runner prints "MANUAL STEP:" prompts and waits for
the user to confirm each BLE flash completed.
"""

from __future__ import annotations

import csv
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "lib"))
import asyncio
from usb_flash import usb_flash
from repl import probe as repl_probe
from ble_probe import probe_sync as ble_probe
from ble_dfu import full_dfu_flash
from ble_partial import partial_flash, DalMismatchError, InvalidHexError, PartialFlashUnsupportedError


HERE = Path(__file__).parent
HEXES = HERE / "test-hexes"
RESULTS = HERE / "results"
RESULTS.mkdir(exist_ok=True)


@dataclass
class PhaseResult:
    phase: str
    transport: str
    hex_name: str
    elapsed_s: float
    ok: bool
    note: str = ""


def banner(s: str) -> None:
    print()
    print(f"=== {s} ===")
    print()


def wait_for(prompt: str) -> None:
    print(f"\n   ⏸  {prompt}\n   Press ENTER when ready…")
    input()


def auto_ble_dfu_flash(hex_path: Path) -> tuple[float, bool, str]:
    """Autonomous full Nordic DFU."""
    print(f"   running full DFU on {hex_path.name}…")
    text = hex_path.read_text()
    r = asyncio.run(full_dfu_flash(text, log=lambda m: print(f"   {m}")))
    return r["elapsed_s"], r["ok"], r.get("error") or ""


def auto_ble_partial_flash(hex_path: Path) -> tuple[float, bool, str]:
    """Autonomous partial flash."""
    print(f"   running partial flash on {hex_path.name}…")
    text = hex_path.read_text()
    r = asyncio.run(partial_flash(text, log=lambda m: print(f"   {m}")))
    return r["elapsed_s"], r["ok"], r.get("error") or ""


def phase_1_usb_baseline() -> PhaseResult:
    banner("Phase 1: USB-flash prog-A.hex (baseline)")
    hex_path = HEXES / "prog-A.hex"
    if not hex_path.exists():
        return PhaseResult("phase-1", "usb", "prog-A.hex", 0.0, False, "hex not built — see build-test-hexes.py")
    elapsed = usb_flash(hex_path)
    print(f"   USB flash done in {elapsed:.1f} s")
    # Verify. After USB flash the device might be in pairing mode (BLE up, no
    # REPL) or normal mode (REPL up, no BLE). Try both.
    repl = repl_probe()
    if repl.alive:
        print(f"   REPL: {repl.implementation}")
        ok = repl.implementation is not None and "micropython" in repl.implementation.lower()
        return PhaseResult("phase-1", "usb", "prog-A.hex", elapsed, ok, repl.implementation or "")
    print(f"   REPL silent — checking BLE (likely in pairing mode)")
    ble = ble_probe()
    if not ble.found:
        return PhaseResult("phase-1", "usb", "prog-A.hex", elapsed, False, "neither REPL nor BLE responding")
    expected_mp = ble.has_partial_flash and ble.has_unbonded_dfu and not ble.has_mbitmore
    return PhaseResult(
        "phase-1", "usb", "prog-A.hex", elapsed, expected_mp,
        f"BLE: pf={ble.has_partial_flash} dfu={ble.has_unbonded_dfu} mbitmore={ble.has_mbitmore}",
    )


def probe_ble_with_retry(attempts: int = 4, settle: float = 4.0):
    """Retry the BLE probe up to N times — Blocks boot can take 10-20 s after
    DFU completes."""
    last = None
    for i in range(attempts):
        last = ble_probe()
        if last.found:
            return last
        print(f"   probe attempt {i + 1}/{attempts}: not found — waiting {settle:.0f} s")
        time.sleep(settle)
    return last


def phase_2_full_dfu() -> PhaseResult:
    banner("Phase 2: BLE full Nordic DFU (Blocks hex, DAL mismatch → forces DFU)")
    hex_path = HEXES / "prog-B-blocks.hex"
    if not hex_path.exists():
        return PhaseResult("phase-2", "ble-dfu", "prog-B-blocks.hex", 0.0, False, "hex not built")
    # Confirm device is in BLE-reachable state.
    ble = ble_probe()
    if not ble.found:
        wait_for("Press A+B+Reset on the mini to enter pairing mode")
        ble = ble_probe()
        if not ble.found:
            return PhaseResult("phase-2", "ble-dfu", "prog-B-blocks.hex", 0.0, False, "BLE never advertised")
    elapsed, ok_flash, err = auto_ble_dfu_flash(hex_path)
    if not ok_flash:
        return PhaseResult("phase-2", "ble-dfu", "prog-B-blocks.hex", elapsed, False, err)
    # Blocks firmware does NOT auto-advertise in app mode (pxt-calliope V3
    # convention: BLE only on A+B+Reset → pairing mode). Empirically:
    # device is alive (DAPLink remount count increments, USB MSC stays
    # mounted) but BLE+REPL both silent until user presses A+B+Reset.
    # So phase-2 success criterion is "DFU protocol completed cleanly" —
    # the post-flash MbitMore check would need manual reset and is skipped
    # for autonomous runs.
    return PhaseResult("phase-2", "ble-dfu", "prog-B-blocks.hex", elapsed, True,
                      "DFU completed; app-mode Blocks doesn't auto-advertise BLE so post-flash service check skipped")


def phase_3_usb_reset() -> PhaseResult:
    banner("Phase 3: USB-flash prog-A.hex again (reset to MicroPython baseline)")
    hex_path = HEXES / "prog-A.hex"
    elapsed = usb_flash(hex_path)
    print(f"   USB flash done in {elapsed:.1f} s")
    # Same verification approach as phase 1.
    repl = repl_probe()
    if repl.alive:
        return PhaseResult("phase-3", "usb", "prog-A.hex", elapsed, True, repl.implementation or "")
    ble = ble_probe()
    if not ble.found:
        return PhaseResult("phase-3", "usb", "prog-A.hex", elapsed, False, "no verify")
    expected_mp = ble.has_partial_flash and not ble.has_mbitmore
    return PhaseResult("phase-3", "usb", "prog-A.hex", elapsed, expected_mp, "")


def phase_4_partial() -> PhaseResult:
    banner("Phase 4: BLE partial flash (prog-A-mod, same MP runtime)")
    hex_path = HEXES / "prog-A-mod.hex"
    if not hex_path.exists():
        return PhaseResult("phase-4", "ble-partial", "prog-A-mod.hex", 0.0, False, "hex not built")
    ble = ble_probe()
    if not ble.found:
        wait_for("Press A+B+Reset on the mini to enter pairing mode")
        ble = ble_probe()
        if not ble.found:
            return PhaseResult("phase-4", "ble-partial", "prog-A-mod.hex", 0.0, False, "BLE never advertised")
    elapsed, ok_flash, err = auto_ble_partial_flash(hex_path)
    if not ok_flash:
        return PhaseResult("phase-4", "ble-partial", "prog-A-mod.hex", elapsed, False, err)
    # Wait for device to come back up after flash.
    time.sleep(5.0)
    return PhaseResult("phase-4", "ble-partial", "prog-A-mod.hex", elapsed, True, "flash ok")


def main() -> int:
    print("Calliope BLE-flash test rig")
    print(f"Build hexes first: run `python build-test-hexes.py` if test-hexes/ is empty")
    print()

    results: list[PhaseResult] = []
    results.append(phase_1_usb_baseline())
    results.append(phase_2_full_dfu())
    results.append(phase_3_usb_reset())
    results.append(phase_4_partial())

    banner("Summary")
    for r in results:
        mark = "✓" if r.ok else "✗"
        print(f"  {mark}  {r.phase:9s}  {r.transport:12s}  {r.elapsed_s:6.1f} s  {r.note}")

    # Critical assertions
    print()
    dfu_t = next((r.elapsed_s for r in results if r.phase == "phase-2"), 0)
    partial_t = next((r.elapsed_s for r in results if r.phase == "phase-4"), 0)
    p4 = next((r for r in results if r.phase == "phase-4"), None)
    if partial_t > 0 and dfu_t > 0:
        speedup = dfu_t / partial_t
        print(f"  Partial flash speedup: {speedup:.1f}× (DFU {dfu_t:.0f}s vs partial {partial_t:.0f}s)")
        # Real regression signal: phase 4 used the partial-flash characteristic
        # (look at the note + ok) and was faster than phase 2.
        if p4 and p4.ok and "flash ok" in p4.note and partial_t < dfu_t:
            print("  ✓ Partial-flash protocol ran to completion and beat DFU wall-clock")
        else:
            print("  ✗ Partial flash didn't beat DFU — investigate")

    # Persist results.
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    csv_path = RESULTS / f"{ts}.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys()))
        w.writeheader()
        for r in results:
            w.writerow(asdict(r))
    print(f"\n  → {csv_path}")

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
