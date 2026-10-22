/* The Exhibitions page, driven through app.js itself.
 *
 * Checks the split between the two pages over the real events.json, then the
 * closing-soon badge and the year on a run that crosses one, which need dates
 * relative to today and so use labelled synthetic records.
 */
import { readFileSync } from 'node:fs';

const [appPath, dataPath] = process.argv.slice(2);
let src = readFileSync(appPath, 'utf8').replace(/\ninit\(\);\s*$/, '\n');
src += '\nreturn { belongsHere, closesWithin, whenLabel, sortEvents, state };\n';
const api = new Function(src)();

const failures = [];
const check = (name, cond, detail = '') => {
  if (!cond) failures.push(`${name}${detail ? ': ' + detail : ''}`);
};

const events = JSON.parse(readFileSync(dataPath, 'utf8')).events;
const exh = events.filter((e) => api.belongsHere(e, 'exhibitions'));
const rest = events.filter((e) => api.belongsHere(e, 'events'));
check('every record is on exactly one page', exh.length + rest.length === events.length);
check('both pages have something', exh.length > 0 && rest.length > 0,
      `${exh.length} / ${rest.length}`);
check('the exhibitions page holds only exhibitions', exh.every((e) => e.exhibition === true));

// Synthetic, relative to today.
const day = 86400000;
const iso = (offset) => new Date(Date.now() + offset * day).toISOString();
const run = (endIn) => ({ id: 'x', title: 'Synthetic run', ongoing: true,
                          start: iso(-30), end: iso(endIn) });
check('a run closing in five days says so', [5, 6].includes(api.closesWithin(run(5), 14)),
      String(api.closesWithin(run(5), 14)));
check('a run closing in two months does not', api.closesWithin(run(60), 14) === null);
check('a run not yet open does not',
      api.closesWithin({ ...run(5), start: iso(2) }, 14) === null);
check('a one-off is never closing', api.closesWithin({ id: 'y', title: 't',
      start: iso(1) }, 14) === null);

const label = api.whenLabel({ id: 'z', title: 't', ongoing: true,
  start: '2026-03-02T00:00:00+00:00', end: '2027-03-02T12:00:00+00:00' });
check('a run across a year shows both years', /2026/.test(label) && /2027/.test(label), label);
const same = api.whenLabel({ id: 'z', title: 't', ongoing: true,
  start: '2026-10-08T00:00:00+01:00', end: '2026-11-08T00:00:00+00:00' });
check('a run inside one year stays short', !/2026/.test(same), same);
check('the venue’s own wording wins', api.whenLabel({ id: 'w', title: 't',
  when_text: 'Until 3 Jan 2027', ongoing: true, start: iso(0),
  end: '2027-01-03T00:00:00+00:00' }) === 'Until 3 Jan 2027');

api.state.sort = 'closing';
const sorted = api.sortEvents(exh).map((e) => e.end || '9999');
check('closing soonest sorts by end', sorted.every((v, i) => i === 0 || sorted[i - 1] <= v));

if (failures.length) {
  console.error('FAIL\n  ' + failures.join('\n  '));
  process.exit(1);
}
