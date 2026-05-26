/**
 * Smoke-only puppeteer test: load the page, check window.flashTest is wired
 * up, capture any console errors, then close.
 *
 * Does NOT need a device. Verifies the build is wired and the widget
 * initialises without crashing.
 */

import puppeteer from 'puppeteer-core';
import { existsSync } from 'node:fs';
import { setTimeout as wait } from 'node:timers/promises';

const URL = 'http://127.0.0.1:5179/';

function chromePath() {
  const candidates = [
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  ];
  for (const p of candidates) if (existsSync(p)) return p;
  throw new Error('Chrome not found');
}

const errs = [];
const browser = await puppeteer.launch({
  executablePath: chromePath(),
  headless: 'new',
  args: ['--no-sandbox'],
});
const page = await browser.newPage();
page.on('console', (m) => {
  const t = m.type();
  console.log(`[page:${t}] ${m.text()}`);
  if (t === 'error') errs.push(m.text());
});
page.on('pageerror', (e) => {
  console.log(`[pageerror] ${e.message}`);
  errs.push(e.message);
});
await page.goto(URL, { waitUntil: 'networkidle2', timeout: 30000 });
await wait(2000);

const ready = await page.evaluate(() => {
  return {
    hasFlashTest: typeof (window).flashTest === 'object',
    methods: Object.keys((window).flashTest || {}),
    title: document.title,
    buttonsPresent: ['b-connect', 'b-flash-a', 'b-flash-mod', 'b-flash-blocks'].every(id => !!document.getElementById(id)),
  };
});
console.log('\n--- smoke result ---');
console.log(ready);
console.log(`console errors: ${errs.length}`);
if (errs.length) for (const e of errs) console.log(`  ! ${e}`);

await browser.close();
process.exit(errs.length === 0 && ready.hasFlashTest && ready.buttonsPresent ? 0 : 1);
