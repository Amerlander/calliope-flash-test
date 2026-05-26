#!/usr/bin/env node
/**
 * Snapshot the running widget-demo:
 *   - opens Chrome with the cached profile (BLE pair persists)
 *   - waits for the widget to reconnect to the mini
 *   - captures `window.flashTest.snapshot()` + the relevant log lines
 *   - writes <key>.json into ./results/
 *
 * Usage: node widget-snapshot.mjs <key>
 */
import puppeteer from 'puppeteer-core';
import { mkdirSync, writeFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as wait } from 'node:timers/promises';

const HERE = dirname(fileURLToPath(import.meta.url));
const WD_PROFILE = join(HERE, '..', 'flash-test', 'widget-demo', '.chrome-profile');
const RESULTS = join(HERE, 'results');
const URL = 'http://127.0.0.1:5179/';

const KEY = process.argv[2];
if (!KEY) { console.error('usage: widget-snapshot.mjs <key>'); process.exit(2); }
mkdirSync(RESULTS, { recursive: true });

function chromePath() {
  if (process.env.CHROME) return process.env.CHROME;
  const c = [
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    `${process.env.LOCALAPPDATA}\\Google\\Chrome\\Application\\chrome.exe`,
  ];
  for (const p of c) if (existsSync(p)) return p;
  throw new Error('Chrome not found. Set CHROME=…');
}

const logs = [];
function logLine(s) {
  const t = `[${new Date().toISOString().slice(11, 23)}] ${s}`;
  process.stdout.write(t + '\n');
  logs.push(t);
}

(async () => {
  const browser = await puppeteer.launch({
    executablePath: chromePath(),
    headless: false,
    userDataDir: WD_PROFILE,
    defaultViewport: null,
    protocolTimeout: 0,
    args: ['--enable-experimental-web-platform-features'],
  });
  const [page] = await browser.pages();
  const consoleLogs = [];
  page.on('console', (m) => {
    const line = `[${m.type()}] ${m.text()}`;
    consoleLogs.push(line);
    logLine(line);
  });
  page.on('pageerror', (e) => {
    consoleLogs.push(`[pageerror] ${e.message}`);
    logLine(`[pageerror] ${e.message}`);
  });

  await page.goto(URL, { waitUntil: 'networkidle2' });
  logLine(`loaded ${URL}`);

  // Wait up to 60s for widget to (re-)connect via cached profile
  const deadline = Date.now() + 60_000;
  let last = '';
  let connected = false;
  while (Date.now() < deadline) {
    const s = await page.evaluate(() => window.flashTest?.snapshot());
    const tag = `${s?.status}|${s?.transport ?? '-'}`;
    if (tag !== last) { logLine(`state: ${tag}`); last = tag; }
    if (s?.status === 'connected' || s?.transport === 'ble') { connected = true; break; }
    await wait(1000);
  }

  if (!connected) {
    logLine('NOT CONNECTED within 60s — try clicking connect manually');
    // Give the user a chance to click manually
    const deadline2 = Date.now() + 120_000;
    while (Date.now() < deadline2) {
      const s = await page.evaluate(() => window.flashTest?.snapshot());
      if (s?.status === 'connected' || s?.transport === 'ble') { connected = true; break; }
      await wait(1000);
    }
  }

  // Allow services to settle
  await wait(2000);

  // Read widget state + try to enumerate BLE services via the widget's
  // BleakClient (the widget doesn't directly expose services, but we can
  // peek into navigator.bluetooth.getDevices() and gatt.getPrimaryServices())
  const services = await page.evaluate(async () => {
    try {
      const devs = await navigator.bluetooth.getDevices();
      const out = [];
      for (const d of devs) {
        if (!d.gatt?.connected) continue;
        const ss = await d.gatt.getPrimaryServices();
        const serviceUuids = [];
        for (const s of ss) {
          const chars = await s.getCharacteristics();
          serviceUuids.push({
            uuid: s.uuid,
            characteristics: chars.map(c => ({ uuid: c.uuid, properties: c.properties })),
          });
        }
        out.push({ name: d.name, id: d.id, services: serviceUuids });
      }
      return out;
    } catch (e) {
      return { error: String(e?.message ?? e) };
    }
  });

  const snapshot = await page.evaluate(() => window.flashTest?.snapshot());
  const out = {
    key: KEY,
    when: new Date().toISOString(),
    connected,
    snapshot,
    services,
    consoleLogs,
  };
  writeFileSync(join(RESULTS, `widget-${KEY}.json`), JSON.stringify(out, null, 2));
  logLine(`saved results/widget-${KEY}.json`);

  await browser.close();
})();
