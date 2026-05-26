#!/usr/bin/env node
/**
 * Puppeteer driver for the widget demo.
 *
 * Single-shot flow: opens Chrome with a persistent profile, auto-clicks the
 * page's "connect" button to surface the BLE chooser, parks until the user
 * picks their mini, then runs the 4-phase sequence and writes results.
 *
 * Persistent profile at ./.chrome-profile/ caches the user's BLE pick so
 * subsequent runs skip the chooser via navigator.bluetooth.getDevices().
 *
 * Usage:
 *   node puppeteer-driver.mjs            # full single-shot
 *   node puppeteer-driver.mjs --park     # open browser only; you drive
 *
 * Requires the Vite dev server on http://127.0.0.1:5179 (pnpm dev).
 */

import puppeteer from 'puppeteer-core';
import { mkdirSync, writeFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as wait } from 'node:timers/promises';

const HERE = dirname(fileURLToPath(import.meta.url));
const RESULTS = join(HERE, '..', 'results');
const PROFILE = join(HERE, '.chrome-profile');
const URL = 'http://127.0.0.1:5179/';

const PARK = process.argv.includes('--park');
const TS = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
const LOG_PATH = join(RESULTS, `widget-run-${TS}.log`);
mkdirSync(RESULTS, { recursive: true });
mkdirSync(PROFILE, { recursive: true });
const logChunks = [];
function logLine(s) {
  const line = `[${new Date().toISOString().slice(11, 23)}] ${s}`;
  process.stdout.write(line + '\n');
  logChunks.push(line);
}
function flushLog() {
  writeFileSync(LOG_PATH, logChunks.join('\n') + '\n');
}
process.on('exit', flushLog);
process.on('SIGINT', () => { flushLog(); process.exit(130); });

function chromePath() {
  // Standard install locations on Windows. Override via CHROME env var if
  // your install is elsewhere.
  if (process.env.CHROME) return process.env.CHROME;
  const candidates = [
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    `${process.env.LOCALAPPDATA}\\Google\\Chrome\\Application\\chrome.exe`,
  ];
  for (const p of candidates) if (existsSync(p)) return p;
  throw new Error('Chrome not found. Set CHROME=... environment variable.');
}

(async () => {
  const browser = await puppeteer.launch({
    executablePath: chromePath(),
    headless: false,
    userDataDir: PROFILE,
    defaultViewport: null,
    // Puppeteer's default 180-s protocolTimeout kills RPC calls (including
    // waitForFunction) that idle beyond it. 0 = disabled.
    protocolTimeout: 0,
    args: [
      '--enable-experimental-web-platform-features',
      // The mini-connection widget uses Web Bluetooth + WebUSB; both already
      // available on stock Chrome but we ensure flags are on.
    ],
  });
  const [page] = await browser.pages();
  page.on('console', (m) => logLine(`[page:${m.type()}] ${m.text()}`));
  page.on('pageerror', (e) => logLine(`[pageerror] ${e.message}`));
  await page.goto(URL, { waitUntil: 'networkidle2' });
  logLine(`loaded ${URL}`);

  if (PARK) {
    logLine('--park: opening browser, you drive. Ctrl+C when done.');
    await new Promise(() => {});
  }

  // Chrome's navigator.bluetooth.requestDevice() requires a *real* user
  // gesture — Puppeteer's synthetic page.click() is rejected with "Must be
  // handling a user gesture". So we highlight the button and wait for the
  // user to click it (and the chooser entry) themselves.
  logLine('');
  logLine('========================================================');
  logLine(' USER ACTION needed in the Chrome window:');
  logLine('  1) click the "connect BLE (mini picker)" button');
  logLine('  2) pick Calliope mini [zuvav] in the chooser');
  logLine('========================================================');
  logLine('');
  await page.evaluate(() => {
    const b = document.getElementById('b-connect-ble');
    if (b) { b.style.outline = '3px solid red'; b.scrollIntoView(); }
  });
  // Poll the page for a BLE connection — each evaluate() is a short RPC so
  // we don't trip the protocol timeout. Logs status changes for debugging.
  let connected = false;
  let lastStatus = '';
  const deadline = Date.now() + 600_000; // 10 min
  while (Date.now() < deadline) {
    const s = await page.evaluate(() => (window).flashTest.snapshot());
    const tag = `${s.status}|${s.transport}|${s.bleStatus ?? ''}`;
    if (tag !== lastStatus) {
      logLine(`  state: ${tag}`);
      lastStatus = tag;
    }
    if (s.transport === 'ble' || s.status === 'connected' || s.bleStatus === 'connected') {
      connected = true;
      break;
    }
    await wait(1000);
  }
  if (!connected) {
    logLine('No BLE connection within 10 min — aborting.');
    await browser.close();
    process.exit(2);
  }
  const initial = await page.evaluate(() => (window).flashTest.snapshot());
  logLine(`connected: status=${initial.status} transport=${initial.transport}`);
  await wait(2000);

  // Mode is settable via ONLY env var: 'all' (default), 'partial', 'dfu'.
  const MODE = process.env.ONLY || 'all';
  const ALL_PHASES = [
    { name: 'phase-partial-A-mod', file: 'prog-A-mod.hex', transport: 'ble' },
    { name: 'phase-partial-A',     file: 'prog-A.hex',     transport: 'ble' },
    { name: 'phase-dfu-blocks',    file: 'prog-B-blocks.hex', transport: 'ble' },
  ];
  const phases =
    MODE === 'partial' ? ALL_PHASES.filter(p => p.name.includes('partial'))
    : MODE === 'dfu'   ? ALL_PHASES.filter(p => p.name.includes('dfu'))
    : ALL_PHASES;
  logLine(`mode=${MODE} (${phases.length} phase${phases.length === 1 ? '' : 's'})`);

  // Helper: wait until status==connected AND has held for `stableMs` ms.
  // Returns true on stability, false on timeout. Logs each state change.
  async function waitForStableConnection(stableMs = 3000, timeoutMs = 60_000) {
    let stableSince = null;
    let lastTag = '';
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const s = await page.evaluate(() => (window).flashTest.snapshot());
      const tag = `${s.status}|${s.transport}|${s.bleStatus ?? ''}`;
      if (tag !== lastTag) {
        logLine(`  pre-flash state: ${tag}`);
        lastTag = tag;
        stableSince = null;
      }
      const isConnected = (s.transport === 'ble' || s.status === 'connected' || s.bleStatus === 'connected') && !s.flashInProgress;
      if (isConnected) {
        if (stableSince === null) stableSince = Date.now();
        if (Date.now() - stableSince >= stableMs) return true;
      } else {
        stableSince = null;
      }
      await wait(500);
    }
    return false;
  }

  const summary = [];
  for (const ph of phases) {
    logLine(`--- ${ph.name}: ${ph.file} via ${ph.transport} ---`);
    const stable = await waitForStableConnection();
    if (!stable) {
      logLine(`  WARN: BLE not stable before ${ph.name} — flashing anyway`);
    }
    const t0 = Date.now();
    const result = await page.evaluate(async ({ name, file, transport }) => {
      try {
        await (window).flashTest.flash(name, file, transport);
        const hist = (window).flashTest.history;
        const last = hist[hist.length - 1];
        return { ok: last.ok, elapsedMs: last.elapsedMs, error: last.error,
                 transportsSeen: last.transportsSeen, phasesSeen: last.phasesSeen };
      } catch (e) {
        return { ok: false, error: String(e?.message ?? e) };
      }
    }, ph);
    const wall = Date.now() - t0;
    logLine(`${ph.name} done in ${wall} ms; widget reported ${(result.elapsedMs / 1000).toFixed(1)} s ok=${result.ok}`);
    if (result.error) logLine(`  error: ${result.error}`);
    if (result.transportsSeen) logLine(`  transports: ${result.transportsSeen.join(',')}`);
    if (result.phasesSeen) logLine(`  phases: ${result.phasesSeen.join(',')}`);
    summary.push({ ...ph, ...result, wallMs: wall });

    // Settle between phases — device reboots into new app + widget's
    // reconnect daemon polls BLE; give it 10 s to re-acquire.
    await wait(10000);
  }

  logLine('=== summary ===');
  for (const s of summary) {
    logLine(`  ${s.name}  ${s.file}  ${s.ok ? '✓' : '✗'}  ${(s.elapsedMs / 1000).toFixed(1)} s  transports=${(s.transportsSeen || []).join('+')}`);
  }
  writeFileSync(LOG_PATH.replace(/\.log$/, '.json'), JSON.stringify(summary, null, 2));

  await browser.close();
  process.exit(0);
})().catch((e) => {
  logLine(`FATAL: ${e?.stack ?? e}`);
  process.exit(1);
});
