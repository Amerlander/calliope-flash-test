# open-link-test

Validation of hex files from the three Calliope campus editors, focused
on the **open-link** (unbonded) BLE / DFU configuration on a Calliope
mini v3.

## Filename convention

The `N` in `makecode-N-{ble,radio}.hex` is the **hardware version**
the hex targets:

- `makecode-1-*` → Calliope mini v1 (legacy hardware)
- `makecode-2-*` → Calliope mini v2 (legacy hardware)
- `makecode-3-*` → Calliope mini v3 (nRF52833, hardware-identical to
  micro:bit V2)

Only the **mini v3 hexes** (`blocks.hex`, `makecode-3-*`,
`python_*`) are exercised on the rig.

## Source editors

| File | Editor | Source URL ([Editors.md](Editors.md)) |
|---|---|---|
| `blocks.hex` | blocks-runtime (scratch-calliope) | `iframe-test7.scratch-calliope.pages.dev` |
| `makecode-3-{ble,radio}.hex` | pxt-calliope | local serve from `LLM/MAKECODE/pxt-calliope` |
| `python_{ble,radio}.hex` | calliope-mini-python-editor | `campus-open.calliope-mini-python-editor.pages.dev` |

## Folder layout

```
open-link-test/
├── README.md              ← this file
├── ANALYSIS.md/.html      ← static hex analysis (offsets, magic, layout)
├── FINDINGS.md/.html      ← live e2e test findings
├── Editors.md             ← URLs of the editors that produced these hexes
├── *.hex                  ← the 9 hex files
├── scripts/
│   ├── analyze_all.py         (top-level static analyzer — generates JSON)
│   ├── _analyze.py            (offsets, sections, sentinels — internal helpers)
│   ├── _analyze3.py           (vector tables, UICR, layout magic — drill-down)
│   ├── _analyze5.py           (PXT vs uPy magic scan)
│   ├── probe.py               (USB-flash + bleak BLE probe loop)
│   ├── probe_paired.py        (single-hex flash + manual A+B+Reset + probe)
│   └── widget-snapshot.mjs    (puppeteer snapshot of running widget-demo)
└── results/
    ├── probe-results.json     ← bleak probe data
    ├── static-analysis.json   ← static analyzer JSON output
    └── widget-session.log     ← widget e2e log
```

## What we validated (mini v3)

[FINDINGS.md](FINDINGS.md) has the full report. Headline:

- All 5 mini-v3 hexes USB-drag-flash cleanly (18–23 s each).
- Every BLE-reachable mini-v3 firmware exposes the **unbonded**
  `8EC90003` characteristic — open-link confirmed throughout.
- 3 of 5 connect in app mode without intervention:
  `python_ble`, `blocks`, `makecode-3-ble`.
- 2 of 5 need A+B+Reset to surface BLE (radio variants), which works.
- `python_ble.hex` is the only hex that the widget can partial-flash
  out of the box (valid uPy layout table). Others fall back to full
  DFU on any change.
- The widget at HEAD `01efeb7` (= calliope-campus pin) connects
  successfully to every reachable mini-v3 hex.

## Quick start

```
# 1. Plug in a Calliope mini v3 (DAPLink mounts as E:)

# 2. Static analysis on all 9 hexes
python scripts/analyze_all.py

# 3. App-mode probe across all 9
python scripts/probe.py

# 4. Pairing-mode probe of a single hex
python scripts/probe_paired.py flash python_radio.hex
#    A+B+Reset the mini, hold for full grid fill
python scripts/probe_paired.py probe python_radio_pairing

# 5. Widget-demo e2e (widget at HEAD = campus pin)
cd ../flash-test/widget-demo
pnpm install      # one-time
pnpm dev          # in one terminal
node puppeteer-driver.mjs --park   # in another; click connect in Chrome
```

## Status

| Component | State |
|---|---|
| Python radio variant fix | Shipped (`calliope-mini-python-editor@318d827`) |
| MakeCode radio variant fix | Shipped (pxt-calliope) — both `makecode-3-{ble,radio}` rebuilt with new BLE/pairing config |
| Widget pin | `01efeb7` matches calliope-campus `feature/native-proxy` |
| `blocks.hex` partial-flash magic | Intentionally absent — scratch-calliope uses MbitMore at runtime instead of re-flashing |
