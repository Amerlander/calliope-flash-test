# E2E test findings — open-link-test hexes

Tested 2026-05-26 against all three Calliope mini hardware versions.

Tooling:

- Python 3.12 + `bleak` for raw BLE probes
  ([`scripts/probe.py`](scripts/probe.py),
  [`scripts/probe_paired.py`](scripts/probe_paired.py))
- Chrome stable + `@calliope-edu/mini-connection-widget@01efeb7` (the
  same commit `calliope-campus` pins) for widget e2e via
  [`flash-test/widget-demo/`](../flash-test/widget-demo/)

## Filename convention

The `N` in `makecode-N-{ble,radio}.hex` is the **Calliope mini
hardware version** the hex targets — `1` for mini v1 (nRF51-based), `2`
for mini v2 (nRF52833 with J-Link OB), `3` for mini v3 (nRF52833 with
DAPLink, hardware-identical to micro:bit V2).

## Top-line result — by hardware

### Mini v3 (DAPLink)

| Hex | USB | BLE app-mode | After A+B+Reset | Open-link DFU | Widget can |
|---|---|---|---|---|---|
| `python_ble.hex` | ✓ 23 s | ✓ PFS + DFU + unbonded | (BLE already on) | `8EC90003` ✓ | partial + full DFU |
| `python_radio.hex` | ✓ 23 s | ✗ SD dormant | ✓ DFU + unbonded | `8EC90003` ✓ | full DFU |
| `blocks.hex` | ✓ 18 s | ✓ PFS + DFU + MbitMore + unbonded | (BLE already on) | `8EC90003` ✓ | full DFU (no partial — bare runtime) |
| `makecode-3-ble.hex` | ✓ 19 s | ✓ PFS + DFU + unbonded | (BLE already on) | `8EC90003` ✓ | partial + full DFU |
| `makecode-3-radio.hex` | ✓ 19 s | ✗ SD dormant | ✓ PFS + DFU + unbonded | `8EC90003` ✓ | partial + full DFU (after A+B+Reset) |

**All five mini-v3 hexes are widget-compatible.** Every BLE-reachable
hex exposes the unbonded `8EC90003` characteristic — open-link
confirmed throughout. With `partial_flashing=1` shipped in pxt-calliope
(2026-05-26), both `makecode-3-*` hexes now expose PFS in addition to
DFU.

### Mini v2 (J-Link OB)

| Hex | USB | BLE app-mode | After A+B+Reset | Open-link DFU |
|---|---|---|---|---|
| `makecode-2-ble.hex` | ✓ 25 s | ✓ PFS only — **no DFU** | (BLE already on) | – (no Nordic DFU service) |
| `makecode-2-radio.hex` | ✓ 25 s | ✗ | ✓ PFS only — **no DFU** | – |

Mini v2 (nRF51-era stack) has the codal PartialFlashing service
working with PXT magic, but **no Nordic Secure DFU service** at all.
The widget's full-DFU path therefore does not apply; partial-flash is
the only BLE-flash route. Bootloader BLE only exposes
`MicroBit Flash Service` (the original Lancaster one), not the modern
Nordic Secure DFU.

### Mini v1 (legacy DAPLink)

| Hex | USB | BLE app-mode | After A+B+Reset | Open-link DFU |
|---|---|---|---|---|
| `makecode-1-ble.hex` | ✓ 17 s | ✗ | ✓ PFS only — **no DFU** | – |
| `makecode-1-radio.hex` | ✓ 18 s | ✗ | ✓ PFS only — **no DFU** | – |

Same pattern as mini v2: PFS works, no Nordic DFU. Mini v1 + v2 share
the same older codal/Lancaster stack on nRF51/nRF52 — neither will
work via the widget's Nordic Secure DFU path.

## Region readbacks — verified hashes

All probed devices reported region hashes matching the static hex
analysis byte-for-byte. The codal PartialFlashing service correctly
exposes the PXT magic + hashes the editor compiled in.

### Mini v3 (nRF52833 / codal v2)

```
makecode-3-ble.hex  (app mode):
  region 0 (SD):  0x00000000-0x0001C000  hash=0000000000000000
  region 1 (DAL): 0x0001C000-0x00045ED4  hash=103229cde9d66cc1 ✓
  region 2 (MC):  0x00046000-0x00073000  hash=6c00b6000e00eb00 ✓

makecode-3-radio.hex  (pairing mode after A+B+Reset):
  region 0 (SD):  0x00000000-0x0001C000  hash=0000000000000000
  region 1 (DAL): 0x0001C000-0x000462D4  hash=3d11446ded28180d ✓
  region 2 (MC):  0x00047000-0x00073000  hash=91004f004000ab00 ✓
```

App at 0x1C000, MakeCode user code at ~0x46000, FS scratch at 0x77000.

### Mini v1 and v2 (nRF51/older codal)

```
makecode-1-ble.hex  (mini v1, pairing mode):
  region 0 (SD):  0x00000000-0x00018000  hash=0000000000000000
  region 1 (DAL): 0x00018000-0x00035708  hash=37b90dbcf1a3f6e0 ✓
  region 2 (MC):  0x00035C00-0x0003BBFF  hash=9c0025006300fa00 ✓

makecode-1-radio.hex  (mini v1, pairing mode):
  region 0 (SD):  0x00000000-0x00018000  hash=0000000000000000
  region 1 (DAL): 0x00018000-0x00035D48  hash=d7fece9c392f3068 ✓
  region 2 (MC):  0x00036000-0x0003BBFF  hash=5e00f500b000c800 ✓

makecode-2-ble.hex  (mini v2, app mode — BLE on by default):
  region 0 (SD):  0x00000000-0x00018000  hash=0000000000000000
  region 1 (DAL): 0x00018000-0x00035748  hash=0c03d3de260b20e2 ✓
  region 2 (MC):  0x00035C00-0x0003BBFF  hash=fb00520030002d00 ✓

makecode-2-radio.hex  (mini v2, pairing mode):
  region 0 (SD):  0x00000000-0x00018000  hash=0000000000000000
  region 1 (DAL): 0x00018000-0x00035D48  hash=2ba4dda00c362520 ✓
  region 2 (MC):  0x00036000-0x0003BBFF  hash=70003b00d300ad00 ✓
```

The smaller offsets (SD ends at 0x18000 instead of 0x1C000) reflect
the legacy Nordic SoftDevice (S110/S130) used on nRF51 / older codal
builds. Total flash ~250 KB vs ~512 KB on v3.

## What the widget can do, per hardware

| Hardware | Partial flash via widget | Full DFU via widget |
|---|---|---|
| Mini v3 | ✓ for `python_ble`, `makecode-3-*` (with PFS+DFU). Falls back to DFU for `blocks` (no magic). | ✓ for all 5 mini-v3 hexes |
| Mini v2 | ✓ for `makecode-2-{ble,radio}` (with PFS) | ✗ no Nordic DFU on mini v2 |
| Mini v1 | ✓ for `makecode-1-{ble,radio}` (with PFS, after A+B+Reset) | ✗ no Nordic DFU on mini v1 |

For mini v1/v2 the widget's flash path will need to fall back to its
USB drag-flash route — BLE-side full DFU isn't available because the
Nordic Secure DFU service isn't part of the older Calliope stack.

## Notable observation: BLE-on-in-app differs by hardware

`makecode-2-ble.hex` (mini v2) advertises BLE in **app mode** without
A+B+Reset. `makecode-1-ble.hex` (mini v1) and `makecode-3-ble.hex`
historically defaulted to BLE off in app mode and required A+B+Reset.
After the user added `partial_flashing=1` to pxt.json and rebuilt,
`makecode-3-ble.hex` also became BLE-on by default. Mini v1 stayed
BLE-off-in-app for the BLE variant — the per-target config in
pxt-calliope appears not to enable BLE auto-start for the v1 build.

## Widget e2e — campus widget code path

The widget at HEAD `01efeb7` (matches calliope-campus pin) connected
successfully to:

- `python_ble.hex` (mini v3, app mode)
- `blocks.hex` (mini v3, app mode)

Captured in [`results/widget-session.log`](results/widget-session.log).
One full BLE-DFU round trip (1195 KB) completed in 19.8 s via the
widget; reconnect daemon recovered from the post-flash disconnect in
~43 s.

## How to reproduce

Plug in the relevant Calliope mini (DAPLink at E:, J-Link or legacy
DAPLink at D:), then:

```bash
cd open-link-test

# Static analysis on all hexes
python scripts/analyze_all.py

# App-mode probe loop (defaults to E: — adapt for other minis):
#   import probe, usb_flash; probe.MINI_DRIVE = 'D:/'; usb_flash.MINI_DRIVE = 'D:/'
python scripts/probe.py

# Pairing-mode probe of a single hex
python scripts/probe_paired.py flash <hex>
# A+B+Reset on the mini, hold for full grid fill
python scripts/probe_paired.py probe <key>
```

For widget e2e (mini v3 only — older minis have no Nordic DFU):

```bash
cd ../flash-test/widget-demo
pnpm install              # one-time
pnpm dev                  # in one terminal
node puppeteer-driver.mjs --park   # in another; click connect
```

## Cross-links

- [ANALYSIS.md](ANALYSIS.md) — static hex analysis (offsets, magic,
  layout)
- [Editors.md](Editors.md) — URLs of the editors that produced these
  hexes
- [results/probe-results.json](results/probe-results.json) — full
  bleak probe output per hex
- [results/static-analysis.json](results/static-analysis.json) — full
  static analysis output per hex
- [results/widget-session.log](results/widget-session.log) — Puppeteer
  capture of the widget connecting to mini-v3 hexes
