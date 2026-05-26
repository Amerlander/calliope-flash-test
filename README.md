# calliope-flash-test

Test rigs and findings from the Calliope mini 3 BLE-flashing speedup work
(2026-05). Two related subprojects:

- **[ble-test/](ble-test/)** — low-level Python+bleak BLE exploration:
  service inspection, MTU/PRN probing, DFU flow experiments,
  single-packet-size sweeps, speed measurement. Used to characterise the
  Nordic Secure DFU protocol's behaviour against the Calliope's
  v2-bootloader and to find the speed ceiling for unbonded BLE on
  Windows. See [`ble-test/BEST_PRACTICES.md`](ble-test/BEST_PRACTICES.md)
  for the consolidated DFU recipe.

- **[flash-test/](flash-test/)** — end-to-end 4-phase test rig:
  USB drag-flash + BLE-DFU + BLE partial-flash + USB recovery, all
  driven from one [`runner.py`](flash-test/runner.py). Includes a
  Puppeteer-driven [`widget-demo/`](flash-test/widget-demo) that
  exercises the same protocols through the production
  [`mini-connection-widget`](https://github.com/calliope-edu/mini-connection-widget)
  for a side-by-side comparison. See
  [`flash-test/CONCLUSIONS.md`](flash-test/CONCLUSIONS.md) and
  [`flash-test/DFU-SPEEDUP.md`](flash-test/DFU-SPEEDUP.md) for the
  consolidated results.

## Headline results

| flash type | before this work | after | improvement |
|---|---|---|---|
| Full Nordic Secure DFU, 146 KB | 87–116 s | **24 s** | ~**4×** |
| Partial flash, 24 KB FS region | 66 s (Python) / 29 s (widget) | 29 s (widget) | unchanged — Chrome was already at the ceiling |
| USB drag-flash, 1.2 MB | 23 s | 23 s | unchanged |

The DFU speedup came from three changes shipped together as the
[`v0.3.5-campus-open-1`](https://github.com/calliope-edu/codal-microbit-v2/releases/tag/v0.3.5-campus-open-1)
codal-microbit-v2 tag + matching widget patches:

1. **Bootloader** — same BD_ADDR as the app (removed Nordic SDK's
   `addr.addr[0] += 1` for unbonded mode), so Web Bluetooth can
   reconnect to the bootloader after a buttonless DFU reboot without
   re-pairing.
2. **Bootloader** — MTU exchange handler enabled in `nrf_dfu_ble.c`
   (already in `sdk_config.h`, just needed a rebuild — the deployed
   `lib/bootloader.o` was a stale pre-handler build). Negotiates ATT
   MTU 247, accepts 244-byte payloads on the Packet characteristic.
3. **Widget** — `PRN_INTERVAL=12` (was 6, safe with the bootloader's
   17 RX buffers), `PACKET_PAYLOAD_STEPS = [244, 64, 20]` (adaptive
   ladder with chunk-1 step-down for older bootloaders), and a
   silent-swallow bug fix in `flashCalliopeViaBle` so transient BLE
   errors during partial-flash entry actually trigger the DFU fallback.

## Why it's useful as a public reference

The diagnostic path went through a lot of dead ends and codal/SoftDevice
internals that aren't well-documented elsewhere:

- Why Python+bleak measures BLE as 2× slower than Chrome on the same
  Windows machine (it never triggers MTU exchange).
- How the codal partial-flash service exposes regions, what its
  zero-range answer means, how to detect a "stub-but-no-layout-table"
  device (e.g. anything not run through `addlayouttable.py`).
- Why the campus-open MicroPython firmware needs A+B+Reset NOT to be
  required between flashes, and what that costs vs the radio variant.
- How to ship two MicroPython variants (BLE-on default, radio-on for
  programs with `import radio`) so the DAL hash check auto-switches via
  full DFU on variant change.

The combined writeup lives in [`flash-test/CONCLUSIONS.md`](flash-test/CONCLUSIONS.md)
+ [`flash-test/DFU-SPEEDUP.md`](flash-test/DFU-SPEEDUP.md). The
exploratory work is in [`ble-test/FINDINGS.md`](ble-test/FINDINGS.md)
and [`ble-test/BEST_PRACTICES.md`](ble-test/BEST_PRACTICES.md).

## Hardware + OS

All measurements were taken on:

- **Calliope mini 3** (nRF52833, hardware-identical to micro:bit v2)
- **Windows 11** + **Chrome stable** for Web Bluetooth measurements
- **WSL2 Ubuntu** for builds (the `/mnt/c/...` 9P bridge is ~20× slower
  for many-small-file workloads; use a WSL-native checkout)

Hex files in `flash-test/test-hexes/` are checked in (~7 MB total) so
the rig is runnable out of the box. They can be rebuilt with
`node flash-test/build-test-hexes.mjs` if you change the source
firmware or the bundled `main.py`.

## Run quickly

```
# Python deps (one-time)
pip install bleak pyserial

# Plug in a Calliope mini 3 via USB. The rig assumes E:/ is the
# DAPLink mass-storage drive and COM8 is the CDC serial — edit
# flash-test/lib/usb_flash.py and flash-test/lib/repl.py to override.

# Full 4-phase rig (USB → DFU → USB → partial flash)
cd flash-test
python runner.py

# Standalone BLE inspection
cd ../ble-test
python 02_inspect.py
python 03_mtu_probe.py
# ...etc.
```

## Related repositories

This work shipped across several repos in the
[calliope-edu](https://github.com/calliope-edu) org:

- **[codal-microbit-v2](https://github.com/calliope-edu/codal-microbit-v2)**
  branch `v0.3.5-calliope-2-campus-open`, tag
  `v0.3.5-campus-open-1` — the patched bootloader.o.
- **[micropython-calliope-mini-v3](https://github.com/calliope-edu/micropython-calliope-mini-v3)**
  branch `campus-open` — MicroPython firmware (BLE variant default,
  radio variant via the build recipe in
  [`README-radio-variant.md`](https://github.com/calliope-edu/calliope-mini-python-editor/blob/campus-open/src/micropython/main/README-radio-variant.md)).
- **[blocks-runtime](https://github.com/calliope-edu/blocks-runtime)** —
  direct codal/CMake build of the Calliope Blocks editor's on-device
  runtime. Replaces the old pxt-microbit-V5 → pxt-blocks pipeline; one
  codal tag across MP + Blocks.
- **[mini-connection-widget](https://github.com/calliope-edu/mini-connection-widget)**
  branch `campus-open` — the Web Bluetooth widget with the DFU
  speedup patches.
- **[calliope-campus](https://github.com/calliope-edu/calliope-campus)**
  branches `rc09` (pinned, frozen) and `rc10` (auto-tracks editor
  deployments).

## License

MIT — see [LICENSE](LICENSE). The hex artifacts under
`flash-test/test-hexes/` and `ble-test/hexes/` are derivative works of
the upstream firmware repos and are redistributed under their
respective licenses (MIT in all cases).
