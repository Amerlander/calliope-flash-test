# pxt-microbit-V5 / Blocks editor codal upgrade — recommendation

You asked whether the Blocks editor (`C:\GIT\Calliope\pxt-microbit-V5`) should adopt the new bootloader fix on its current codal tag, or jump to our latest `v0.3.5-campus-open-1`. Here's the lay of the land.

## Where pxt-microbit-V5 currently builds

`pxtarget.json:165-171` points at:

- repo: `calliope-edu/codal-microbit-v2`
- branch: **`public_ble_services`** (commit `f3119e2`)

The `public_ble_services` branch and our `v0.3.5-calliope-2-campus-open` (commit `eeb128f`) **diverged at `v0.2.40` (2022-06-15)** — a 3-year-old fork point. They are not on the same v0.3.5 line.

| branch                          | what's on it                                                                                          | commits unique to it |
|---------------------------------|-------------------------------------------------------------------------------------------------------|----------------------|
| v0.3.5-calliope-2-campus-open   | full upstream v0.3.5 + your fork's campus-open work (open BLE, no bonds, security_mode override, …)   | 292                  |
| public_ble_services             | v0.2.40 base + 12 BLE-advertising customisation commits for the Blocks editor                         | 12                   |

The 12 unique Blocks-editor commits add: `MicroBitBLEManager.configureAdvertising()`, custom advertising data parameter, `ble_advdata` integration, connectable/discoverable defaults, debug logging. All BLE-customisation work that's specific to how scratch-gui drives the device.

## Two options

### Option A: Drop the bootloader.o in on `public_ble_services`

Minimal change. Just replace `lib/bootloader.o` on that branch:

```bash
cd codal-microbit-v2
git checkout public_ble_services
cp ../v3-bootloader/bootloader/microbit/armgcc/bootloader_s113.o lib/bootloader.o
git commit -m "bootloader: rebuild with BD_ADDR keep-app + MTU 247"
git tag v0.2.40-calliope-public-ble-1
git push origin public_ble_services --tags
```

Then pxt-microbit-V5's `pxtarget.json:169` updates `"branch": "public_ble_services"` → `"branch": "v0.2.40-calliope-public-ble-1"` (or keep on branch — your call).

**Outcome:** Blocks editor gets the BD_ADDR fix + MTU 247 in DFU. Per-byte DFU speed improvement matches what we measured (3–4× faster). No risk of regressions in the 12 Blocks-specific commits because they don't touch the bootloader.

### Option B: Migrate Blocks to `v0.3.5-campus-open-1`

Major change. Three years of upstream commits from lancaster-university, plus all the calliope-edu campus-open work, would land in the Blocks editor's codal in one jump. Then re-apply the 12 Blocks-specific commits as a fresh patch series.

Benefits beyond the bootloader fix:
- Open-mode BLE security (no bonding required) at the app layer too — would let the Blocks editor's flash flow drop the chooser-after-bond UX.
- Newer microbit-core APIs (audio, mic, MEMS sensor wakeup, etc).
- Newer codal-core scheduler.

Risks:
- Audio/microphone hardware path changed materially in v0.3.x.
- nrf5sdk integration is reorganised — `lib/codal/libraries/codal-microbit-nrf5sdk/` is the new layout; v0.2.40 didn't have it.
- pxt-microbit-V5's `libs/microphone`, `libs/audio` may need API updates.
- The 12 Blocks-specific commits are against `MicroBitBLEManager` whose interface evolved — they'll need rewriting, not cherry-picking.

Estimated work: 1–2 weeks if you're prepared for an audio/mic regression hunt. Not the right call just to ship a bootloader fix.

## Recommendation

**Do Option A now** — keep Blocks on `public_ble_services` base, drop in the new bootloader.o, tag, deploy. That's 5 minutes of work and ships the DFU speed-up immediately.

**Defer Option B** until you have a separate window for a deliberate v0.2 → v0.3 codal jump in the Blocks editor. That migration is its own project; lumping it under "DFU speedup" risks shipping a Blocks regression that's unrelated to flash.

## Quick recipe for Option A

I haven't done this — leaving it for you to run when ready. Verbatim:

```
cd C:\GIT\Calliope\LLM\FIRMWARE\codal-microbit-v2
git checkout public_ble_services
git pull
cp ..\v3-bootloader\bootloader\microbit\armgcc\bootloader_s113.o lib\bootloader.o
git add lib\bootloader.o
git commit -m "bootloader: rebuild with BD_ADDR keep-app + MTU 247

Same binary that v0.3.5-campus-open-1 carries — fixes Web Bluetooth
reconnect after buttonless DFU (gap_address_change no longer increments
addr.addr[0]) and enables 244-byte ATT writes (MTU 247 + handler).
Verified ~4x DFU speedup on Calliope mini 3 via Web Bluetooth."
git tag v0.2.40-calliope-public-ble-1
git push origin public_ble_services v0.2.40-calliope-public-ble-1
```

Then in pxt-microbit-V5: switch `pxtarget.json:169` branch ref to the tag, rebuild, ship.

The widget-side DFU client changes (PRN=12, payload ladder [244,64,20]) are already in your mini-connection-widget patch — Blocks editor shares that widget via the campus iframe, so no change there.
