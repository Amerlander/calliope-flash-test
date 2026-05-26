# open-link-test

Validation of hex files from the three Calliope campus editors, against
all three Calliope mini hardware versions.

## Filename convention

The `N` in `makecode-N-{ble,radio}.hex` is the **hardware version**
the hex targets — `1` for mini v1 (nRF51), `2` for mini v2 (nRF52833
with J-Link OB), `3` for mini v3 (nRF52833 with DAPLink,
hardware-identical to micro:bit V2).

## Source editors

| File | Editor | Source URL ([Editors.md](Editors.md)) |
|---|---|---|
| `blocks.hex` | blocks-runtime (scratch-calliope) | `iframe-test7.scratch-calliope.pages.dev` (mini v3 only) |
| `makecode-{1,2,3}-{ble,radio}.hex` | pxt-calliope | local serve from `LLM/MAKECODE/pxt-calliope` |
| `python_{ble,radio}.hex` | calliope-mini-python-editor | `campus-open.calliope-mini-python-editor.pages.dev` (mini v3 only) |

## Folder layout

```
open-link-test/
├── README.md              ← this file
├── ANALYSIS.md/.html      ← static hex analysis (offsets, magic, hashes)
├── FINDINGS.md/.html      ← live e2e test findings
├── Editors.md             ← URLs of the editors
├── *.hex                  ← the 9 hex files
├── scripts/
│   ├── analyze_all.py         (top-level static analyzer → JSON)
│   ├── _analyze*.py           (internal helpers)
│   ├── probe.py               (USB-flash + bleak BLE probe loop)
│   ├── probe_paired.py        (single-hex flash + A+B+Reset + probe)
│   └── widget-snapshot.mjs    (puppeteer snapshot of widget-demo)
└── results/
    ├── probe-results.json     ← bleak probe data
    ├── static-analysis.json   ← static analyzer JSON
    └── widget-session.log     ← widget e2e log (mini v3)
```

## What we validated

[FINDINGS.md](FINDINGS.md) has the full report. Headline:

- **All 9 hexes USB drag-flash cleanly** on their target hardware.
- **All 6 MakeCode hexes** expose PFS over BLE with hashes matching
  the static hex analysis — after the 2026-05-26 pxt-calliope rebuild
  with `partial_flashing=1` in pxt.json.
- **Mini v3 only** has Nordic Secure DFU on board — its 5 hexes all
  expose the unbonded `8EC90003` characteristic (open-link confirmed).
- **Mini v1 and v2 have no Nordic DFU service** at all (their codal
  stack predates Nordic Secure DFU). Partial flash via PFS is the only
  BLE-flash route for these older minis.
- The campus widget at HEAD `01efeb7` connects to every mini-v3 hex
  successfully.

## Widget capability — quick reference

| Hardware | Partial flash | Full DFU |
|---|---|---|
| Mini v3 | ✓ for `python_ble` and `makecode-3-*` (with PFS + DFU). Falls back to DFU for `blocks` (bare runtime, no magic). | ✓ for all 5 mini-v3 hexes |
| Mini v2 | ✓ for `makecode-2-{ble,radio}` (PFS only) | ✗ no Nordic DFU |
| Mini v1 | ✓ for `makecode-1-{ble,radio}` (PFS only, after A+B+Reset) | ✗ no Nordic DFU |

## Quick start

```bash
# Plug in a Calliope mini (drive letter varies — E: for mini v3 DAPLink,
# D: for mini v2 J-Link / mini v1 legacy DAPLink).

# Static analysis on all 9 hexes
python scripts/analyze_all.py

# App-mode probe loop (default drive E:; for D: patch probe.MINI_DRIVE)
python scripts/probe.py

# Pairing-mode probe of a single hex
python scripts/probe_paired.py flash <hex>
#   A+B+Reset on the mini, hold for full grid fill
python scripts/probe_paired.py probe <key>

# Widget e2e (mini v3 only)
cd ../flash-test/widget-demo
pnpm install      # one-time
pnpm dev          # one terminal
node puppeteer-driver.mjs --park   # other terminal; click connect in Chrome
```

## Status

| Component | State |
|---|---|
| Python radio variant (mini v3) | Shipped: `calliope-mini-python-editor@318d827` |
| pxt-calliope `partial_flashing=1` | Shipped: all 6 makecode hexes rebuilt 2026-05-26 |
| Widget pin | `75ff901` (campus `feature/native-proxy@d84b069`) |
| `blocks.hex` partial-flash magic | Intentionally absent — scratch-calliope uses MbitMore at runtime |
| Mini v1 / v2 BLE-side full DFU | Not available — widget must fall back to USB for full reflashes on legacy hardware |
