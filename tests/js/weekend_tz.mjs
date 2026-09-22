/* The weekend filter must mean London's weekend, whoever is looking.
 * Run under TZ=America/Chicago and TZ=Asia/Tokyo as well as Europe/London.
 */
import { readFileSync } from 'node:fs';
globalThis.document = { createElement: () => ({}), getElementById: () => null, querySelectorAll: () => [] };
globalThis.localStorage = { getItem: () => null, setItem: () => {} };
globalThis.location = { hash: '', pathname: '/', href: 'https://x.test/' };
globalThis.history = { replaceState: () => {} };
globalThis.window = { addEventListener: () => {} };

let src = readFileSync(process.argv[2], 'utf8').replace(/\ninit\(\);\s*$/, '\n');
src += '\nreturn { isLondonWeekend, state, matches, load: (xs) => { all = xs; } };\n';
const api = new Function(src)();

const cases = [
  ['Sat 09:00 London', '2026-10-03T09:00:00+01:00', true],
  ['Sun 00:30 London', '2026-10-04T00:30:00+01:00', true],   // Sat evening in Chicago
  ['Fri 21:00 London', '2026-10-02T21:00:00+01:00', false],  // Sat already in Tokyo
  ['Mon 12:00 London', '2026-10-05T12:00:00+01:00', false],
];
const failures = [];
for (const [name, iso, want] of cases) {
  const got = api.isLondonWeekend(new Date(iso));
  if (got !== want) failures.push(`${name}: expected ${want}, got ${got} (TZ=${process.env.TZ || 'system'})`);
}

// and through the filter itself
api.state.when = 'all';
api.state.flags = new Set(['weekend']);
const events = cases.map(([name, iso], i) => ({ id: String(i), title: name, start: iso }));
api.load(events);
const kept = events.filter(api.matches).map((e) => e.title);
const want = cases.filter(([, , w]) => w).map(([n]) => n);
if (kept.join('|') !== want.join('|')) {
  failures.push(`filter kept ${JSON.stringify(kept)}, expected ${JSON.stringify(want)}`);
}

if (failures.length) { console.error('FAIL\n  ' + failures.join('\n  ')); process.exit(1); }
console.log(`ok: London weekend under TZ=${process.env.TZ || 'system'}`);
