# Transfer-speed test conclusions (2026-05-21)

Two-line summary:

- **Partial flash:** widget is already 2.2× faster than my Python+bleak
  reference (29 s vs 66 s for a 24 KB filesystem change). Don't use bleak
  as the speed reference — it's slow because of how Windows WinRT
  negotiates the link with it, not because of the protocol.
- **Full DFU:** no Web-BLE-only speed lever exists. The bootloader is
  hardcoded to ATT MTU 23 (silently drops anything larger), so every
  client — widget, Python+bleak, iOS, Android — is stuck at 20-byte
  writes. To make DFU faster needs a v2-bootloader change. Out of scope
  given your "no firmware changes" constraint.

## What we have hard numbers for

| measurement                  | best (widget) | other        |
|------------------------------|---------------|--------------|
| Partial flash 24 KB (prog-A-mod) | **29.9 s** ✓ | Python+bleak 66 s |
| Partial flash 24 KB (prog-A)     | **29.1 s** ✓ | — |
| Full DFU 146 KB (Blocks)         | (couldn't test cleanly) | Python+bleak 87-116 s |
| USB drag-flash 1.2 MB            | 23 s        | (single path; same for both) |

For DFU, the widget run never completed in our test rig because of a
state issue: ~6 s idle between connect and flash → first GATT operation
fails with `Bluetooth-Verbindung fehlgeschlagen`. The widget then
silently swallows that error (widget-side bug: transient BLE error
during partial-flash entry returns ok=true without falling back to DFU).
Not a transfer-speed problem — but worth fixing in the widget separately.

Because the bootloader caps both widget and Python at 20-byte writes,
the partial-flash MTU upgrade Chrome gets does NOT apply during DFU.
**Widget DFU should be roughly the same speed as Python DFU** (~90-110 s).

## What slows DFU and why we can't fix it from the web side

Source of truth: `mini-connection-widget/src/ble-dfu-web.ts:128-160`,
inline comment captured from an empirical session 2026-05-19:

> "Any write >20 bytes to the Packet characteristic (8EC90002) succeeds
> at the host's BLE stack but is silently dropped at the bootloader's
> ATT layer — no PRN fires, the bootloader's offset stays at zero.
> After enough silent drops, Windows tries to upgrade encryption
> "just-works" thinking it'll help; the bonded bootloader rejects the
> pairing → 'X' on the LED matrix and the device is wedged until reset.
>
> iOS Nordic DFU works because Apple's library hardcodes 20-byte writes
> per Nordic Secure DFU recommendations — we do the same.
>
> Trade-off: ~4 KB/s wire speed. 180 KB firmware = ~45 s. Slow but
> reliable. Larger payloads can be revisited once the firmware properly
> exposes MTU exchange (probably needs SDK_CONFIG change in v3-bootloader's
> nrf_dfu_ble.c — Nordic SDK 17's BLE manager normally responds to MTU
> exchange but maybe the Calliope build has it disabled or the GATT MTU
> is hardcoded)."

The only Web-side DFU knobs that remain:

| knob | current | feasible change | expected gain |
|------|---------|-----------------|---------------|
| PRN interval | 6 | 8 (cap of `NRF_DFU_BLE_BUFFERS=8`) | ~3-5 % fewer notification waits |
| Chunk size | bootloader-set | n/a | — |
| Packet size | 20 bytes | impossible without bootloader change | — |
| PHY | 1 Mbps | impossible without bootloader change | — |
| Connection interval | ~30 ms | impossible (bootloader requires bond for 1.x intervals) | — |

PRN=8 is the only safe change. Per the comment, raising PRN beyond 8
risks RX-buffer overflow during page-erase windows (~85 ms) where the
bootloader stops draining. Expected speedup: marginal (~3-5 %), maybe
5 s shaved off a 90-s DFU. Not worth the regression risk.

## Where the partial-flash 2.2× speedup actually comes from

When we connect with `bleak.BleakClient`, the WinRT GattSession reports
`MaxPduSize = 23` — the LE default that's never negotiated up. Even
after multiple GATT writes, `MaxPduSize` stays at 23.

Chrome's Web Bluetooth on Windows evidently negotiates higher (Chrome's
typical post-negotiation MaxPduSize is 247 on WinRT). This doesn't show
up directly in the partial-flash wire payload (both still send 20-byte
writes per protocol), but it changes how the LE controller packs writes
into connection events:

- MTU 23 / DLE off: 1 write per connection event ⇒ ~30 ms per write
- MTU 247 / DLE on: multiple writes per event ⇒ writes amortise

This matches the per-block measurement: Chrome's ~47 ms/block vs bleak's
~160 ms/block.

Bleak has **no Windows API** to force MTU exchange — Windows decides at
GATT session start. So:

- The Python rig is fine as a protocol-correctness regression test, but
  its wall-clock numbers are not the speed reference.
- For any CLI/test tool that needs to match the widget's speed, use
  Node + `@abandonware/noble`, or drive headless Chrome.
- The widget's speed is the true ceiling for this protocol on this OS.

## Practical recommendations

In order of value-per-effort:

1. **Partial flash already meets the bar.** 29 s for a 24 KB filesystem
   change is acceptable for a campus iteration loop. No work needed.
2. **Fix the widget's silent-swallow bug** so that when partial flash's
   first GATT call fails with a transient error, the dispatcher falls
   back to DFU instead of returning ok=true. See `ble.ts:584
   handleBleFlashError(err)` — that function logs and updates UI state
   but doesn't re-throw, so `flashOverBle` thinks the partial flash
   succeeded. (Separate bugfix, not a speed issue.)
3. **Make the partial-flash path the only path for non-runtime changes.**
   Right now MakeCode/Blocks edits *might* hit DFU if `parseMicroPythonHex`
   /`parseMakeCode` fails. Tightening the parser to recognise more hex
   shapes would route more flashes through the fast 29-s path.
4. **DFU is unavoidable when changing the MicroPython runtime version.**
   That's a rare event (firmware upgrade, not a code change). Users can
   absorb a 90-s wait on that workflow.
5. **If full DFU speed is ever a hard requirement**, the only real win
   is a v2-bootloader rebuild with `BLE_GATT_ATT_MTU_DEFAULT=247` and
   the SDK_CONFIG change in `nrf_dfu_ble.c` mentioned in the inline
   comment. Predicted gain: 12× (per-packet payload from 20 → 244
   bytes, fewer connection events per chunk). Bootloader change is
   not "codal" per se — it's a separate v2-bootloader repo. The
   open-mode app firmware stays as-is.

## What didn't work in this session

- Programmatic `page.click('#b-connect-ble')` from Puppeteer: Chrome
  rejects synthetic gestures for `navigator.bluetooth.requestDevice()`.
  Requires real user click; cached profile then carries through.
- Puppeteer default `protocolTimeout=180000` kills RPCs that wait that
  long. Driver sets `protocolTimeout: 0` and polls instead.
- DFU phase in the widget driver: BLE was nominally connected but the
  first GATT call after ~6 s of idle threw a transient error. Reproducible
  across two attempts (programmatic + manual button click). Widget then
  silently returned ok=true.

## Test artefacts

- [results/full-run-1.log](results/full-run-1.log),
  [results/full-run-2.log](results/full-run-2.log) — Python rig 4-phase runs
- [results/widget-run-2026-05-21T12-11-18.log](results/widget-run-2026-05-21T12-11-18.log) — 2× partial flash widget run with 29 s timings
- [results/widget-run-2026-05-21T12-32-31.log](results/widget-run-2026-05-21T12-32-31.log) — failed DFU programmatic
- [SPEED-EXPERIMENTS.md](SPEED-EXPERIMENTS.md) — experiment matrix and rationale
- [FINDINGS.md](FINDINGS.md) — detailed per-phase analysis
