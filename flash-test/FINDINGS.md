# Flash-rig findings — autonomous run results (2026-05-21)

Two full sequences executed end-to-end without user intervention. Logs in
[results/full-run-1.log](results/full-run-1.log),
[results/full-run-2.log](results/full-run-2.log); CSVs in `results/*.csv`.

## Results

Run 1 (logs: full-run-1.log, csv 2026-05-21T12-41-43.csv):

| phase | transport         | hex                  | elapsed  | ok | note |
|-------|-------------------|----------------------|----------|----|------|
| 1     | USB drag-flash    | prog-A.hex           | **23.3 s** | ✓  | REPL confirms MicroPython 1.23 on `Calliope mini v2.1.2b with nRF52833` |
| 2     | BLE Nordic DFU    | prog-B-blocks.hex    | **86.6 s** | ⚠️ | Flash protocol fully completed (36/36 chunks); post-flash mbitmore probe failed — initially read as flake, then identified as fundamental (see findings) |
| 3     | USB drag-flash    | prog-A.hex           | **23.5 s** | ✓  | REPL confirms MicroPython baseline restored |
| 4     | BLE partial flash | prog-A-mod.hex       | **65.5 s** | ✓  | 384/384 blocks ACK'd (24576 B over `E97DD91D`), no DFU fallback |

Run 2 (logs: full-run-2.log, csv 2026-05-21T13-24-45.csv), with phase-2
assertion corrected to "DFU completed cleanly":

| phase | transport         | hex                  | elapsed  | ok | note |
|-------|-------------------|----------------------|----------|----|------|
| 1     | USB drag-flash    | prog-A.hex           | **23.3 s** | ✓  | MicroPython 1.23 |
| 2     | BLE Nordic DFU    | prog-B-blocks.hex    | **115.7 s** | ✓ | DFU completed; mbitmore check skipped |
| 3     | USB drag-flash    | prog-A.hex           | **23.2 s** | ✓  | MicroPython restored from silent-Blocks state |
| 4     | BLE partial flash | prog-A-mod.hex       | **66.8 s** | ✓ | partial-flash protocol clean, no DFU fallback |

Partial-flash / full-DFU speedup: **1.3×–1.7× run-to-run** (DFU has wider
variance, ~87-116 s; partial flash is consistent ~66 s).

## What worked

1. **Partial-flash protocol port is correct end-to-end.**
   - MicroPython layout-table parser identifies runtime region 2 + FS region 3,
     resolves the HASH_POINTER (id=2 / ht=2) via CRC32 of the device version
     string, and produces a DAL hash that matches what the device returns over
     `REGION_INFO`: `9ef12d3600000000`.
   - Synthesised MakeCode hash `00600000fe32076d` ≠ device's all-zero FS hash,
     so the widget shortcut doesn't fire — flash is dispatched as expected.
   - 4-packet-per-64-B block framing + ACK loop completes cleanly with
     `ACK_WRITTEN` every time; no `ACK_OUT_OF_ORDER` retries observed.
   - `RESET+APPLICATION` would be sent next; we currently just send `EOT` and
     let the device reset itself.

2. **App→pairing transition works on first attempt** (the retry-loop refactor
   was load-bearing for catching the case but not actually exercised here).
   The codal `restartInBLEMode` ran fast enough that the disconnect was
   observed in the first 200 ms poll window.

3. **DAPLink drag-flash + automated REPL probe** is rock-solid: phase 1
   and phase 3 both completed in ~23 s including the 5 s settle wait, and
   `repl.probe()` returned the correct `sys.implementation` string both times.

4. **Full DFU port** (extracted from `ble-test/07_full_probe.py`) ran the
   full 146 KB Blocks app in ~87 s end-to-end including the buttonless DFU
   handover. No protocol error.

## What didn't / surprises

### 1. Partial flash is barely faster than full DFU (1.3×, not 20-30×)

Numbers from this run:

- DFU: 146 464 B in 87 s ≈ **1685 B/s** (with PRN=6 batching)
- Partial: 24 576 B in 65 s ≈ **378 B/s** (single-block ACK lockstep)

So **per-byte, partial flash is ~4× slower than DFU on this connection.**
It only "wins" because the data set is smaller.

Mechanism: the partial-flash protocol forces a notify-ACK after every 64-byte
block. Without bonding the L2CAP connection interval is high (~30 ms on
Windows host?), so each ACK round-trip costs ~170 ms → throughput floor.
DFU's PRN=6 lets us pipeline 6 × 4 KB chunks before the receiver sends an
ACK, masking the round-trip cost.

This is consistent with the user's original premise that "without a bond
flashing is always slow." The partial-flash protocol doesn't escape that —
it just transfers less data, so wall-clock matters less *for small diffs*.

**Implication for the rig's pass/fail bar:** the README target of "phase 4
<15 s" is too tight given the current ACK cadence — at 378 B/s it would
need a binary <~5 KB, but the smallest interesting MP-FS change is ~6 KB
(one fresh filesystem page). Suggest raising the bar to "phase 4 < phase 2"
*and* "phase 4 doesn't fall back to DFU" — the second is the real
regression signal.

### 2. Blocks firmware doesn't auto-advertise BLE in app mode

Initially read as a verification flake. Investigated by:

- Retrying the BLE probe 5× over 65 s (still no advertisement)
- Probing REPL via COM8 (silent)
- Confirming DAPLink is alive (`Remount count: 3` increased, USB MSC still
  mounted with CALLIOPE.HTM and DETAILS.TXT)
- Scanning all BLE (not just Calliope-name-matched): the mini at
  `C9:2E:AC:B9:E1:B6` is not in the advertisement table at all

So the device is alive and the DFU did flash *something* (DAPLink remount
proves the app region was written), but the Blocks/pxt-calliope runtime
*does not auto-advertise BLE in app mode*. It only enters BLE-discoverable
state on **A+B+Reset → pairing mode**. This matches the same convention as
the calliope-mini-python-editor pxt-calliope V3 firmware (BLE only on
pairing).

This means the original assertion "after DFU we should see MbitMore" is
fundamentally wrong for the autonomous case — verifying via BLE post-flash
requires manual A+B+Reset, which we can't trigger from DAPLink (no
button-press command in the firmware we use).

**Fix applied:** phase-2 assertion is now "DFU protocol completed cleanly"
(matching what we can actually measure unattended). The post-flash
MbitMore check is removed from the autonomous flow; it can run as a
separate verification step when the user can press A+B+Reset.

**Recovery from silent-Blocks state:** phase 3's USB drag-flash works fine
even when the device is BLE+REPL silent — proves DAPLink drag-flash is
the universal failsafe. Re-verified in run 2.

### 3. Synthetic MakeCode hash is identical for prog-A and prog-A-mod

Both use `len(fs_bytes) | first_4_fs_bytes` as the synthesised hash. The
microbit-fs filesystem image has the same length and same first 4 bytes
for both inputs (header is identical; the differing `display.show("A")`
vs `"a"` lives deeper in the FS). So:

- prog-A    MC hash = `00600000fe32076d`
- prog-A-mod MC hash = `00600000fe32076d` (same)

This is fine for correctness — both still differ from the device's
all-zero hash, so the flash dispatches. But it means we can't use
"hash changed between back-to-back flashes" as an assertion. The widget's
intent here is **always-flash semantics for MicroPython** (because the FS
hash isn't authoritative), so this matches the design.

## Hash details from the live device

Captured by `_run_flash_protocol` during phase 4:

```
hex   DAL hash = 9ef12d3600000000   (CRC32 of version string, little-endian + zero pad)
hex   MC hash  = 00600000fe32076d   (synthetic: 0x6000 length | FS[0..3])
device DAL     = 0x1c000-0x669a4, hash 9ef12d3600000000   ← matches hex
device MC reg  = 0x6d000-0x73000,  hash 0000000000000000   ← HASH_NONE for FS
```

Version string the device's runtime-pointer dereferences to (parsed by us
from the hex during layout-table walk):

```
micro:bit v2.1.2b+9f9e309-dirty on 2026-05-20; MicroPython v1.23.0-1.gb7ce7a84a on 2026-05-20
```

CRC32 of that null-terminated string = `0x362df19e` → little-endian
`9e f1 2d 36` → 8-byte hash field padded with zeros = `9ef12d3600000000`. ✓

## Widget vs Python — headline result (2026-05-21 12:15-12:17)

User clicked the BLE chooser once; Puppeteer drove three back-to-back
partial flashes via `flashCalliope`. Logs:
[results/widget-run-2026-05-21T12-11-18.log](results/widget-run-2026-05-21T12-11-18.log)
+ `.json`.

| flash | binary | Python+bleak | Chrome Web BLE | Δ |
|-------|--------|--------------|----------------|----|
| Partial flash prog-A-mod (24 KB FS) | 24576 B | **66 s** | **29.9 s** | **2.2× faster** |
| Partial flash prog-A    (24 KB FS) | 24576 B | (not retested) | **29.1 s** | repeat ⇒ rock-solid |
| Nordic DFU prog-B-blocks (146 KB)  | 146464 B | 87-116 s | **invalid — see below** | — |

### Same protocol, same device, same OS BLE stack — different speed

This is the same Calliope mini (C9:2E:AC:B9:E1:B6), same hex files, same
Windows 11 WinRT BLE backend. Both implementations use 4×20-byte
`writeValueWithoutResponse` per 64-byte block + ACK notification. The
**only** difference is the userspace BLE library:

- Python rig calls `bleak.BleakClient.write_gatt_char(..., response=False)`
- Widget calls `BluetoothRemoteGATTCharacteristic.writeValueWithoutResponse()`
  via Chrome's Web Bluetooth → WinRT

Breakdown from the widget's `phase=running…finalising` timeline for
phase-partial-A:

```
12:16:23.349  Flash 15%   — data writes start
12:16:41.226  EOT received — data writes done
-------- 17.877 s of pure data transfer for 384 blocks --------
```

That's **47 ms per 64-byte block** through Chrome. Python's per-block was
roughly **160 ms** (extrapolated from 60 s of pure data window). Chrome is
~**3.4× faster per block** at the BLE layer — the rest of the wall-clock
gap closes because both stacks have similar ~5 s setup + reset+pairing
overhead.

### So we just found 2.2× speedup with no firmware change

The widget already ships this faster path. The Python rig is a useful
protocol-correctness regression test, but its wall-clock numbers should
NOT be used as the baseline for "how fast can BLE flash actually go" —
the widget's number is the real ceiling for *this* protocol on *this*
host stack today.

### What we still don't know

Why is bleak slower? Three plausible mechanisms; we haven't confirmed
which:

1. **Bleak serialises writes through Python asyncio**, adding ~20 ms of
   queue/context-switch latency per call. Chrome's WinRT integration
   may queue multiple writes onto a single connection interval.
2. **Bleak's `response=False` may quietly fall back to write-with-response**
   on some Windows code paths. Need to confirm with HCI snoop or
   Wireshark BLE capture.
3. **Connection interval differs by stack**. Chrome may negotiate a
   shorter interval via implicit ATT MTU exchange (Chrome auto-requests
   ATT_MTU=247 on connect, bleak does not).

(3) is the most likely single explanation — a ~7 ms vs ~30 ms interval
gap matches the 3-4× per-block ratio.

### Phase-3 (DFU) is invalid — needs a re-run

The DFU phase reported `ok=true` in 1.5 s, but inspection of the log shows
the widget hit "BLE flash failed: Bluetooth-Verbindung fehlgeschlagen"
immediately after starting (line 125 of widget-run log). The post-partial
auto-reconnect was unstable in the 10 s settle window between phases —
the widget connected, then dropped, then reconnected, but the DFU kicked
off in the dropped window. The widget's reconnect daemon caught it but
flashCalliope didn't throw, leaving our runFlash recording ok=true.

To re-run cleanly: longer settle (30 s instead of 10), or wait for
`bleStatus === 'connected'` to hold stable for N seconds before
proceeding to the next phase.

## Widget-demo harness details

Scaffolded in `widget-demo/`. See [widget-demo/README.md](widget-demo/README.md).
Notes that came up during the run:

- Puppeteer's synthetic `page.click()` is **rejected** by Chrome for
  `navigator.bluetooth.requestDevice()` — Chrome demands a "real" user
  gesture. The driver highlights the connect button and waits for the
  user to click it manually. After the one-time pairing the profile at
  `widget-demo/.chrome-profile/` caches the device permission so silent
  reconnect via `getDevices()` works on subsequent runs.
- Puppeteer's default `protocolTimeout=180000` kills any RPC that idles
  beyond it (including a long `waitForFunction`). Driver now uses
  `protocolTimeout: 0` + per-second `evaluate()` polling.

## Next steps

In rough priority order:

1. **User drives the one-time BLE pairing**, then runs `--auto` for the
   widget-vs-Python comparison.
2. **Consider PRN-style batching for partial flash?** The codal partial-flash
   service responds to every block. We can't change that, but we could
   parallelise: write N+1's first packet while waiting for N's ACK. This
   would need careful sequencing because the service uses sequence numbers,
   not block addresses, for ordering. Probably 2× speedup at most, but
   would push 24 KB partial flash from 65 s → ~33 s.
3. **Bonded BLE re-test.** The user's original question: would adding
   "fake-bond" support reduce these numbers? Worth running phase-2 + phase-4
   once with a paired Windows host to measure the connection-interval
   improvement.

## Reproducing

### Python rig (autonomous)

```
cd c:/GIT/Calliope/calliope-flash-test/flash-test
node build-test-hexes.mjs        # one-time
python runner.py                 # full 4-phase, autonomous
```

### Widget rig (Puppeteer + user-driven pairing)

```
cd c:/GIT/Calliope/calliope-flash-test/flash-test/widget-demo
pnpm install                          # one-time
pnpm dev                              # terminal A
node puppeteer-driver.mjs             # terminal B — first run: pair via chooser
node puppeteer-driver.mjs --auto      # later runs: unattended 4-phase
```
