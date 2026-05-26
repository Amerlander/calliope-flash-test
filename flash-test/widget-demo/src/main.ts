/**
 * Minimal harness around @calliope-edu/mini-connection-widget so a
 * Puppeteer driver can time `flashCalliope` calls + capture the
 * widget's internal log stream.
 *
 * Everything that matters is hung off `window.flashTest` so the driver
 * can call into it without needing to inspect Svelte stores.
 */

import {
  initializeCalliopeConnection,
  connectCalliope,
  disconnectAndForget,
  flashCalliope,
  calliopeState,
  calliopeLog,
} from '@calliope-edu/mini-connection-widget';

const $log = document.getElementById('log') as HTMLPreElement;
const $status = document.getElementById('s-status')!;
const $transport = document.getElementById('s-transport')!;
const $phase = document.getElementById('s-phase')!;
const $elapsed = document.getElementById('s-elapsed')!;
const $name = document.getElementById('s-name')!;

interface FlashRecord {
  name: string;
  startedAt: number;
  finishedAt?: number;
  elapsedMs?: number;
  ok?: boolean;
  error?: string;
  phasesSeen: string[];
  transportsSeen: string[];
}

const history: FlashRecord[] = [];

function appendLine(s: string) {
  $log.textContent += s + '\n';
  $log.scrollTop = $log.scrollHeight;
  console.log(s);
}

// Hex cache — we load all three hexes once at startup so the buttons fire
// without a fetch round-trip in the timing window.
const hexCache: Record<string, string> = {};
async function loadHex(name: string): Promise<string> {
  if (hexCache[name]) return hexCache[name];
  const r = await fetch(`/test-hexes/${name}`);
  if (!r.ok) throw new Error(`HTTP ${r.status} fetching ${name}`);
  const text = await r.text();
  hexCache[name] = text;
  appendLine(`[boot] cached ${name} (${(text.length / 1024).toFixed(1)} KB)`);
  return text;
}

initializeCalliopeConnection();

// Mirror widget state to the page (and to `window.flashTest.state` for the
// driver).
calliopeState.subscribe((s) => {
  $status.textContent = s.status;
  $transport.textContent = s.transport ?? '(none)';
  $phase.textContent = s.flashPhase ?? '(idle)';
  const cur = history[history.length - 1];
  if (cur && !cur.finishedAt) {
    if (s.flashPhase && !cur.phasesSeen.includes(s.flashPhase)) cur.phasesSeen.push(s.flashPhase);
    if (s.transport && !cur.transportsSeen.includes(s.transport)) cur.transportsSeen.push(s.transport);
  }
});

// Mirror log entries to the visible log + console.
let lastSeen = 0;
calliopeLog.subscribe((entries) => {
  for (let i = lastSeen; i < entries.length; i++) {
    const e = entries[i];
    appendLine(`[${e.direction}${e.transport ? ':' + e.transport : ''}] ${e.text}`);
  }
  lastSeen = entries.length;
});

async function runFlash(name: string, file: string, transport?: 'usb' | 'ble') {
  const text = await loadHex(file);
  const rec: FlashRecord = { name, startedAt: performance.now(), phasesSeen: [], transportsSeen: [] };
  history.push(rec);
  $name.textContent = name;
  $elapsed.textContent = '(running…)';
  appendLine(`\n=== flash start: ${name} (${file}) transport=${transport ?? 'auto'} ===`);
  try {
    await flashCalliope(text, name, transport);
    rec.ok = true;
  } catch (e: any) {
    rec.ok = false;
    rec.error = String(e?.message ?? e);
    appendLine(`[error] ${rec.error}`);
  } finally {
    rec.finishedAt = performance.now();
    rec.elapsedMs = rec.finishedAt - rec.startedAt;
    $elapsed.textContent = `${(rec.elapsedMs / 1000).toFixed(1)} s ${rec.ok ? '✓' : '✗'}`;
    appendLine(`=== flash end: ${name} — ${rec.ok ? 'ok' : 'fail'} in ${(rec.elapsedMs / 1000).toFixed(1)} s ===`);
  }
}

// Wire up the buttons.
document.getElementById('b-connect-usb')!.addEventListener('click', () => connectCalliope('usb'));
document.getElementById('b-connect-ble')!.addEventListener('click', () => connectCalliope('ble'));
document.getElementById('b-forget')!.addEventListener('click', () => disconnectAndForget());
document.getElementById('b-flash-a-usb')!.addEventListener('click', () => runFlash('prog-A', 'prog-A.hex', 'usb'));
document.getElementById('b-flash-mod-ble')!.addEventListener('click', () => runFlash('prog-A-mod', 'prog-A-mod.hex', 'ble'));
document.getElementById('b-flash-blocks-ble')!.addEventListener('click', () => runFlash('prog-B-blocks', 'prog-B-blocks-v2.hex', 'ble'));

// Driver API — Puppeteer pokes these.
(window as any).flashTest = {
  connectUsb: () => connectCalliope('usb'),
  connectBle: () => connectCalliope('ble'),
  forget: () => disconnectAndForget(),
  flash: runFlash,
  history,
  // Snapshot of state, structurally cloneable for Puppeteer's evaluate().
  snapshot: () => {
    let s: any;
    const u = calliopeState.subscribe((v) => { s = v; });
    u();
    return {
      status: s.status,
      transport: s.transport,
      flashPhase: s.flashPhase,
      flashTransport: s.flashTransport,
      lastFlashCompleted: s.lastFlashCompleted,
      history: history.map(h => ({ ...h })),
    };
  },
};

appendLine('[boot] flash-test demo ready — driver API on window.flashTest');
