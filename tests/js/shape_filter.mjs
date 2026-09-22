/* Drives app.js's own matches() over the real events.json.
 *
 * app.js is a browser script with no exports, so it is evaluated as a function
 * body with its DOM entry point removed and the few things the filter touches
 * returned.  That keeps the test honest: it exercises the shipped code rather
 * than a copy of the predicate.
 */
import { readFileSync } from 'node:fs';

const [appPath, dataPath] = process.argv.slice(2);
let src = readFileSync(appPath, 'utf8').replace(/\ninit\(\);\s*$/, '\n');
src += '\nreturn { state, matches, runsOverTime, load: (xs) => { all = xs; } };\n';
const api = new Function(src)();

const doc = JSON.parse(readFileSync(dataPath, 'utf8'));
const events = (doc.events || []).filter((e) => e && e.id && e.title);
api.load(events);
api.state.when = 'all';               // isolate the shape filter from the date window

const failures = [];
const check = (name, cond, detail = '') => {
  if (!cond) failures.push(`${name}${detail ? ': ' + detail : ''}`);
};

const count = (shape) => {
  api.state.shape = shape;
  return events.filter(api.matches).length;
};

const any = count('all');
const once = count('once');
const running = count('running');

check('the two kinds partition the catalogue', once + running === any,
      `${once} + ${running} != ${any}`);
check('there are some of each', once > 0 && running > 0, `${once} / ${running}`);

api.state.shape = 'once';
const oneOff = events.filter(api.matches);
check('one-off excludes every run',
      oneOff.every((e) => !e.ongoing && !e.anytime),
      oneOff.filter((e) => e.ongoing || e.anytime).map((e) => e.title).slice(0, 3).join('; '));

api.state.shape = 'running';
const runs = events.filter(api.matches);
check('runs are exactly the ongoing and the undated',
      runs.every((e) => e.ongoing || e.anytime),
      runs.filter((e) => !e.ongoing && !e.anytime).map((e) => e.title).slice(0, 3).join('; '));
/* Compared against what the *other* filters already let through, not against
 * the raw file: matches() also hides cancelled events, so the raw count of
 * ongoing records is legitimately higher. */
api.state.shape = 'all';
const baseline = events.filter(api.matches);
const expected = baseline.filter(api.runsOverTime).map((e) => e.id).sort().join();
check('every run that passes the other filters is kept',
      runs.map((e) => e.id).sort().join() === expected);
check('one-off is the rest of that baseline',
      oneOff.length + runs.length === baseline.length);

// A run and a one-off that are otherwise identical must sort into the two groups.
const synthetic = [
  { id: 'a', title: 'Evening talk', start: '2026-10-01T18:30:00+01:00' },
  { id: 'b', title: 'Exhibition', start: '2026-10-01T00:00:00+01:00',
    end: '2026-12-01T00:00:00+00:00', ongoing: true },
  { id: 'c', title: 'Open gallery', start: '2026-10-01T00:00:00+01:00', anytime: true },
];
api.load(synthetic);
api.state.shape = 'once';
check('a plain dated event is one-off',
      events.length && synthetic.filter(api.matches).map((e) => e.id).join() === 'a');
api.state.shape = 'running';
check('an exhibition and an undated venue both count as runs',
      synthetic.filter(api.matches).map((e) => e.id).join() === 'b,c');
api.state.shape = 'all';
check('"Any" filters nothing out', synthetic.filter(api.matches).length === 3);

if (failures.length) {
  console.error('FAIL\n  ' + failures.join('\n  '));
  process.exit(1);
}
console.log(`ok: ${any} events, ${once} one-off, ${running} running`);
