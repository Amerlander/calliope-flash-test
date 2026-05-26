/**
 * Convert a universal-hex (V1+V2 combined) file to a single-board V2-only
 * Intel HEX so nrf-intel-hex's MemoryMap.fromHex() can consume it.
 *
 * The widget's BLE DFU code parses the supplied hex with nrf-intel-hex,
 * which only knows record types 0x00–0x05 — universal-hex's 0x0A–0x0E
 * records make it throw "Invalid record type 0x0A". The bundled
 * blocks.hex in mini-connection-widget/src/assets/ is universal-hex, so
 * we need to pre-separate it before feeding to flashCalliopeViaBleDfu.
 *
 * Usage: node universal-to-v2.mjs INPUT.hex OUTPUT.hex
 */

import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
// Resolve the package via its absolute path inside the pnpm store —
// widget-demo doesn't list it as a direct dep so createRequire from
// widget-demo's package.json can't find it.
const UHEX_DIR = join(
    HERE, '..', 'widget-demo', 'node_modules',
    '.pnpm', '@microbit+microbit-universal-hex@0.2.2_tslib@2.8.1',
    'node_modules', '@microbit', 'microbit-universal-hex',
);
const requireFromUhex = createRequire(join(UHEX_DIR, 'package.json'));
const universalHex = requireFromUhex(UHEX_DIR);

const [, , inPath, outPath] = process.argv;
if (!inPath || !outPath) {
    console.error('usage: node universal-to-v2.mjs INPUT.hex OUTPUT.hex');
    process.exit(2);
}

const input = readFileSync(inPath, 'utf-8');
const separated = universalHex.separateUniversalHex(input);
// 0x9903 == Calliope mini v3 / micro:bit v2. Some hexes use 0x9900/0x9901.
const v2 = separated.find(
    (h) => h.boardId === 0x9903 || h.boardId === 0x9900 || h.boardId === 0x9901,
);
if (!v2) {
    console.error('No V2 board found in universal-hex. Saw board IDs:',
        separated.map((h) => `0x${h.boardId.toString(16)}`).join(', '));
    process.exit(1);
}

writeFileSync(outPath, v2.hex);
console.log(`Wrote ${outPath}: ${(v2.hex.length / 1024).toFixed(1)} KB, boardId=0x${v2.boardId.toString(16)}`);
