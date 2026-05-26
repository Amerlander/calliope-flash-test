# Speed-improvement experiments

## What's actually slow

Measured baseline from Python rig (logs in `results/full-run-*.log`):

| flash                | bytes   | time    | bytes/s | bytes/s relative |
|----------------------|---------|---------|---------|------------------|
| USB drag-flash       | 1.2 MB  | 23.3 s  | 53 k/s  | 100% (baseline)  |
| BLE Nordic DFU       | 146 KB  | ~100 s  | 1.5 k/s | ~3%              |
| BLE partial flash    | 24 KB   | 66 s    | 0.4 k/s | ~0.7%            |

BLE is ~30-130× slower than USB. **The slowness is in BLE itself**, not in
our protocols. Both DFU and partial-flash are limited by the same things:
ATT MTU and connection interval.

## Root cause

Three things compound:

1. **ATT MTU = 23 bytes** (default). Per write we move 20 bytes of payload
   (3 bytes ATT header). With MTU 247, one write moves 244 bytes — **12× more**.
2. **Connection interval ≈ 30 ms** when unbonded (Windows + Calliope default).
   Each notify-ACK round-trip takes ≥1 connection interval = ≥30 ms. With
   bonded link the interval can drop to 7.5 ms — **4× faster RTT**.
3. **PHY = 1 Mbps**. nrf52833 supports 2 Mbps but neither bleak nor Chrome
   Web Bluetooth API lets userspace ask for it — requires a codal-side
   PHY-update procedure on connect.

The protocol layer (DFU PRN=6 batching, partial-flash 4-packet blocks)
doesn't reduce the slow-down because the bottleneck is below it.

## Why partial-flash is worse per-byte than DFU

Despite being a "fast path", partial-flash transfers fewer bytes per RTT:

- DFU: pipeline up to 6 chunks (24 KB) before an ACK ⇒ amortises RTT.
- Partial: forced ACK after every 64-byte block ⇒ RTT cost is paid 384 times
  for our 24 KB hex.

For a 24 KB transfer, partial-flash does ~384 round-trips; DFU does ~36.
Different protocols, same underlying BLE stack, ~10× the round-trips →
~4× slower per byte despite being "the fast path".

## Experiments — in order of expected ROI

### E1. Widget vs Python parity — DONE 2026-05-21 ⚡

**Outcome contradicted prediction.** Widget was **2.2× faster** than
Python+bleak (29.9 s vs 66 s for 24 KB partial flash). Same hex, same
device, same Windows WinRT backend underneath both stacks.

Per-block timing: Chrome ~47 ms/block, bleak ~160 ms/block — Chrome is
3.4× faster at the BLE-write layer alone.

This means **switching userspace BLE library is itself a speed lever**,
and the Python rig's wall-clock numbers are not representative of what
the user-visible product (the widget) achieves today.

### E1b. Why is bleak slower than Chrome? — INVESTIGATED 2026-05-21

**Confirmed:** Bleak's `BleakClient(...)` on the WinRT backend reports
`mtu_size = 23` (LE default) and `GattSession.max_pdu_size = 23`. MTU
**stays at 23 even after multiple GATT writes** — bleak's WinRT backend
only subscribes to `MaxPduSizeChanged` events, it never issues an
ATT_MTU_EXCHANGE request.

Bleak has **no public API** on Windows to request a larger MTU. Windows
WinRT decides MTU at GATT session start; Chrome's Web Bluetooth
implementation evidently triggers a higher negotiation (presumably
247 — the typical WinRT-driven value), bleak does not.

Whether the bottleneck is the MTU itself (small ATT PDUs ⇒ one
write per connection event) or LE Data Length Extension (separate
negotiation, controls L2CAP fragmentation) we can't tell without an
HCI/Wireshark capture — but either way, the cause is in the OS-driven
link parameters, not in the protocol or the userspace code.

**Practical implication:**
- **Don't use bleak as the speed reference.** Its wall-clock numbers
  underestimate what the widget actually achieves by ~2.2×.
- **For CLI/test tools that need real speed**, use a Node-based BLE lib
  (`@abandonware/noble`) or drive Chrome headless — same WinRT but with
  Chrome's MTU negotiation behaviour.
- **For Python tooling where speed matters less than correctness** (the
  test rig itself), bleak is fine — protocol regression detection
  doesn't need fast transfers.

The Python rig stays as a protocol-correctness regression test only.
The widget is the speed reference.

### E2. Bonded BLE

Goal: test the user's original hypothesis — does pairing with Windows
shorten the connection interval enough to matter?

- Prerequisite: firmware must accept bond. The Q variant we use sets
  `MICROBIT_BLE_OPEN=1` only; per [project_codal_v035_security_mode_bug],
  this *may* still accept bonded pairing (the SECURITY_MODE override bug).
  Verify by attempting bond in Windows Settings.
- Setup: Pair via Windows Settings → re-connect via widget → measure
  partial flash + DFU.
- Compare: bonded vs unbonded elapsed.

**Predicted outcome:** 2-4× speedup if the interval drops from ~30 ms to
~7.5 ms. Bonded partial flash should hit 15-30 s instead of 66 s.

### E3. Force MTU exchange (codal-side change)

Goal: ATT MTU > 23 means one write carries more payload. The partial-flash
protocol uses fixed 20-byte payloads currently, so this requires both a
firmware AND a protocol change.

- Codal change: enable MTU exchange in `MicroBitBLEManager.cpp` (CODAL has
  it disabled by default per [project_ble_dfu_best_practices]).
- Protocol change: bump the partial-flash service to carry 64-byte blocks
  in a single write instead of 4×20. Breaks compat with existing widget.

**Predicted outcome:** 4× fewer writes per block. Smaller win than expected
because the RTT cost is per-ACK, not per-write. Maybe 1.5-2× speedup.

Skip until E2 results say RTT isn't the dominant cost.

### E4. 2 Mbps PHY

Goal: switch link from 1 Mbps to 2 Mbps. Halves transmission time per
packet but does NOT halve RTT (connection interval is fixed by the
slower side).

- Codal change: call `sd_ble_gap_phy_update` on connect.
- No widget/Python change.

**Predicted outcome:** small (~1.2×) improvement because most of the
RTT is interval-bound, not transmission-bound. Worth doing as a free
firmware change but won't move the needle.

### E5. Eliminate per-block ACK (protocol redesign)

Goal: change partial-flash to use a sliding-window scheme like DFU's
PRN=6 — ACK every N blocks, not every block.

- Codal change: rewrite `MicroBitPartialFlashingService.cpp` ACK logic.
- Widget+Python change: send N blocks then wait for cumulative ACK.

**Predicted outcome:** approaches DFU's per-byte rate. 24 KB partial flash
would drop from 66 s to ~15 s.

Largest potential win but largest scope. Reserve for if E2 (bonding) isn't
enough.

## Decision matrix

| If we want                  | And we can change          | Then do |
|-----------------------------|----------------------------|---------|
| Speedup without firmware    | nothing                    | E2 (bond) |
| 2× speedup, light firmware  | small codal tweak          | E2 + E4 |
| 4-5× speedup, heavy firmware | codal protocol redesign   | E2 + E5 |
| Maximum, no compat constraint | full BLE stack overhaul  | E2 + E3 + E4 + E5 |

The user originally proposed E2 ("Fake bond" — device accepts bond but
doesn't enforce). That maps directly to E2 here. If the Q firmware's
SECURITY_MODE override bug means it *already* accepts bonds (just with
the wrong UUID character `8EC90004`), then E2 may "just work" with
Windows pairing today — no firmware change needed.
