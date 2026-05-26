# Calliope mini 3 BLE DFU — native test rig

Python `bleak` test suite that talks to a Calliope mini directly over BLE
from this Windows PC, bypassing Chrome's Web Bluetooth (and therefore the
campus + widget + iframe stack). Useful for nailing down questions the
widget logs leave ambiguous — MTU, security, what the bootloader actually
sees on the wire.

## Setup

```powershell
cd C:\GIT\Calliope\LLM\ble-test
python -m venv .venv
.venv\Scripts\pip install bleak
```

All test scripts auto-find the strongest-signal Calliope-ish device in
range. Run with `.venv\Scripts\python <script>` (must be UTF-8 capable;
the scripts force `sys.stdout.reconfigure(encoding="utf-8")`).

## Scripts

| Script | Purpose |
|---|---|
| `01_scan.py` | Passive 8-second scan; lists every Calliope adv heard + adv frequency + service UUIDs |
| `02_inspect.py` | Connect + dump every service / characteristic / descriptor + each char's `max_write_without_response_size` (what the host stack believes MTU-3 is) |
| `03_mtu_probe.py` | Write 20→500 bytes to the bootloader Control Point — see at what size the host stack rejects the call |
| `04_dfu_flow.py` | Simulate a single 4096-byte chunk transfer via Packet characteristic, then CalcChecksum to see what the bootloader actually received. `--payload N` to vary write size |
| `05_dfu_prn.py` | Same chunk-streaming flow but with PRN enabled (notify every N packets). `--payload N --prn N --chunk N --execute` |
| `06_dfu_packet_calibrate.py` | Write a small fixed number of packets at a chosen size, then CalcChecksum — confirm bytes received |

`--execute` is destructive (actually commits the chunk to flash). Default
is probe-only — `CreateObject(Data)` resets per-object state on next
attempt so the bootloader stays clean.

## What we learned (2026-05-19)

### Bootloader BLE state

- Calliope mini 3 in DfuTarg mode advertises ONLY the Nordic DFU service
  (`0x0000fe59-…`). The vendor-specific Secure DFU service
  (`0x8EC9000?-…`) is *inside* `0x0000fe59`'s primary service, exposed via
  the `0x8EC90001` (Control Point — `notify | write`) and `0x8EC90002`
  (Packet — `write-without-response`) characteristics.
- `max_write_without_response_size` is reported as **244** by the Windows
  BT stack on both characteristics. **This number lies.**
- The bootloader actually negotiated ATT MTU = 23. Any write >20 bytes
  to the Packet characteristic is silently dropped at the bootloader's
  ATT layer. No PRN fires, the bootloader's offset stays at 0.
- Repeated oversized writes trigger Windows to attempt automatic
  encryption upgrade (just-works pairing). The bonded bootloader rejects
  the pairing → "X" on the LED matrix and the device wedges until reset.

### What works

| Payload size | PRN | Result |
|---|---|---|
| 20 B | 0 | All packets accepted, CalcChecksum returns correct offset+CRC |
| 20 B | 1 | Every packet gets its own PRN, perfect CRC match throughout, ~660 B/s |
| 244 B | 0 | Writes return OK but bootloader receives 0 bytes (offset stays at 0). CalcChecksum times out at 5s (bootloader silent) |
| 244 B | 1 | Same — no PRN ever fires, bootloader silent |

### Implications for the widget

- `mini-connection-widget/src/ble-dfu-web.ts:PACKET_PAYLOAD_STEPS` MUST
  be `[20]` only. Higher values appear to work at the host's BLE API
  level but silently drop on the device. The misleading-success then
  triggers the "X" pairing failure.
- iOS Nordic DFU works on the same hex because the Nordic iOS library
  hardcodes 20-byte writes — they don't trust the OS-reported MTU.
- Bootloader's max chunk size is 4096 (page-aligned). The chunk size
  passed to `CreateObject(Data, N)` must be ≤ 4096 AND a multiple of 4.
  Random sizes like 732 are rejected with `result=0x03 INVALID_PARAMETER`.
- The bootloader does NOT respond to `MtuGet (op 0x07)` — returns
  `0x02 OP_NOT_SUPPORTED`. So we can't query MTU at runtime.

### Open question

Why does the bootloader negotiate MTU 23 when the Windows BT stack
would happily go to 247? The bootloader's BLE manager is built from
Nordic SDK 17's `ble_dfu_transport` (`nrf_dfu_ble.c`). The SDK normally
responds to `ATT_EXCHANGE_MTU_REQ` with its own configured MTU
(`NRF_SDH_BLE_GATT_MAX_MTU_SIZE`, default 23). The Calliope v3-bootloader
SDK config doesn't appear to override this — so MTU stays at 23.

**Fix would be firmware-side**: bump `NRF_SDH_BLE_GATT_MAX_MTU_SIZE` to
247 in the bootloader's sdk_config.h and rebuild. Not attempting that
now after the earlier toolchain-mismatch bricking. For rc07 dev,
20-byte writes are fine; we accept the ~45-second flash time.

### How to reproduce the finding

```powershell
# Put the Calliope into bootloader mode (button A + Reset, or trigger
# DFU from any client). The display should show the Bluetooth logo.
.venv\Scripts\python 01_scan.py            # verify DfuTarg adv visible
.venv\Scripts\python 02_inspect.py         # confirm Nordic DFU service exposed
.venv\Scripts\python 05_dfu_prn.py --payload 20 --prn 1 --chunk 4096
# → should see 205 packets stream with perfect PRN/CRC

# Now the same with payload 244 — fails silently
.venv\Scripts\python 05_dfu_prn.py --payload 244 --prn 1 --chunk 4096
# → first packet "writes OK", PRN never arrives, 15s timeout
```

The mini should be reset between runs if it ends in a bad state ("X"
display = bond failed; "+" display = mid-transfer aborted).

## Glossary

- **PRN** = Packet Receipt Notification. Bootloader-emitted checksum on
  control point after every N successful Packet writes. The host uses
  this for flow control + per-batch CRC verification.
- **MTU** = Maximum Transmission Unit. ATT MTU - 3 = max payload per
  write. Default ATT MTU = 23 (payload 20). Negotiated upward via
  `ATT_EXCHANGE_MTU_REQ` early in the connection.
- **Buttonless DFU** = the in-app characteristic (`0x8EC90003`
  unbonded / `0x8EC90004` bonded) the host writes 0x01 to in order to
  reboot the device into the bootloader. Lives in the application
  firmware, not the bootloader.
- **Secure DFU** = the actual flash protocol exposed by the bootloader
  via Control Point + Packet characteristics. The thing we've been
  fighting with above.
