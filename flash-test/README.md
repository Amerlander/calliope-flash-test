# BLE-flash test rig — Calliope mini

Autonomous (mostly) end-to-end test of the flash pipeline: USB drag-flash + Web Bluetooth full DFU + Web Bluetooth partial flash. Measures timing for each path so we can track regressions in the widget / firmware / partial-flash protocol.

## What this tests

The 4-phase loop the user proposed:

| step | what | transport | expected speed | expected path |
|---|---|---|---|---|
| 1 | Flash baseline MicroPython hex (program **A**) | USB MSC | <5 s | DAPLink drag-flash |
| 2 | Flash a **runtime-mismatched** hex (Blocks `.hex`) | Web BLE | ~110 s (slow) | Nordic Secure DFU (full) |
| 3 | Flash program **A** again | USB MSC | <5 s | DAPLink drag-flash |
| 4 | Flash a slightly-modified A (program **A′**, same MicroPython runtime, different `main.py`) | Web BLE | ~3–5 s (fast) | Partial flashing via `E97DD91D` |

We assert: step 2 takes >60 s (verifies the slow path), step 4 takes <15 s (verifies the fast path now works), and step 4 doesn't accidentally fall through to DFU.

## Feasibility — what's automated vs. manual

| component | autonomous? | reason |
|---|---|---|
| USB drag-flash | ✅ Python | `shutil.copy(hex, "E:/MINI.hex")` |
| Verify via USB REPL | ✅ Python | `pyserial` to `COM8`, `print(2+2)` |
| BLE inspect (services, hashes) | ✅ Python | `bleak`, existing `ble-test/02_inspect.py` |
| **Full BLE DFU** | ✅ Python | reuse `ble-test/07_full_probe.py` |
| **BLE partial flashing** | ⚠️ Python (need to port from `mini-connection-widget/src/ble-flash-web.ts`) | ~80 lines of TypeScript to translate |
| **Web Bluetooth via Chrome+Puppeteer** | ❌ blocked | Chrome's BLE chooser dialog is a native OS UI; Puppeteer cannot click it |
| Workaround: `getDevices()` pre-permitted | ⚠️ feasible | Manual one-time pairing per browser profile; afterwards Puppeteer can use `getDevices()` to reconnect silently |
| Reset device between cycles | ⚠️ depends | USB drag-flash usually re-enters pairing mode automatically (per our empirical observation in this session); if not, A+B+Reset is manual |
| DAPLink-driven reset / button simulation | ❌ no | No interface code anywhere in the repo; DAPLink firmware we use doesn't expose a button-press command |

**Bottom line:** the Python+`bleak` route gets us full end-to-end automation of the **BLE protocol** (which is what we actually want to regression-test). The Chrome widget code is validated separately, by checking that the Python and widget BLE behaviour produces identical wire traffic (matching DAL hash, identical flash addresses, identical PRN windows).

If Chrome E2E is later needed: pair the test mini once manually, save the Chrome profile, and use Puppeteer with `--user-data-dir=<saved-profile>` so `getDevices()` returns the test mini without a chooser.

## Test sequence (Python-driven)

```
runner.py [--cycles N] [--keep-hexes]

  phase 1: USB-flash prog-A.hex, wait for reboot
           verify REPL: print(sys.implementation), expect MicroPython v1.23
  phase 2: scan BLE (8 s), connect, classify session
           if app-mode: trigger A+B+Reset prompt (or use buttonless DFU char)
           wait for pairing-mode advertise
           run full Nordic DFU with the Blocks hex
           measure: t_dfu_start → t_dfu_complete
           verify: BLE-inspect post-flash, expect MbitMore service present
  phase 3: USB-flash prog-A.hex again
           verify REPL again
  phase 4: scan + connect (cached) or A+B+Reset
           run partial flash with prog-A-mod.hex
           measure: t_partial_start → t_partial_complete
           verify: BLE-inspect, expect runtime hash unchanged; REPL: read main.py
  emit: timing CSV + console summary
```

## Test hexes

Built once, committed to `test-hexes/`:

- **`prog-A.hex`** — MicroPython MINI.hex from `FIRMWARE/micropython-calliope-mini-v3` (variant Q, md5 `b35b48115c6e02a4467fe08c6d11a211`) + injected `main.py`:
  ```python
  from microbit import *
  display.show("A")
  ```
- **`prog-A-mod.hex`** — same MINI.hex + `main.py`:
  ```python
  from microbit import *
  display.show("a")   # lowercase — single-byte FS diff
  ```
- **`prog-B-blocks.hex`** — bundled blocks hex from `mini-connection-widget/src/assets/blocks.hex` (V5 scratch lib, gives runtime-mismatch with MicroPython → forces DFU)

Injection is done with `@microbit/microbit-fs` (Node) or `microfs.py` (Python equivalent). Build script: `build-test-hexes.py`.

## Directory layout

```
flash-test/
├── README.md           — this file
├── build-test-hexes.py — produces test-hexes/*.hex from base firmwares + user .py
├── runner.py           — orchestrator: runs the 4-phase test, emits CSV
├── lib/
│   ├── usb_flash.py    — drag-flash + reboot detection (E:/ drive)
│   ├── repl.py         — pyserial REPL probe on COM8
│   ├── ble_dfu.py      — full Nordic DFU (extracted from ble-test/07_full_probe.py)
│   └── ble_partial.py  — partial flash (port of ble-flash-web.ts session.run())
├── test-hexes/
│   ├── prog-A.hex
│   ├── prog-A-mod.hex
│   └── prog-B-blocks.hex
└── results/
    ├── YYYY-MM-DDTHH-MM-SS.csv
    └── YYYY-MM-DDTHH-MM-SS.log
```

## Why this is the right test

Three concrete failures we'd catch:

1. **Partial-flash regression after a widget change.** If a future widget patch breaks `parseMicroPythonHex` (we just shipped `a54f89d` after two prior bugs), phase 4 throws or falls back to DFU — measured timing would jump from ~3 s to ~110 s. The test fails loudly.
2. **Codal partial-flash protocol drift.** If we ever bump `codal-microbit-v2` again (we just bumped to `v0.3.5-calliope-2`), the partial-flashing memory map could shift region IDs or hash semantics again — phase 4 would mismatch DAL hash → fall back to DFU → fail.
3. **Bootloader regression.** A faulty bootloader rebuild could break the `8EC90003` open buttonless DFU — phase 2's "enter bootloader" command would time out → caught by the per-phase timeout.

## What we explicitly skip

- **Mass storage layout assertions** — too fragile across DAPLink firmware revisions.
- **Chrome widget UI testing** — Puppeteer + BLE chooser is broken at the platform level.
- **Multi-device parallel testing** — single-device rig for now; can multi-instance later.

## State machine: REPL vs BLE are mutually exclusive

Verified empirically when scaffolding this rig (2026-05-21):

```
            ┌─ A+B+Reset ───────────────────┐
            │                               ▼
       ┌─────────┐                    ┌─────────────┐
       │ NORMAL  │                    │  PAIRING    │
       │  MODE   │                    │   MODE      │
       │         │                    │             │
       │ REPL ✓  │                    │ BLE ✓       │
       │ BLE  ✗  │                    │ REPL ✗      │
       └─────────┘                    └─────────────┘
            ▲                               │
            └─── auto on flash success ─────┘
                 (after writing new program)
```

The test runner has to verify state-appropriately:
- After a successful flash, the device is in **normal mode** → REPL is the assertion path.
- Between flashes (when we need to push a new one), the device must be in **pairing mode** → BLE is the assertion path.

If the device is in the wrong mode for what we want to do, the runner prompts the user to A+B+Reset (or, future work, programmatically triggers it via the buttonless DFU characteristic when available).

## Empirical assumptions used

These were verified during this session, captured for next-time:

- USB drag-flash to `E:/` triggers bootloader programming + auto-reboot in ~3 s.
- DAPLink does **not** expose any button or reset commands over its CDC serial (`COM8`); the serial is REPL-only.
- The Q firmware (`MICROBIT_BLE_ENABLED=0` + open BLE config) only advertises BLE after **A+B+Reset → pairing mode**. After a fresh USB flash we observed pairing mode automatically — possibly due to leftover `flashIncomplete` flag, possibly DAPLink-specific; needs empirical re-verification at test rig setup.
- Codal partial-flashing service (`E97DD91D`) on MicroPython firmware exposes region 2 (slot index 2) as the filesystem (`0x6D000–0x73000`), HASH_NONE. This is why `parseMicroPythonHex` synthesises a non-zero `makeCodeHash` to force flashing.
- The MicroPython runtime hash record (id=2) is HASH_POINTER — must be CRC32-resolved client-side, as the widget now does in `a54f89d`.

## Open questions before first run

1. Does the test mini reliably enter pairing mode after USB drag-flash, or does it need A+B+Reset?
2. Does Chrome cache BLE permissions per origin survive Puppeteer's default ephemeral profile? (No — needs `--user-data-dir`.)
3. Is there a cleaner way to trigger A+B+Reset programmatically — e.g., setting `RebootMode` in flash storage via the partial-flashing protocol? Worth a 30 min experiment.

## Current rig status (2026-05-21 scaffolding)

What's implemented and verified working on hardware:

- `lib/usb_flash.py` — drag-flash to `E:/`, detect re-mount, settle (23 s end-to-end for a 1.2 MB hex; bulk of that is the 5 s post-flash safety wait).
- `lib/repl.py` — pyserial REPL probe on `COM8`. Confirms MicroPython version + can read `main.py` (one cosmetic parsing bug to fix — sentinel detection captures the echo'd statement, not the file content).
- `lib/ble_probe.py` — bleak-based scan + connect + classify session by service set. Returns `BleProbe(found, name, has_partial_flash, has_unbonded_dfu, …)`. Works.
- `build-test-hexes.mjs` — Node script that injects `main.py` into the bundled MicroPython hex via `@microbit/microbit-fs` (resolved from the python-editor's `node_modules` via `createRequire`). Produces `prog-A.hex`, `prog-A-mod.hex`, `prog-B-blocks.hex` in `test-hexes/`. Works.
- `runner.py` — orchestrator. Phase 1 (USB flash + verify) is autonomous end-to-end; phases 2 + 4 (BLE flash) use `manual_ble_flash()` stubs that prompt the user to drive the campus widget while we time the round-trip.

What's still **TODO** to make the rig fully autonomous:

- `lib/ble_dfu.py` — wrap `ble-test/07_full_probe.py`'s end-to-end DFU into a callable function. Estimated 1–2 h.
- `lib/ble_partial.py` — port `mini-connection-widget/src/ble-flash-web.ts` `BluetoothPartialFlashSession.run()` to Python. The wire protocol is small (~150 lines TS); the trickiest bit is the page-aligned MakeCode-marker / MicroPython-layout-table parser, which we already ported the algorithm for in `parseMicroPythonHex` (commit `a54f89d`). Estimated 4–6 h.
- Once both are in, the runner can drop `manual_ble_flash()` and run unattended.

What we observed that needs follow-up (out of scope for the rig itself):

- The `FIRMWARE/micropython-calliope-mini-v3/src/MINI.hex` on disk is **variant R** (BLE_ENABLED=1 + radio takeover guard) which we deferred during this session after seeing panic 071. When flashed via the test rig, **it booted with both REPL AND BLE alive simultaneously** — the lazy radio-takeover idea the user proposed may actually work, and earlier 071 panics may have been flash-state-pollution. Worth a clean re-test with a fresh power-cycled mini.

## How to run

```bash
# one-time: build the test hexes from current firmware/widget assets
cd c:/GIT/Calliope/calliope-flash-test/flash-test
node build-test-hexes.mjs

# run the 4-phase test (interactive; pauses at BLE phases)
python runner.py
```

For fully unattended runs, do the manual one-time browser pairing first (so `getDevices()` works), then implement the two TODO lib modules above.
