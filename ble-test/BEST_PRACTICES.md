# Calliope mini 3 BLE-DFU — best practices

**Audience:** engineers shipping BLE flash for Calliope mini 3 in any web/desktop client (campus widget, Teachable, custom apps), or modifying the device firmware.
**Hardware target:** Calliope mini 3 (nRF52833, CODAL-based, v3-bootloader Nordic Secure DFU).
**Last verified:** 2026-05-19.

If you're reading this because BLE DFU isn't working, jump to [Diagnostic checklist](#diagnostic-checklist) first.

---

## TL;DR

**To enable BLE DFU without OS bonding (i.e. without the user pre-pairing through Windows/macOS Bluetooth settings):**

### App firmware

```jsonc
// yotta config (pxt.json or codal.json equivalent)
"microbit-dal": {
  "bluetooth": {
    "enabled": 1,
    "pairing_mode": 1,
    "private_addressing": 0,
    "whitelist": 0,                    // must be 0
    "advertising_timeout": 0,
    "tx_power": 6,
    "dfu_service": 1,
    "event_service": 0,
    "device_info_service": 1,
    "partial_flashing": 1,
    "security_level": "SECURITY_MODE_ENCRYPTION_OPEN_LINK"
    // equivalent: "open": 1, "security_level": null
  }
}
```

### Host (Web Bluetooth / native BLE)

- Write payload **= 20 bytes** per Packet write (ATT_WRITE_CMD, writeWithoutResponse).
- Flow control **PRN=6** on the bootloader Control Point.
- Send a valid init packet (Nordic Secure DFU NO_VALIDATION format: 12-byte `microbit_app` magic + `<I` version + `<I` app_size + `<I` 0 + 32-byte zero hash) before any data writes.
- See `mini-connection-widget/src/ble-dfu-web.ts` for the reference implementation.

### Bootloader

Use the **shipped binary as-is** (`codal-microbit-v2/lib/bootloader.o`, MD5 `a2d8d9a96d6684691252b21488ab9ff6`, 26052 bytes). Do not rebuild without GCC 7.3 — the SDK 16 source has known incompatibilities with GCC 10+ that produce binaries which boot dark.

---

## How the BLE-DFU pipeline works

```
┌──────────────────────────────────────────────────────────────────────┐
│ Calliope mini 3 flash layout (nRF52833, 512 KB)                      │
├──────────────────────────────────────────────────────────────────────┤
│ 0x00000 - 0x01000  MBR              (master boot record)              │
│ 0x01000 - 0x1C000  SoftDevice S113  (Nordic BLE stack)                │
│ 0x1C000 - 0x77000  Application      ← partial flashing writes here    │
│ 0x77000 - 0x7E000  Bootloader       ← Secure DFU code, never written  │
│                                       at runtime by BLE               │
│ 0x7E000 - 0x80000  MBR params + bootloader settings page              │
│ 0x10001014/18      UICR             pointers to bootloader + MBR      │
└──────────────────────────────────────────────────────────────────────┘
```

**Two distinct services do the flashing work:**

| Service | Where | Purpose |
|---|---|---|
| **In-app CODAL Partial Flashing** | App firmware (0x1C000-0x77000 range, exposed via `0xE97DD91D…`) | Quick partial updates of the program region; gated by `MICROBIT_BLE_SECURITY_MODE` on its CCCD |
| **In-app Buttonless DFU** | App firmware, characteristic `0x8EC90003` (unbonded) or `0x8EC90004` (bonded) | Host writes `0x01` here to ask the device to reboot into bootloader. **Different security per variant** — UUID is selected at compile time |
| **Bootloader Secure DFU** | Bootloader (0x77000-0x7E000), characteristics `0x8EC90001` (Control Point) + `0x8EC90002` (Packet) | Actual flashing protocol once in bootloader |

**The three security configurations that interact:**

| App `security_level` | Buttonless UUID | App char security | Bootloader char security | Unbonded-DFU works? |
|---|---|---|---|---|
| `SECURITY_MODE_ENCRYPTION_OPEN_LINK` (mode 1) | `8EC90003` (unbonded variant) | SEC_OPEN (no encryption) | SEC_OPEN | ✅ yes |
| `SECURITY_MODE_ENCRYPTION_NO_MITM` (mode 2) | `8EC90004` (bonded variant) | SEC_JUST_WORKS | SEC_OPEN* | ❌ no — buttonless CCCD subscribe blocked |
| `SECURITY_MODE_ENCRYPTION_WITH_MITM` (mode 3) | `8EC90004` (bonded variant) | SEC_MITM | SEC_OPEN* | ❌ no — same, plus MITM |

*The bootloader's own DFU characteristics are SEC_OPEN regardless of app-side security. The bottleneck is the in-app buttonless characteristic, not the bootloader.

---

## The 4 hard requirements for unbonded BLE-DFU

If any of these are missing, unbonded DFU breaks somewhere along the chain.

### 1. `MICROBIT_BLE_SECURITY_LEVEL = "SECURITY_MODE_ENCRYPTION_OPEN_LINK"` (or `MICROBIT_BLE_OPEN = 1`)

This propagates through `MicroBitConfig.h`'s macro chain to `MICROBIT_BLE_SECURITY_MODE = 1`, which:
- Makes the in-app buttonless characteristic SEC_OPEN (no CCCD subscribe encryption)
- Selects the unbonded variant of the Nordic SDK's `ble_dfu` (UUID `0x8EC90003`)

`OPEN_LINK` and `OPEN=1` compile to **bit-identical** binaries. Use whichever is clearer in your codebase. The campus-open branch of `pxt-calliope` uses `open: 1` for `core-mini-codal` because it's terser.

### 2. `MICROBIT_BLE_WHITELIST = 0`

Whitelist mode (`= 1`) makes the device **invisible to unbonded scanners** — it only does directed advertising to already-bonded peers. With no existing bond, Web Bluetooth's `requestDevice()` picker won't show the device. There's no recovery path from this without entering pairing mode on the device first.

### 3. Host writes 20 bytes per packet, with PRN=6

The bootloader's negotiated ATT MTU is 23 (max payload 20 bytes per `ATT_WRITE_CMD`). Anything larger is silently dropped at the bootloader's ATT layer — the host's BLE API will report the write as successful, but the bootloader's offset stays at 0. Confirmed empirically with native `bleak`, 2026-05-19.

PRN=6 means "send checksum notification every 6 packets" — that keeps us under Nordic SDK's default `NRF_DFU_BLE_BUFFERS=8` RX buffer limit. Without PRN, packets pile up after ~110 bytes and the bootloader drops the rest silently.

### 4. Host sends a valid init packet before any data

Nordic Secure DFU requires the init packet first. Without it, `CreateObject(Data)` returns `0x08 OPERATION_NOT_PERMITTED`. The init packet's `microbit_app` magic + size + (optional) SHA256 hash tells the bootloader the firmware is valid.

For dev/test you can use the `NO_VALIDATION` form (`hash_size = 0`, 32 bytes of zeros) — the v3-bootloader's `94f99a4 "Use NO_VALIDATION"` commit makes it accept these. For production hexes, use the real SHA256 of the application binary (see `ble-dfu-web.ts#createInitPacketV2`).

---

## Common pitfalls

### "The device boots dark after I rebuild the bootloader"

You used GCC 10+ to build the v3-bootloader. The Nordic SDK 16 source has incompatibilities (`-fno-common` default in GCC 10, stricter aliasing, etc.) that produce a binary which won't initialize the SoftDevice. **Use GCC 7.3** (the SDK 16 vintage toolchain) — the bootloader's `Makefile.windows` points here by default. The earlier 2026-05-18 attempt with GCC 10.3.1 wedged the device until USB-flash recovery.

### "MAINTENANCE drive doesn't appear"

Hold button A on the mini while pressing Reset. The DAPLink firmware enters mass-storage mode and the drive should mount. If it still doesn't, the USB cable may be charge-only (no data lines) — try a different cable.

### "Build cache lies about config changes"

`python build.py` in `microbit-v2-samples` does not invalidate ninja's cache when only the `codal.json` `config` block changes. Files that USE the new defines (Nordic SDK source files) don't get re-compiled — you'll get a binary built with the previous config. **Always `rm -rf build/` before changing the `config` block.** (Changing `target.branch` does invalidate, because it triggers a target re-clone.)

### "I set defines in `target.json`'s config block but nothing changed"

`target.json`'s `config` block defines **are not propagated** to the Nordic SDK source files through codal's build system. Adding `MICROBIT_BLE_SECURITY_MODE=1` or `NRF_DFU_BLE_BUTTONLESS_SUPPORTS_BONDS=0` to `target.json` is a no-op — those flags must go in the **application's `codal.json` config block** (or `microbit-dal` yotta config in pxt.json). Confirmed by md5-comparing builds with and without `target.json` overrides on 2026-05-19; identical output.

### "Same yotta block, different recipes, same binary"

Three different yotta configs all produce bit-identical hexes (md5 `c2ac16da`) because the compiler/linker eliminates the differences:

```jsonc
// recipe 1: OPEN macro forces everything
"bluetooth": { "open": 1, "security_level": null, "whitelist": 0 }
// recipe 2: SECURITY_LEVEL drives SECURITY_MODE; explicit individual fields
"bluetooth": { "open": 0, "security_level": "SECURITY_MODE_ENCRYPTION_OPEN_LINK", "whitelist": 0 }
// recipe 3: same as 2 but with extra explicit fields
"bluetooth": { "enabled": 1, "pairing_mode": 1, "private_addressing": 0, "whitelist": 0, "advertising_timeout": 0, "tx_power": 6, "dfu_service": 1, "event_service": 0, "device_info_service": 1, "security_level": "SECURITY_MODE_ENCRYPTION_OPEN_LINK" }
```

This is fine. Pick the recipe that's clearest for your project; the runtime behavior is identical.

### "Chrome shows mwwr=244 but writes >20 bytes fail"

This is the most subtle finding. The OS BT stack reports MTU 247 (max-write-without-response-size = 244) because that's what was negotiated at the L2CAP layer. But the **bootloader's actual MTU is 23** — anything larger than 20 bytes is silently dropped at the bootloader's ATT layer. After ~3 silent drops, Windows tries to upgrade encryption (just-works), the bonded bootloader rejects, and the display shows "X" (bond failed).

Always use **20 bytes per writeWithoutResponse** on the bootloader Packet characteristic, regardless of what the OS reports as maximum.

### "Single 244-byte write works, but multiple in a row don't"

`writeWithResponse` (which uses ATT prepared-writes internally) can land a single 244-byte write — confirmed via `10_writeresp_probe.py`. But sequenced writes (244 bytes × N packets) destabilize the bootloader after 3-4 packets because it can't process the previous payload to flash fast enough. Windows BT then cancels subsequent writes with `ERROR_CANCELLED (0x800704C7)`.

Conclusion: **don't try to use writeWithResponse for speed on this bootloader build**. The firmware-side fix would be increasing `NRF_DFU_BLE_BUFFERS` in `sdk_config.h` and rebuilding the bootloader.

### "Whitelisted device doesn't show in BLE picker"

If you flashed a hex with `whitelist: 1` (legacy paired-mode default), `requestDevice()` won't see it without an existing OS bond. Recovery:
1. Long-press button A+B on the mini while pressing Reset → enters pairing mode
2. Add via Windows Bluetooth settings → bond established
3. Now `requestDevice()` will see it

For rc07 dev we explicitly want `whitelist: 0` so this never bites.

### "iOS Calliope app flashes without bond on a paired-mode hex"

iOS users had pre-paired the device in iOS Bluetooth settings before opening the Calliope app. iOS's CoreBluetooth then has an existing bond + just-works encryption available for the Secure DFU service. **Web Bluetooth on Chrome+Windows has no equivalent transparent re-pair path** — there's no "the user already bonded this device once, please use that bond" API. So for Web Bluetooth, the user-experience requirement of "click connect, flash works" forces us to use the open-mode (`OPEN_LINK`) firmware config.

---

## Performance expectations

| Mode | Wire speed | 180 KB MakeCode hex | Notes |
|---|---|---|---|
| **20 B + PRN=6 + writeWithoutResponse** (current widget) | 1.6 KB/s | ~115 s | The reliable mode. Ship this. |
| 244 B writeWithResponse sequenced | unstable | n/a | Bootloader can't keep up; ~3 packets before crash |
| iOS Nordic DFU (paired-mode hex, pre-bonded device) | ~6 KB/s | ~30 s | What we'd love to match |

To match iOS speed end-to-end on Web Bluetooth, the firmware-side change is bumping `NRF_DFU_BLE_BUFFERS` (Nordic SDK default = 8) to 16-24 in the bootloader's `sdk_config.h` and rebuilding. Pair that with PRN=8 on the host side. **Don't attempt the rebuild without GCC 7.3** (see "boots dark" pitfall above).

---

## Testing workflow

Reproducible test rig at `c:\GIT\Calliope\LLM\ble-test\`. Used for the 2026-05-19 investigation.

### Setup (one-time)

```powershell
cd C:\GIT\Calliope\LLM\ble-test
python -m venv .venv
.venv\Scripts\pip install bleak
```

### Probing a new firmware config

1. Edit `c:\GIT\Calliope\LLM\FIRMWARE\microbit-v2-samples\codal.json`'s config block with the BLE flags you want to test.
2. `cd microbit-v2-samples && rm -rf build && python build.py` *(rm -rf build is essential — see pitfalls)*
3. Copy `MICROBIT.hex` somewhere → drag-drop onto the mini's MAINTENANCE drive
4. Verify boot via display
5. `cd ble-test && .venv\Scripts\python 07_full_probe.py --label "your config"` — runs services + buttonless + reboot + DFU transfer + Execute
6. Read the result row; compare against expected behavior

### Test scripts inventory

| Script | Use when… |
|---|---|
| `01_scan.py` | "Is my device advertising? What's the signal strength?" |
| `02_inspect.py` | "What services / characteristics does this firmware expose? What MTU does the OS report?" |
| `03_mtu_probe.py` | "At what byte size does the host BLE stack reject the write call?" (host-side limit, not bootloader-side) |
| `04_dfu_flow.py` | "What does a basic single-chunk DFU transfer look like?" (no PRN) |
| `05_dfu_prn.py` | "Same with PRN flow control — useful for diagnosing rapid-write issues" |
| `06_dfu_packet_calibrate.py` | "Did the bootloader actually receive my bytes?" (small chunk, checked via CalcChecksum) |
| `07_full_probe.py` | **The main one.** End-to-end probe of a firmware config. Run after every USB-flash to verify behavior. |
| `08_single_packet_size.py` | "What's the on-wire MTU?" (single packet, varying sizes — needs in-DFU state) |
| `09_mtu_sweep.py` | Same but in a single session with init packet first |
| `10_writeresp_probe.py` | "Does writeWithResponse let me bypass the MTU limit?" |
| `11_speed_test.py` | "How fast is each mode in practice?" |

### Trigger entry to DFU bootloader from Python

After flashing a Config A hex:

```powershell
.venv\Scripts\python 07_full_probe.py --label "trigger" --skip-dfu
```

This writes `0x01` to the in-app buttonless characteristic, waits for the device-side disconnect, and exits. The device should be advertising as `DfuTarg` within 2-3 seconds.

### Recovery if a device wedges

Drag-drop `ble-test\hexes\RECOVERY.hex` onto MAINTENANCE drive. This is a heart-animating Config A hex — restores boot, exposes Config A's BLE config, ready for further testing. Build it from `microbit-v2-samples` with the current campus-open codal config if you ever lose it.

### Symptoms cheat sheet

| Display | Meaning | Recovery |
|---|---|---|
| Heart animation | App running normally (RECOVERY hex or any working test hex) | None needed |
| Bluetooth logo / scanning chevrons | In-app BLE active, awaiting connect / pair | None needed |
| **X** | Bond failed — Windows tried to encrypt and the bootloader rejected (usually after oversized writes) | Flash RECOVERY |
| **+** (with row of dots filling) | Bootloader in Secure DFU mode, mid-transfer | Flash RECOVERY (or complete the DFU) |
| Dark (no display) | Bootloader can't init SoftDevice (likely a broken rebuild) | Flash RECOVERY; do NOT trust the broken bootloader.o |

---

## Deployment checklist

When shipping unbonded BLE-DFU support to users:

### Firmware

- [ ] App built with `MICROBIT_BLE_OPEN=1` or `SECURITY_LEVEL=OPEN_LINK`
- [ ] `MICROBIT_BLE_WHITELIST = 0`
- [ ] `MICROBIT_BLE_DFU_SERVICE = 1`
- [ ] `MICROBIT_BLE_PARTIAL_FLASHING = 1`
- [ ] Bootloader is the shipped `lib/bootloader.o` (md5 `a2d8d9a9…`) — DO NOT REBUILD without GCC 7.3
- [ ] Init packet generator in your flashing client matches `microbit_dfu_app_t` layout (see `ble-dfu-web.ts#createInitPacketV2`)

### Host / widget

- [ ] `PACKET_PAYLOAD_STEPS = [20]` only — no MTU optimism
- [ ] `PRN_INTERVAL = 6`
- [ ] writeWithoutResponse for Packet writes
- [ ] writeWithResponse for Control Point writes
- [ ] Init packet sent before any data writes
- [ ] PRN waiter registered BEFORE the Nth packet write (notifications without a pending resolver are dropped — see `streamChunkWithPrn` in `ble-dfu-web.ts`)
- [ ] BLE classifier verdict `bond-ok` recognizes unbonded-but-open as "auth verified" (don't pop OS-pairing modal)

### Distribution

- [ ] Wire speed expectation set with end-users: ~2 minutes for a typical MakeCode hex. If that's unacceptable, see "Performance expectations" above for the firmware-side speed-up path.

---

## Future work (if you're picking this up later)

1. **Bootloader RX buffer expansion** — bump `NRF_DFU_BLE_BUFFERS` in `v3-bootloader/bootloader/microbit/config/sdk_config.h` to 16 or 24, rebuild with GCC 7.3 (NOT 10+), regenerate `bootloader.o`. Expected: ~4× speedup, matching iOS.
2. **ATT MTU exchange in the bootloader** — current bootloader stays at MTU 23. If `NRF_SDH_BLE_GATT_MAX_MTU_SIZE` could be bumped to 247 (and the bootloader RX path handles larger writes), we could use 244-byte writes natively.
3. **Web Bluetooth long-write support** — investigate whether Chrome's `BluetoothRemoteGATTCharacteristic.writeValue()` can be coerced to use ATT prepared-writes on a write-without-response-only characteristic. iOS does this; Chrome currently doesn't seem to. May require browser-level changes.
4. **OS-pairing fallback UX** — when users have a `whitelist=1` legacy hex on the device and need to flash a new one, the widget currently can't see the device. Add a UI flow: "Your Calliope is in paired mode — open Windows Bluetooth settings, add the device, then come back."
5. **Speed-up via larger chunks** — bootloader's `max_size` for data objects is 4096 (flash page). With faster wire transfer, increasing this to e.g. 16 KB pages could reduce per-chunk overhead. Needs firmware change.

---

## Reference docs in this project

- [`FINDINGS.md`](./FINDINGS.md) — the empirical results that led to this guide, with test logs
- [`README.md`](./README.md) — basic test rig setup
- [`mini-connection-widget/src/ble-dfu-web.ts`](../mini-connection-widget/src/ble-dfu-web.ts) — reference Web Bluetooth DFU implementation
- [`FIRMWARE/v3-bootloader/`](../FIRMWARE/v3-bootloader/) — Calliope v3 bootloader source (don't rebuild without GCC 7.3!)
- [`FIRMWARE/codal-microbit-v2/lib/bootloader.o`](../FIRMWARE/codal-microbit-v2/lib/bootloader.o) — the shipped bootloader binary; `a2d8d9a96d6684691252b21488ab9ff6`

---

## Diagnostic checklist

When something's broken, walk this list top-to-bottom — each step rules out a layer.

### 1. Is the device alive?

- USB-flash `ble-test\hexes\RECOVERY.hex` onto MAINTENANCE drive
- Display should show animated heart within ~3 seconds
- If dark → bootloader broken; check `lib/bootloader.o` md5 is `a2d8d9a9…`; do NOT rebuild without GCC 7.3
- If "X" → bond failed previously; recovery works, you're good for the next test

### 2. Does the device advertise to a fresh scanner?

```powershell
cd ble-test; .venv\Scripts\python 01_scan.py
```

- Should see `Calliope mini [xxxxx]` with strong RSSI (-50 or better)
- If not visible → most likely `MICROBIT_BLE_WHITELIST = 1` in the running firmware (directed adv only). Reflash an open-mode hex
- If visible as `DfuTarg` → device is already in bootloader from a previous interrupted DFU. That's fine; jump to step 5

### 3. Does the buttonless characteristic appear with the right UUID?

```powershell
.venv\Scripts\python 02_inspect.py
```

Look for `Secure DFU` service. Inside it:
- `8EC90003` = unbonded variant (= you want this for open-mode DFU)
- `8EC90004` = bonded variant (= app firmware is paired-mode; will fail unbonded DFU)

If you see `8EC90004` instead of `8EC90003`, check:
- App's `MICROBIT_BLE_SECURITY_LEVEL` — should be `OPEN_LINK` (or `MICROBIT_BLE_OPEN=1`)
- Build cache stale? `rm -rf microbit-v2-samples/build && python build.py` and reflash

### 4. Does the host actually negotiate ATT MTU 23?

```powershell
.venv\Scripts\python 02_inspect.py | findstr mwwr
```

- `mwwr=244` is the OS-reported "max writeWithoutResponse size" — this LIES about the bootloader's real limit
- `mwwr=20` on the bootloader is what you'd hope to see honestly reported
- Both can occur; trust empirically that **20 is the actual on-wire max** for the bootloader Packet characteristic

### 5. Can we do a single 4 KB DFU chunk transfer?

```powershell
.venv\Scripts\python 07_full_probe.py --label "diagnose"
```

Expected output for a working open-mode firmware:
- ✅ Buttonless write succeeded
- ✅ Device rebooted to DfuTarg
- ✅ DFU reconnect OK
- ✅ Init packet committed
- ✅ Chunk 4096 B transferred at 20B/PRN=6
- ✅ Execute OK

Failure modes:

| Symptom | Likely cause |
|---|---|
| "device not found" before app probe | Whitelisted firmware, see step 2 |
| "Insufficient Authentication" on subscribe | App is bonded variant (`8EC90004`), see step 3 |
| Buttonless write throws | Same as above |
| Device doesn't reboot to DfuTarg | App's SVCI mismatch with bootloader (maybe app uses unbonded SVCI but bootloader is bonded — unlikely on shipped firmware) |
| `op 0x01 res=0x08 OPERATION_NOT_PERMITTED` on CreateObject(Data) | Init packet not sent first — see `07_full_probe.py` for the init step |
| Chunk transfer hangs / GATT disconnect | Wire-write size > 20 bytes, or PRN missing — see step 6 |

### 6. If transfer fails: what wire size does the bootloader actually accept?

```powershell
.venv\Scripts\python 07_full_probe.py --skip-dfu   # gets device into DfuTarg
.venv\Scripts\python 09_mtu_sweep.py               # tests 20→244 single-shot
```

- 20 should land cleanly. 30+ should disconnect the link.
- If 20 doesn't even land, the bootloader is wedged — flash RECOVERY and start over

### 7. Widget-side checks (if Python works but Web Bluetooth doesn't)

- `mini-connection-widget/src/ble-dfu-web.ts`:
  - `PACKET_PAYLOAD_STEPS = [20]` (single entry only)
  - `PRN_INTERVAL = 6`
  - writeWithoutResponse (NOT response: true) for Packet writes
- Is `node_modules/@calliope-edu/mini-connection-widget` linked to the workspace copy? Check with `ls -la node_modules/@calliope-edu/`. If pnpm pulled from github tarball, your local widget changes won't be served. Switch to `"link:../mini-connection-widget"` in `package.json`
- Did Vite cache the old bundle? `rm -rf node_modules/.vite && pnpm dev -- --force`
- Mixed content? https-only campus tries to load http://localhost:3232 — check Chrome's lock icon for blocked content; Chrome allows http on localhost by default but verify in DevTools

---

## Quick reference: the magic numbers

| Constant | Value | What it is |
|---|---|---|
| Bootloader region | `0x77000` – `0x7E000` | 0x7000 = 28 KB |
| App region | `0x1C000` – `0x77000` | 0x5B000 = 364 KB |
| Settings page | `0x7F000` – `0x80000` | 4 KB, bootloader state |
| UICR NRFFW[0] | `0x10001014` | points at bootloader start |
| UICR NRFFW[1] | `0x10001018` | points at MBR params |
| ATT MTU (negotiated) | 23 | bootloader max; max 20-byte ATT payload |
| Buttonless DFU unbonded | `8EC90003-F315-4F60-9FB8-838830DAEA50` | enabled by SECURITY_MODE=1 |
| Buttonless DFU bonded | `8EC90004-F315-4F60-9FB8-838830DAEA50` | enabled by SECURITY_MODE≠1 |
| Secure DFU Control Point | `8EC90001-F315-4F60-9FB8-838830DAEA50` | bootloader cmd channel |
| Secure DFU Packet | `8EC90002-F315-4F60-9FB8-838830DAEA50` | bootloader data channel |
| Nordic DFU service | `0000FE59-0000-1000-8000-00805F9B34FB` | parent of all DFU chars |
| Partial Flashing service | `E97DD91D-251D-470A-A062-FA1922DFA9A8` | CODAL in-app fast updates |
| `NRF_DFU_BLE_BUFFERS` default | 8 | bootloader RX buffer slots |
| `PRN_INTERVAL` (widget) | 6 | host-side flow control |
| `PACKET_PAYLOAD_STEPS` (widget) | `[20]` | bytes per Packet write |
| Init packet size | 56 bytes | magic+ver+size+hashsize+hash |
