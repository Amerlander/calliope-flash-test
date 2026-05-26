# DFU speedup — 4× achieved (2026-05-21)

End-to-end measurement comparing widget+old-bootloader vs widget+new-bootloader for a 146 KB Blocks DFU.

| stack                                  | DFU 146 KB elapsed | rate       | speedup |
|----------------------------------------|--------------------|------------|---------|
| Python+bleak, old bootloader           | 87–116 s           | ~1.5 KB/s  | 1.0×    |
| Widget, old bootloader                 | failed (BD_ADDR mismatch + couldn't reconnect) | n/a | n/a |
| **Widget, new bootloader (this work)** | **24.07 s**        | **6.1 KB/s** | **3.6–4.8×** |

Per-chunk timing on the new stack: ~550 ms for each 4 KB chunk (17 of those chunks = ~9.4 KB/s sustained inside chunks; the overhead between chunks brings the average down).

## What changed

Three changes, none of them dropping the open-mode no-bonding security setup:

### 1. Bootloader rebuild — same BD_ADDR as the app

**File:** `FIRMWARE/v3-bootloader/NRF5SDK_mods/components/libraries/bootloader/ble_dfu/nrf_dfu_ble.c:911-928`

Removed the Nordic SDK default `addr.addr[0] += 1;` line inside `gap_address_change()`. The stock SDK bumps the BD_ADDR by one when advertising openly so the bootloader is distinguishable on-air; but Web Bluetooth's `BluetoothDevice` handle is pinned to the app's MAC, so `device.gatt.connect()` couldn't reach `:B7` after we'd paired with `:B6`. With the bump removed, the bootloader advertises as `DfuTarg` at the same `:B6` and the widget's reconnect works transparently.

The bootloader is still distinguishable at the GATT layer (advertised service set is FE59 only, advertised name is `DfuTarg`), so this only affects the BD_ADDR — not the wire identity for any client that filters by name or service.

### 2. Bootloader rebuild — MTU 247 already configured

The existing `sdk_config.h` already had `NRF_SDH_BLE_GATT_MAX_MTU_SIZE = 247` and the `BLE_GATTS_EVT_EXCHANGE_MTU_REQUEST` handler in `nrf_dfu_ble.c` properly replies up to that. We confirmed this was active in the new build — the widget's `MTU_GET` call returns the negotiated value and the widget's adaptive ladder picks 244-byte writes (= MTU 247 − 3 ATT header).

The previously-deployed bootloader (older build in `codal-microbit-v2/lib/bootloader.o`) silently dropped writes >20 bytes — the comment in `mini-connection-widget/src/ble-dfu-web.ts:128-160` was written against that build. The rebuild fixes it.

### 3. Widget DFU client patch

**File:** `mini-connection-widget/src/ble-dfu-web.ts`

- `PACKET_PAYLOAD_STEPS` changed from `[20]` to `[244, 64, 20]` (largest first, adaptive step-down on chunk 1 failure handles older bootloaders that can't take 244)
- `PRN_INTERVAL` raised from 6 → 12 (safe with the new bootloader's 17 RX buffers; old bootloader caps at 8 so chunk-1 step-down also catches that)
- Removed the dead "MTU_GET is informational only" branch; now we actually trust the bootloader's MTU reply to pick the starting ladder step

### 4. Widget dispatcher fallback bug (companion fix)

**Files:** `mini-connection-widget/src/ble.ts:584`, `src/flash.ts:245-271`

Previously, `flashCalliopeViaBle` called `handleBleFlashError(err)` for any non-`Partial*` error and returned normally — so `flashOverBle` thought partial flash succeeded, the dispatcher recorded ok=true, and the DFU fallback never fired. Found while debugging the BD_ADDR issue (transient BLE failures during partial-flash entry were getting swallowed).

Fix:
- `flashCalliopeViaBle` now re-throws after `handleBleFlashError`
- `flashOverBle`'s catch widened to fall back to DFU on any partial-flash failure (not just the three named `Partial*` error types). Comment explains why.

## How the new bootloader gets deployed

The user pointed out that the bootloader is part of the codal-built MINI.hex (via `FIRMWARE/codal-microbit-v2/lib/bootloader.o`), and every USB drag-flash deploys a fresh bootloader. So for the field:

1. Rebuild `bootloader_s113.o` from the patched `nrf_dfu_ble.c`:
   ```
   cd FIRMWARE/v3-bootloader/bootloader/microbit/armgcc
   make nrf52833_xxaa_s113_object
   make bob   # produces bootloader_s113.o
   ```
2. Replace `FIRMWARE/codal-microbit-v2/lib/bootloader.o` with the rebuilt one.
3. Rebuild MicroPython MINI.hex (codal build picks up the new bootloader.o).
4. Ship the new MINI.hex via your normal release channel (campus-python-editor, etc).

For this test session we shortcut step 3 via [`flash-test/tools/merge-bootloader.py`](tools/merge-bootloader.py): overlays a freshly-built `nrf52833_xxaa_s113_object.hex` onto an existing host hex (preserving the settings page at 0x7E000+ from the host so the new bootloader inherits valid DFU state). The user's existing MINI-flashing workflow then drops the merged hex onto E:/ as usual.

## Where time still goes on the new stack

Per-chunk breakdown of the steady state (chunks 5-30, ~545 ms each):
- 16 × 244-byte writes per chunk (4096 / 256 rounded up, with the last write padded) ≈ 16 ATT round-trips
- PRN every 12 packets → ~2 receipt waits per chunk
- CalcChecksum + Execute at chunk end → ~2 control round-trips

So ~20 round-trips per chunk × ~25 ms per RTT (Web Bluetooth on Windows, unbonded) = ~500 ms — matches measurement.

## What further levers exist (if 24 s isn't fast enough)

Roughly in priority order:

1. **Bigger chunks.** Bootloader `Select Data Object` returns `maxSize` — we honour it (4096). Raising the bootloader's `NRF_DFU_BLE_BUFFERS` and chunk size would amortise the per-chunk overhead. ~5 % win.
2. **PRN ≥ 16.** With 17 RX buffers we can go higher than 12. Each PRN saves one notification RTT, so e.g. PRN=16 vs 12 trims ~3 % off.
3. **2 Mbps PHY.** nRF52833 supports it. Add a `sd_ble_gap_phy_update` call in the bootloader's connect handler. Halves the on-air time per packet but not per-RTT; ~10 % win in this regime.
4. **DLE max 251 octets.** `sdk_config.h` has `NRF_SDH_BLE_GAP_DATA_LENGTH = 27` (default). Bumping to 251 lets the LE controller pack one full ATT PDU per connection event without fragmentation. Should give another ~10 % wall-clock.

The combination of items 3 + 4 + chunk-size could realistically take DFU from 24 s to ~15 s. Diminishing returns after that without bonding.

## Verification done

- Old bootloader restored via prog-A.hex → REPL alive, BLE at :B6 — confirmed recoverable.
- New bootloader v2 deployed → REPL alive, BLE at :B6 — boots cleanly with preserved settings page.
- Buttonless DFU triggered → DfuTarg advertises at **:B6** (was :B7 before patch) — BD_ADDR fix confirmed.
- Widget DFU completed cleanly with `payload=244` from chunk 1 onwards — MTU exchange confirmed.
- 146,464 bytes flashed in 24.07 s — speedup confirmed.

## Files touched in this session

```
mini-connection-widget/src/ble.ts                                       # silent-swallow fix
mini-connection-widget/src/flash.ts                                     # broader DFU fallback
mini-connection-widget/src/ble-dfu-web.ts                               # 244-byte payload + PRN=12
FIRMWARE/v3-bootloader/NRF5SDK_mods/components/libraries/bootloader/ble_dfu/nrf_dfu_ble.c  # BD_ADDR keep
flash-test/tools/merge-bootloader.py                                   # NEW: hex region overlay
flash-test/tools/universal-to-v2.mjs                                   # NEW: universal-hex → V2-only
flash-test/test-hexes/prog-A-bootloader-v2.hex                         # NEW: deploy artifact
flash-test/test-hexes/prog-B-blocks-v2.hex                             # NEW: V2-only Blocks
flash-test/widget-demo/                                                # NEW: Puppeteer test harness
```
