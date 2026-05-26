# widget-demo

Minimal Vite + Svelte 5 page that imports `@calliope-edu/mini-connection-widget`
and exposes `flashCalliope` to a Puppeteer driver. Used to validate the
**widget's** flash path (parallel to the Python rig in `../`, which validates
the **protocol** directly).

## Layout

```
widget-demo/
├── package.json
├── vite.config.ts        — Svelte + dev-time middleware that serves ../test-hexes/
├── tsconfig.json
├── index.html
├── src/
│   └── main.ts           — wires buttons + exposes window.flashTest
├── smoke-test.mjs        — headless puppeteer page-load check (no device)
└── puppeteer-driver.mjs  — non-headless driver for the 4-phase sequence
```

The widget source is consumed directly from `../../mini-connection-widget/src/`
via a pnpm `link:` dependency — no build step required, Vite compiles the
widget's `.ts`/`.svelte` on demand.

## One-time setup

```
cd c:/GIT/Calliope/calliope-flash-test/flash-test/widget-demo
pnpm install
```

Test hexes must exist in `../test-hexes/` (build them with
`node ../build-test-hexes.mjs` from the parent dir).

## Smoke test (no device needed)

```
pnpm dev                # in one terminal
node smoke-test.mjs     # in another
```

Verifies the page loads, the widget bootstraps, and `window.flashTest` is
wired up. Exits non-zero on any console error.

## Interactive driver — one-time pairing

```
pnpm dev                            # in one terminal
node puppeteer-driver.mjs           # in another (no --auto flag)
```

Chrome opens with a persistent profile at `./.chrome-profile/`. **You** click:

1. The page's `connect (open BLE picker)` button
2. The mini in the chooser dialog (Chrome's native UI — Puppeteer cannot
   click this)

After that one pairing, the Chrome profile remembers the device. Subsequent
runs can reconnect silently via `navigator.bluetooth.getDevices()`.

Click the flash buttons yourself to time individual flashes; the driver
streams the widget's log to stdout and `../results/widget-run-<ts>.log`.

## Automated 4-phase run (after pairing)

```
node puppeteer-driver.mjs --auto
```

Runs phase 1 (USB-flash prog-A) → phase 2 (BLE flash prog-B-blocks → DFU
expected) → phase 3 (USB-flash prog-A) → phase 4 (BLE flash prog-A-mod →
partial expected), with 6 s settle between phases.

Output goes to `../results/widget-run-<ts>.log` + `.json`. The JSON has
per-phase `elapsedMs`, `transportsSeen`, `phasesSeen` for diffing against
the Python rig's CSV.

## Known limitations

- **Chrome BLE chooser is unbypassable.** The driver assumes the device is
  already paired with the Chrome profile. If the profile gets wiped or the
  device's MAC changes, the user has to re-pair.
- **Headless mode disables Web Bluetooth.** Puppeteer must run in
  non-headless mode; `smoke-test.mjs` is a build-correctness check only,
  not a flash check.
- **The widget's USB path needs Chrome's WebUSB picker too.** Same problem;
  needs one-time user click per origin per profile.

## What this validates that the Python rig doesn't

- The widget's `flashCalliope()` routing logic (`USB > BLE > USB-plug
  prompt`) — Python rig tests the protocols directly, not the routing.
- The Svelte UI state transitions (`status`, `transport`, `flashPhase`).
- The widget's `parseMicroPythonHex` and partial-flash session against
  the **same** test hexes the Python rig uses, so any divergence between
  the widget's protocol and the Python port should show up as a phase
  failing in one but not the other.
