/* Captures the .ics text app.js actually produces.
 *
 * Blob and URL.createObjectURL are stubbed so the calendar body can be read
 * back; everything else is the shipped code.
 */
import { readFileSync } from 'node:fs';

let captured = '';
class Node {
  constructor(t) { this.tagName = t; this.children = []; this.attrs = {}; this.listeners = {}; this._text = ''; this.dataset = {}; }
  appendChild(c) { this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(n, f) { (this.listeners[n] ||= []).push(f); }
  click() { (this.listeners.click || []).forEach((f) => f()); }
  remove() { this.removed = true; }
  querySelector() { return new Node('div'); }
  querySelectorAll() { return []; }
  set textContent(v) { this._text = v; }
  get textContent() { return this._text; }
}
const ids = new Map();
globalThis.document = {
  createElement: () => new Node('a'),
  getElementById: (id) => { if (!ids.has(id)) ids.set(id, new Node('div')); return ids.get(id); },
  querySelectorAll: () => [],
};
globalThis.Blob = class { constructor(parts) { captured = parts.join(''); } };
/* Add the object-URL helpers to the real URL class: replacing it wholesale
 * broke safeUrl(), which parses with `new URL(...)`. */
URL.createObjectURL = () => 'blob:x';
URL.revokeObjectURL = () => {};
globalThis.localStorage = { getItem: () => null, setItem: () => {} };
globalThis.location = { hash: '', pathname: '/', href: 'https://example.test/' };
globalThis.history = { replaceState: () => {} };
globalThis.window = { addEventListener: () => {} };
const alerts = [];
globalThis.alert = (m) => alerts.push(m);

let src = readFileSync(process.argv[2], 'utf8').replace(/\ninit\(\);\s*$/, '\n');
src += '\nreturn { downloadIcs, canGoInACalendar };\n';
const api = new Function(src)();

const failures = [];
const check = (name, cond, detail = '') => { if (!cond) failures.push(name + (detail ? ': ' + detail : '')); };

const talk = { id: 'a', title: 'Evening talk', start: '2026-10-01T18:30:00+01:00',
               end: '2026-10-01T20:00:00+01:00', url: 'https://x.test/a,b' };
const standing = { id: 'b', title: 'Old Bailey public galleries',
                   start: '2026-09-22T00:00:00+01:00', anytime: true, all_day: true,
                   when_text: 'Weekdays when the court is sitting' };

check('a standing offer cannot go in a calendar', api.canGoInACalendar(standing) === false);
check('a dated talk can', api.canGoInACalendar(talk) === true);

captured = ''; alerts.length = 0;
api.downloadIcs([standing]);
check('exporting only a standing offer writes no calendar', captured === '', captured.slice(0, 60));
check('and says why', alerts.length === 1, JSON.stringify(alerts));

captured = '';
api.downloadIcs([talk, standing]);
check('the undated record is left out of a mixed export',
      (captured.match(/BEGIN:VEVENT/g) || []).length === 1,
      String((captured.match(/BEGIN:VEVENT/g) || []).length));
check('no fabricated all-day entry for it', !captured.includes('Old Bailey'));
check('the real event is present', captured.includes('SUMMARY:Evening talk'));
check('timed events keep a timed DTSTART', /DTSTART:\d{8}T\d{6}Z/.test(captured));
/* RFC 5545 types URL as URI, not TEXT: escaping its commas would corrupt the
 * link, so it is deliberately not run through icsEscape. */
check('the URL is not text-escaped', captured.includes('URL:https://x.test/a,b'));

if (process.env.DUMP) console.log(captured.replace(/\r\n/g, '\n'));
if (failures.length) { console.error('FAIL\n  ' + failures.join('\n  ')); process.exit(1); }
console.log('ok: ics export omits undated records');
