/**
 * Build the three test hexes used by runner.py:
 *
 *   prog-A.hex         — MP runtime + main.py: display.show("A")
 *   prog-A-mod.hex     — MP runtime + main.py: display.show("a") (single-byte FS diff)
 *   prog-B-blocks.hex  — Bundled Blocks/scratch hex (DAL mismatch w/ MP → forces DFU)
 *
 * Uses @microbit/microbit-fs from the python editor's node_modules. Run from
 * the workspace root or this script's directory; the relative paths to the
 * source hexes are resolved off this file's location.
 *
 * Usage: node build-test-hexes.mjs
 */

import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { createRequire } from 'node:module';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..');

// Resolve @microbit/microbit-fs out of the python-editor's node_modules
// directly — saves us from making flash-test/ a real npm workspace.
const requireFromEditor = createRequire(
    join(ROOT, 'calliope-mini-python-editor/package.json'),
);
const { MicropythonFsHex } = requireFromEditor('@microbit/microbit-fs');

const MP_HEX = join(ROOT, 'FIRMWARE/micropython-calliope-mini-v3/src/MINI.hex');
const BLOCKS_HEX = join(ROOT, 'mini-connection-widget/src/assets/blocks.hex');
const OUT = join(HERE, 'test-hexes');

const MAIN_PY_A = `from microbit import *
display.show("A")
`;
const MAIN_PY_A_MOD = `from microbit import *
display.show("a")
`;

function build(baseHexPath, mainPy, outName) {
    const hex = readFileSync(baseHexPath, 'utf-8');
    // MicropythonFsHex wants IntelHexWithId[]: [{ hex, boardId }, ...]
    // 0x9903 = Calliope mini V3 (same as micro:bit v2 board id per calliope-edu fork)
    const fs = new MicropythonFsHex([{ hex, boardId: 0x9903 }]);
    fs.write('main.py', mainPy);
    const out = fs.getIntelHex(0x9903);
    writeFileSync(join(OUT, outName), out);
    console.log(`  ${outName} (${(out.length / 1024).toFixed(1)} KB)`);
}

console.log('Building test hexes…');
console.log(`  MP base:     ${MP_HEX}`);
console.log(`  Blocks base: ${BLOCKS_HEX}`);
console.log();

build(MP_HEX, MAIN_PY_A, 'prog-A.hex');
build(MP_HEX, MAIN_PY_A_MOD, 'prog-A-mod.hex');

// prog-B-blocks is just the bundled blocks hex — no injection needed.
const blocks = readFileSync(BLOCKS_HEX, 'utf-8');
writeFileSync(join(OUT, 'prog-B-blocks.hex'), blocks);
console.log(`  prog-B-blocks.hex (${(blocks.length / 1024).toFixed(1)} KB)`);

console.log('\nAll test hexes built.');
