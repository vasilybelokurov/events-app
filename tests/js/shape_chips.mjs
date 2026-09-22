/* Builds the Kind chips against a stub DOM and clicks one.
 *
 * `node --check` proves app.js parses, and shape_filter.mjs proves the
 * predicate is right, but neither would notice a typo in the wiring that
 * leaves the filter panel empty.  This is the smallest DOM that buildChips()
 * needs, so a real click path is exercised without a browser.
 */
import { readFileSync } from 'node:fs';

class Node {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.attrs = {};
    this.listeners = {}; this._text = ''; this.className = ''; this.dataset = {};
  }
  appendChild(c) { this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  click() { (this.listeners.click || []).forEach((f) => f()); }
  /* Permissive on purpose: render() runs on every chip click and walks the
   * card template, and this test is about the chips, not the cards. */
  querySelector() { return new Node('div'); }
  querySelectorAll() { return []; }
  cloneNode() { return new Node(this.tagName); }
  remove() {}
  insertBefore(c) { return this.appendChild(c); }
  get content() { return { firstElementChild: new Node('article') }; }
  get classList() {
    return { add() {}, remove() {}, toggle() {}, contains: () => false };
  }
  get firstElementChild() { return this.children[0] || new Node('div'); }
  set textContent(v) { this._text = v; if (v === '') this.children = []; }
  get textContent() { return this._text || this.children.map((c) => c.textContent).join(''); }
}

const nodes = new Map();
const byId = (id) => { if (!nodes.has(id)) nodes.set(id, new Node('div')); return nodes.get(id); };
globalThis.document = {
  createElement: (t) => new Node(t),
  getElementById: byId,
  querySelectorAll: () => [],
};
globalThis.localStorage = { getItem: () => null, setItem: () => {} };
globalThis.location = { hash: '', pathname: '/' };
globalThis.history = { replaceState: () => {} };
globalThis.window = { addEventListener: () => {} };

const [appPath] = process.argv.slice(2);
let src = readFileSync(appPath, 'utf8').replace(/\ninit\(\);\s*$/, '\n');
src += '\nreturn { state, buildChips, load: (xs) => { all = xs; }, stub: () => { render = () => {}; } };\n';
// `render` is a function declaration, so it cannot be reassigned from outside;
// the stub DOM is instead complete enough for render() to run harmlessly.
const api = new Function(src)();

api.load([
  { id: 'a', title: 'Talk', start: '2026-10-01T18:30:00+01:00', careers: [], work_styles: [] },
  { id: 'b', title: 'Exhibition', start: '2026-10-01T00:00:00+01:00',
    end: '2026-12-01T00:00:00+00:00', ongoing: true, careers: [], work_styles: [] },
]);
api.buildChips();

const failures = [];
const shape = document.getElementById('shape');
const labels = shape.children.map((c) => c.textContent);
if (shape.children.length !== 3) failures.push(`expected 3 Kind chips, got ${shape.children.length}`);
if (!labels.some((l) => l.startsWith('One-off'))) failures.push(`no One-off chip: ${labels}`);
if (!labels.some((l) => l.startsWith('Runs over time'))) failures.push(`no Runs chip: ${labels}`);
if (!labels[0].startsWith('Any')) failures.push(`first chip is not Any: ${labels[0]}`);

const counts = shape.children.map((c) => c.children.at(-1)?.textContent);
if (counts.join() !== '2,1,1') failures.push(`chip counts wrong: ${counts.join()}`);

if (shape.children[0].getAttribute('aria-pressed') !== 'true') {
  failures.push('Any is not pressed by default');
}
const once = shape.children[1];
once.click();
if (api.state.shape !== 'once') failures.push(`click did not set state: ${api.state.shape}`);
if (once.getAttribute('aria-pressed') !== 'true') failures.push('clicked chip not pressed');
if (shape.children[0].getAttribute('aria-pressed') !== 'false') {
  failures.push('Any stayed pressed: the chips are not mutually exclusive');
}

if (failures.length) { console.error('FAIL\n  ' + failures.join('\n  ')); process.exit(1); }
console.log(`ok: chips ${labels.join(' | ')}`);
