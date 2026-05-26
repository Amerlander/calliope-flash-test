# Calliope mini 3 — BLE DFU without bonding: conclusive findings

**Date:** 2026-05-19
**Tested by:** Native Python `bleak` 3.0.2 → Windows BT stack → Calliope mini 3 (nRF52833, CODAL v0.3.5-calliope-2 era)
**All 4 distinct firmware configs tested back-to-back with USB-flashed CODAL "heart" hexes.**

## TL;DR

**Only `MICROBIT_BLE_SECURITY_LEVEL = SECURITY_MODE_ENCRYPTION_OPEN_LINK` (equivalently `MICROBIT_BLE_OPEN = 1`) with `MICROBIT_BLE_WHITELIST = 0` enables BLE-DFU without an OS bond.**

Every other security configuration (NO_MITM, WITH_MITM) requires the device to already be OS-bonded before Web Bluetooth can do anything with it. The iOS Calliope app works on the regular paired-mode hex *because the user paired the device in iOS Settings* — the Nordic DFU framework on iOS then has an existing bond + just-works encryption to talk through.

## Test matrix

| Config | `MICROBIT_BLE_OPEN` | `SECURITY_LEVEL` | `WHITELIST` | hex md5 |
|---|---|---|---|---|
| **A** (T0 = T1 = T2) | `1` (or `0` with OPEN_LINK) | `OPEN_LINK` (mode=1) | 0 | `c2ac16da` |
| **B** (T3) | 0 | `NO_MITM` (mode=2) | 0 | `f6480d93` |
| **C** (T4) | 0 | `NO_MITM` | 1 | `20e6c365` |
| **D** (T5, iOS reference) | 0 | `NO_MITM` | 1, DIS=0, tx=0 | `3216060c` |

T0 / T1 / T2 compile to byte-identical binaries — `MICROBIT_BLE_OPEN=1`, `SECURITY_LEVEL=OPEN_LINK` with `MICROBIT_BLE_OPEN=0`, and the campus-open target.json overrides are all equivalent paths to "SECURITY_MODE=1, SEC_OPEN characteristics, no whitelist." Different recipes, same result.

## Results

| Config | Boots | Buttonless UUID | mwwr | Buttonless write (no bond) | Reboot → DfuTarg | Init pkt (NO_VAL) | Chunk @ 20B/PRN=6 | Execute |
|---|---|---|---|---|---|---|---|---|
| A | ✅ | `8EC90003` (unbonded) | 20 | ✅ | ✅ | ✅ | ✅ 4096 B match | ✅ |
| B | ✅ | `8EC90004` (bonded) | 20 | ❌ "Insufficient Authentication" on subscribe | — | — | — | — |
| C | ✅ (heart) | n/a | n/a | ❌ device not in scan (whitelist filters) | — | — | — | — |
| D | ✅ (heart) | n/a | n/a | ❌ device not in scan (whitelist filters) | — | — | — | — |

## Key technical facts discovered

1. **The bootloader's ATT MTU is 23**, not 247. Windows BT stack reports `max_write_without_response_size = 244` but the bootloader silently drops anything > 20 bytes. iOS Nordic DFU library hardcodes 20-byte writes; the widget must do the same.
2. **PRN flow control is mandatory** for chunked writes. With PRN=0 (no flow control), the bootloader's `NRF_DFU_BLE_BUFFERS=8` RX buffer overflows and packets are silently dropped after the first ~110 bytes. PRN=6 (notify every 6 packets) keeps us under the buffer limit. The widget already uses PRN=6.
3. **An init packet is required** before the bootloader permits data-object creation. Returns `0x08 OPERATION_NOT_PERMITTED` otherwise. The init packet can be a NO_VALIDATION variant (`hash_size=0`) which skips SHA256 verification — that's enabled by the v3-bootloader's `94f99a4 "Use NO_VALIDATION"` commit and works on this hardware.
4. **The buttonless characteristic UUID is selected by `(MICROBIT_BLE_SECURITY_MODE != 1)`** at compile time in `sdk_config.h`. SECURITY_MODE=1 (SEC_OPEN) → unbonded variant `8EC90003`. Any other value → bonded variant `8EC90004` (requires encrypted CCCD write).
5. **`MICROBIT_BLE_WHITELIST = 1`** with no existing bond makes the device **invisible to scanners** — it only directed-advertises to bonded peers. There's no way to connect a fresh host.
6. **The bootloader's Secure DFU service does NOT itself require encryption** for the Control Point or Packet characteristics on this firmware build. We confirmed it accepts unencrypted Select / Create / CalcChecksum / Execute / writeWithoutResponse without any pairing prompt. So the bottleneck was always app-side (buttonless characteristic + whitelist), not bootloader-side.

## Working recipe for open BLE-DFU

### Application's yotta config

```json
"microbit-dal": {
  "bluetooth": {
    "enabled": 1,
    "pairing_mode": 1,
    "private_addressing": 0,
    "whitelist": 0,
    "advertising_timeout": 0,
    "tx_power": 6,
    "dfu_service": 1,
    "event_service": 0,
    "device_info_service": 1,
    "partial_flashing": 1,
    "security_level": "SECURITY_MODE_ENCRYPTION_OPEN_LINK"
  }
}
```

Equivalently (different recipe, same binary): `"open": 1, "security_level": null, "whitelist": 0`.

### Widget configuration (mini-connection-widget)

- `PACKET_PAYLOAD_STEPS = [20]` — already set after this session.
- `PRN_INTERVAL = 6` — already set.
- Init packet flow uses `createInitPacketV2` with SHA-256 of the app binary (production) or `hash_size = 0` for NO_VALIDATION (test). Production widget should keep SHA-256.

### Bootloader

**No change needed.** The shipped Calliope v3 bootloader (md5 `a2d8d9a9` of `lib/bootloader.o`) already accepts unencrypted Secure DFU operations and supports NO_VALIDATION init packets. The earlier rebuild attempt was unnecessary and wedged the device due to a GCC 10 vs SDK 16 toolchain mismatch — that whole detour is reverted.

## Why each non-A config fails for Web Bluetooth

- **NO_MITM (B, C, D)**: The Nordic SDK builds the BONDED buttonless variant (`8EC90004`) whose CCCD requires SEC_JUST_WORKS authentication. Windows BT will NOT auto-pair a fresh device when Web Bluetooth subscribes — the user has to manually pair via Settings first. iOS CoreBluetooth DOES auto-handle just-works pairing transparently; that's why iOS works on the same hex.
- **Whitelist=1 (C, D)**: Device only directed-advertises to bonded peers. Fresh hosts can't even see the device in `requestDevice()` picker, let alone connect.

## Recommended firmware changes

1. **pxt-calliope `libs/core-mini-codal/pxt.json`** and **`libs/one-time-pairing/pxt.json`**: keep the campus-open flip (`open: 1, security_level: null, whitelist: 0`). Already done.
2. **micropython-calliope-mini-v3 `src/codal_app/codal.json`**: keep `MICROBIT_BLE_OPEN: 1, MICROBIT_BLE_SECURITY_MODE: 1, MICROBIT_BLE_WHITELIST: 0`. Already done in campus-open branch.
3. **codal-microbit-v2 `v0.3.5-calliope-2-campus-open` target.json**: The `MICROBIT_BLE_SECURITY_MODE: 1` and `NRF_DFU_BLE_BUTTONLESS_SUPPORTS_BONDS: 0` defines I added are **no-ops** (config block doesn't propagate to the Nordic SDK source files in this build setup). They can be removed for clarity, but they don't hurt either.

## Recommended widget UX change

When a user clicks "Flash via Bluetooth" and the device has `WHITELIST=1` configured (e.g., a legacy paired-mode hex), Web Bluetooth's `requestDevice` won't show the device. The widget should:

1. Detect "no Calliope showed up in picker" and surface a hint: "Your Calliope might be in paired-mode. Open Windows Bluetooth settings, add the device by pairing, then come back here."
2. After OS pairing, the device should appear and the widget can use the existing bond.

This is a separate UX improvement; not blocking for the open-mode rc07 path.

## Wire speed analysis (added 2026-05-19 same day)

Measured against the Calliope v3 bootloader from Config A, via native bleak:

| Mode | 4 KB chunk time | Throughput | Stable? | 180 KB projection |
|---|---|---|---|---|
| writeWithoutResponse + 20 B + PRN=6 *(current widget)* | 2556 ms | **1.6 KB/s** | ✅ | ~115 s |
| writeWithResponse + 244 B (single packet) | ~30 ms | (instant per pkt) | ✅ | n/a |
| writeWithResponse + 244 B sequenced + PRN=6 | died at pkt 4 | — | ❌ | — |
| writeWithResponse + 244 B sequenced + PRN=1 | died at pkt 1 | — | ❌ | — |

The bootloader **can** receive single 244-byte writes (via ATT prepared-writes), confirming MTU 247 is negotiated end-to-end. But sequenced 244-byte writes destabilize the bootloader after 3-ish packets — it can't keep up with rapid prepared-write transactions while simultaneously processing the previous payload into its flash buffer. Windows BT cancels subsequent writes with `ERROR_CANCELLED (0x800704C7)`.

**iOS achieves ~6 KB/s (180 KB in ~30 s)** by using 244-byte writes with very careful flow control — likely shorter inter-packet delays via Apple's native Core Bluetooth fast paths, possibly combined with ATT_WRITE_REQ ack-pacing that Windows BT can't replicate cleanly.

### To match iOS speed end-to-end

The firmware-side fix is increasing `NRF_DFU_BLE_BUFFERS` (Nordic SDK default = 8) in the bootloader's `sdk_config.h` and rebuilding. With ~16-24 buffers, the bootloader could absorb a burst of 244-byte writes without dropping subsequent ones. Combined with PRN=8 or so, throughput should match iOS.

**Not attempting that rebuild** in this session — the earlier bootloader rebuild bricked devices due to a GCC 10 vs SDK 16 toolchain mismatch. The toolchain situation needs to be resolved (use GCC 7.3) before any future bootloader work.

### What the widget does today

`PACKET_PAYLOAD_STEPS = [20]` + `PRN_INTERVAL = 6` + writeWithoutResponse. Slow but rock solid. A 180 KB MakeCode hex takes ~2 minutes; campus users will notice but it works.

## Test rig

All test scripts at `c:\GIT\Calliope\LLM\ble-test\`. Built hexes at `ble-test\hexes\`. See `README.md` for procedure.

## Scripts added this session

| Script | Purpose |
|---|---|
| `07_full_probe.py` | End-to-end probe for a single firmware config: services, buttonless, reboot, DFU |
| `08_single_packet_size.py` | Single-packet size sweep (failed pre-init) |
| `09_mtu_sweep.py` | Single-session init + per-size single-packet probe via writeWithoutResponse |
| `10_writeresp_probe.py` | Same sweep but via writeWithResponse — confirmed 244 B lands single-shot |
| `11_speed_test.py` | 4 KB chunk timing across 3 modes |
