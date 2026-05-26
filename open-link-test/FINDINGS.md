# E2E test findings — open-link-test hexes

Hardware: Calliope mini v3 (DAPLink unique-id `9906…c6`, nRF52833,
hardware-identical to micro:bit V2).

Tooling:

- Python 3.12 + `bleak` for raw BLE probes
  ([`scripts/probe.py`](scripts/probe.py),
  [`scripts/probe_paired.py`](scripts/probe_paired.py))
- Chrome stable + `@calliope-edu/mini-connection-widget@01efeb7` (the
  same commit `calliope-campus` pins) for widget e2e via
  [`flash-test/widget-demo/`](../flash-test/widget-demo/)

Hex set under test: the **five mini-v3 hexes**.
`makecode-1-*` and `makecode-2-*` are not for this hardware (mini v1
and v2 respectively) and don't run on the mini v3 — they're listed in
[ANALYSIS.md](ANALYSIS.md) as background but skipped here.

## Top-line result — mini v3 hexes

| Hex | USB drag-flash | BLE app-mode | After A+B+Reset (pairing) | Widget connect | Open-link DFU char |
|---|---|---|---|---|---|
| `python_ble.hex` | ✓ 23 s | ✓ PFS + DFU + unbonded | (n/a — BLE already on) | ✓ via widget | `8EC90003` |
| `python_radio.hex` | ✓ 23 s | ✗ BLE off in app (SD dormant, radio peripheral free for MP) | ✓ DFU + unbonded | ✓ via widget | `8EC90003` |
| `blocks.hex` | ✓ 18 s | ✓ PFS + DFU + MbitMore + unbonded | (n/a) | ✓ via widget | `8EC90003` |
| `makecode-3-ble.hex` | ✓ 19 s | ✓ DFU + unbonded (no PFS in app) | (n/a — BLE already on) | full DFU only | `8EC90003` |
| `makecode-3-radio.hex` | ✓ 19 s | ✗ BLE off in app | ✓ DFU + unbonded | full DFU only | `8EC90003` |

**All five mini-v3 hexes are widget-compatible.** Three connect in app
mode without intervention; two need A+B+Reset to surface BLE. Every
reachable hex exposes the unbonded `8EC90003` characteristic and no
bonded `8EC90004` — open-link confirmed throughout.

## Detailed observations

### python_ble.hex (BLE-on default, MicroPython)

App mode advertises with the full service set + PFS regions filled
from the uPy layout table at 0x66FF0:

```
services:   PFS=True  DFU=True  MbitMore=False
buttonless: unbonded=True  bonded=False
region 0 (SD):   0x00001000-0x0001C000  hash=0000000000000000
region 1 (MP):   0x0001C000-0x000669A4  hash=1bee72ce00000000   ← CRC32 of "v2.1.2b…" string
region 2 (FS):   0x0006D000-0x00073000  hash=0000000000000000
```

Region 1 hash `1bee72ce` is the device-computed CRC32 of the version
string (`HASH_TYPE_POINTER` in the layout table). The widget compares
this against the hex's expected CRC to decide partial-flash vs full
DFU.

### python_radio.hex (radio-variant MicroPython)

App mode: **no BLE advertising** — SoftDevice in flash is dormant
because `MICROBIT_BLE_ENABLED=0`. The radio peripheral is free for
MicroPython's `radio` module.

Pairing mode (after A+B+Reset, hold for full 5×5 grid fill — codal
[MicroBit.cpp:228-264](../../LLM/FIRMWARE/codal-microbit-v2/model/MicroBit.cpp#L228-L264)
calls `bleManager.init(...,true,...)`):

```
services:   PFS=False (codal doesn't expose PFS in pairing-mode init)
            DFU=True
            MbitMore=False
buttonless: unbonded=True  bonded=False
```

The widget can full-DFU from pairing mode. After DFU completes the
mini reboots into whatever new firmware was flashed.

**Fix shipped** in `calliope-mini-python-editor@318d827` on
`campus-open`. See ANALYSIS for the build recipe (drop the patches
that set `DEVICE_BLE=0` and force the linker layout — keep
`DEVICE_BLE=1` + `MICROBIT_BLE_ENABLED=0` to preserve the pairing-mode
block while keeping the SoftDevice dormant in app mode).

### blocks.hex (codal blocks-runtime)

App mode:

```
services:   PFS=True  DFU=True  MbitMore=True
buttonless: unbonded=True
region 0: 0x00000000-0x00000000  hash=0000000000000000   (no PXT magic)
region 1: 0x00000000-0x00000000  hash=0000000000000000
region 2: 0x00000000-0x00000000  hash=0000000000000000
```

MbitMore service (`0b50f3e4-...`) is what scratch-calliope uses for
real-time block execution. Partial flashing reports all-zero ranges
(no PXT magic embedded in this bare-runtime build) → widget treats it
as "stub" and falls back to full DFU on any change.

Confirmed via widget (`results/widget-session.log`):

```
[info] BLE state: app-mode — partial-flash visible;
       services=[0b50f3e4, e97dd91d, 0000180a, 0000fe59]
```

### makecode-3-ble.hex (pxt-calliope BLE variant)

App mode (after the recent `optionalConfig.bluetooth.enabled=1`
update):

```
services:   PFS=False  DFU=True  MbitMore=False
buttonless: unbonded=True
```

DFU is available immediately in app mode — no A+B+Reset needed. PFS
is NOT exposed though; pxt-calliope's config doesn't enable
`MICROBIT_BLE_PARTIAL_FLASHING`. Net effect: widget always does full
BLE-DFU when re-flashing, never partial flash.

### makecode-3-radio.hex (pxt-calliope radio variant, updated)

App mode: no BLE advertising (radio peripheral free for user program).

Pairing mode (A+B+Reset, hold for full grid):

```
services:   PFS=False  DFU=True  MbitMore=False
buttonless: unbonded=True  bonded=False
```

Same fix pattern as `python_radio.hex` — keep BLE compiled in
(`DEVICE_BLE=1`), don't auto-start (`MICROBIT_BLE_ENABLED=0`), so
pairing-mode block stays compiled in and A+B+Reset still works.
**Fix shipped** in pxt-calliope (separate repo).

## Widget e2e — campus widget code path

The widget at HEAD `01efeb7` (matches calliope-campus pin) connected
successfully to both BLE-on firmwares in app mode and to the radio
variants in pairing mode. From [`results/widget-session.log`](results/widget-session.log):

```
[info] Connected (BLE)
[info] BLE state: app-mode — partial-flash visible;
       services=[0000180a, 0000fe59, e97dd91d]                   ← python_ble.hex
       services=[0b50f3e4, e97dd91d, 0000180a, 0000fe59]         ← blocks.hex
```

A full DFU round trip via the widget completed in 19.8 s (BLE-DFU,
1195 KB image). The widget's BLE reconnect daemon recovered from a
post-flash disconnect in ~43 s.

## Widget compatibility matrix

What the widget can do with each mini-v3 hex on the connected device:

| Hex | After USB flash, widget can: | Requires A+B+Reset? |
|---|---|---|
| `python_ble.hex` | Connect, partial-flash, full-DFU | No |
| `python_radio.hex` | Connect (after A+B+Reset), full-DFU | Yes |
| `blocks.hex` | Connect, full-DFU (no partial — by design) | No |
| `makecode-3-ble.hex` | Connect, full-DFU (no partial — pxt-calliope config) | No |
| `makecode-3-radio.hex` | Connect (after A+B+Reset), full-DFU | Yes |

Partial flashing only ever works on `python_ble.hex` out of the box.
For partial-flash on `blocks.hex`, the scratch-calliope editor needs
to append PXT magic when bundling user blocks. For `makecode-3-ble`,
pxt-calliope would need to enable `MICROBIT_BLE_PARTIAL_FLASHING`.

## How to reproduce

Plug a Calliope mini v3 in (DAPLink mounts as `E:`), then from
`open-link-test/`:

```
# Static analysis on all 9 hexes
python scripts/analyze_all.py

# App-mode probe loop on all 9 (skips makecode-1/2 if device is mini v3,
# they just hang at the BLE-not-found step which is the expected
# "wrong hardware" result)
python scripts/probe.py

# Pairing-mode probe for the BLE-off-in-app variants
python scripts/probe_paired.py flash python_radio.hex
# A+B+Reset on the mini, hold for full grid fill
python scripts/probe_paired.py probe python_radio_pairing

# Widget e2e (the widget at HEAD = campus pin)
cd ../flash-test/widget-demo
pnpm install                    # one-time
pnpm dev                        # in one terminal
node puppeteer-driver.mjs --park   # in another; click connect in Chrome
```

## Files produced

- [results/probe-results.json](results/probe-results.json) — full bleak
  probe output per hex
- [results/static-analysis.json](results/static-analysis.json) — full
  static analysis output per hex
- [results/widget-session.log](results/widget-session.log) — Puppeteer
  capture of the widget connecting to BLE-reachable hexes

## Cross-links

- [ANALYSIS.md](ANALYSIS.md) — static analysis (offsets, magic, layout)
- [Editors.md](Editors.md) — URLs to the editors that produced these
  hexes
- Widget commit validated: `01efeb7` (matches calliope-campus
  `feature/native-proxy` pin)
