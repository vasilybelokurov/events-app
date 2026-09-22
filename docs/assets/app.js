/* What's On — client-side filtering and sorting over docs/data/events.json.
 *
 * Everything the collector produced is untrusted text: it is inserted with
 * textContent, never innerHTML, and every URL is checked against an allowlist
 * of schemes before it reaches an href.
 *
 * State lives in two places:
 *   - the URL hash, so a filtered view can be shared or bookmarked;
 *   - localStorage, for the per-person "saved" and "not interested" lists.
 */
'use strict';

const DATA_URL = 'data/events.json';
const STORE = 'whatson.v1';

const state = {
  q: '',
  age: 14,
  sort: 'date',
  view: 'cards',
  when: '90',           // 7 | 30 | 90 | all | custom
  shape: 'all',         // all | once | running
  from: '',
  to: '',
  cities: new Set(),
  careers: new Set(),
  styles: new Set(),
  flags: new Set(),     // free, eligible, saved, online, weekend
};

let all = [];
let meta = {};
let saved = new Set();
let hidden = new Set();

/* ---------- storage ---------- */

function loadStore() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORE) || '{}');
    saved = new Set(raw.saved || []);
    hidden = new Set(raw.hidden || []);
  } catch (e) { /* private browsing or blocked storage: carry on */ }
}

function saveStore() {
  try {
    localStorage.setItem(STORE, JSON.stringify({
      saved: [...saved], hidden: [...hidden],
    }));
  } catch (e) { /* ignore */ }
}

/* ---------- helpers ---------- */

const SAFE_SCHEME = /^https?:$/;

function safeUrl(url) {
  if (!url) return null;
  try {
    const u = new URL(url, location.href);
    return SAFE_SCHEME.test(u.protocol) ? u.href : null;
  } catch (e) { return null; }
}

const DAY = 86400000;

function startOfToday() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d;
}

function parseDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return isNaN(d) ? null : d;
}

const FMT_DAY = new Intl.DateTimeFormat('en-GB', {
  weekday: 'short', day: 'numeric', month: 'short', timeZone: 'Europe/London',
});
const FMT_TIME = new Intl.DateTimeFormat('en-GB', {
  hour: '2-digit', minute: '2-digit', timeZone: 'Europe/London',
});
const FMT_MONTH = new Intl.DateTimeFormat('en-GB', {
  month: 'long', year: 'numeric', timeZone: 'Europe/London',
});

function whenLabel(e) {
  if (e.when_text) return e.when_text;
  const s = parseDate(e.start);
  if (!s) return 'Date to be confirmed';
  const end = parseDate(e.end);
  if (e.ongoing && end && end - s > DAY) {
    return `${FMT_DAY.format(s)} – ${FMT_DAY.format(end)}`;
  }
  let out = FMT_DAY.format(s);
  if (!e.all_day) out += `, ${FMT_TIME.format(s)}`;
  if (end && !e.all_day && end - s < DAY) out += `–${FMT_TIME.format(end)}`;
  return out;
}

/* One evening, or something you can catch across a window of time.
 *
 * Two different records mean the second thing: a multi-day run (`ongoing`,
 * typically an exhibition) and a standing offer with no fixed date
 * (`anytime` — a public gallery, a weekly tour). They are the same answer to
 * the question a person actually asks, which is whether they have to be
 * somewhere at 18:30 on Tuesday or can go any time over the next six weeks.
 *
 * This is deliberately not a guess about recurrence: an ICS `RRULE` is never
 * expanded, so a weekly series still arrives as one dated occurrence and is
 * counted as one. The site does not claim to know what it has not been told.
 */
function runsOverTime(e) {
  return !!(e.ongoing || e.anytime);
}

function isRunningNow(e) {
  if (e.anytime) return true;
  const s = parseDate(e.start);
  if (!s) return false;
  const end = parseDate(e.end);
  const now = Date.now();
  return s.getTime() <= now && !!end && end.getTime() >= now;
}

function eligibility(e, age) {
  if (e.age_min != null && age < e.age_min) return 'excluded';
  if (e.age_max != null && age > e.age_max) return 'excluded';
  const aud = e.audiences || [];
  if (aud.length === 1 && aud[0] === 'children' && age > 12) return 'excluded';
  if (e.age_min != null || e.age_max != null) return 'eligible';
  if (aud.includes('teens') && age >= 13 && age <= 17) return 'eligible';
  if (aud.includes('families')) return 'eligible';
  return 'unknown';
}

/* A hand-written claim is only as good as the date a human last checked it.
 * Three states, mirroring collector/verify.py: verified recently, expired, or
 * never checked. A resolving link is not a verification. */
function verificationState(e) {
  if (!e.provenance && !e.verified_on) return null;   // not a curated claim
  if (!e.verified_on) return { state: 'never', label: 'unverified claim' };
  const when = parseDate(e.verified_on);
  if (!when) return { state: 'never', label: 'unverified claim' };
  const days = Math.floor((Date.now() - when) / DAY);
  const limit = meta.verify_after_days || 90;
  if (days > limit) {
    return { state: 'expired', label: `needs re-checking (${days}d)` };
  }
  return { state: 'ok', label: `checked ${days}d ago` };
}

function isStale(e) {
  const seen = parseDate(e.last_seen);
  if (!seen || !meta.stale_after_hours) return false;
  return (Date.now() - seen) > meta.stale_after_hours * 3600 * 1000;
}

/* ---------- filtering ---------- */

function windowEnd() {
  if (state.when === 'all') return null;
  if (state.when === 'custom') return state.to ? new Date(state.to + 'T23:59:59') : null;
  return new Date(startOfToday().getTime() + Number(state.when) * DAY);
}

function windowStart() {
  if (state.when === 'custom' && state.from) return new Date(state.from + 'T00:00:00');
  return startOfToday();
}

function matches(e) {
  if (hidden.has(e.id) && !state.flags.has('saved')) return false;
  if (e.status && e.status !== 'scheduled' && !state.flags.has('all-status')) return false;

  const from = windowStart(), to = windowEnd();
  const s = parseDate(e.start), end = parseDate(e.end) || s;
  if (s) {
    if (to && s > to) return false;
    if (end && end < from) return false;
  }

  if (state.shape === 'once' && runsOverTime(e)) return false;
  if (state.shape === 'running' && !runsOverTime(e)) return false;

  if (state.cities.size) {
    const key = e.online ? 'Online' : (e.city || 'Other');
    if (!state.cities.has(key)) return false;
  }

  if (state.careers.size) {
    const cs = e.careers || [];
    if (!cs.some((c) => state.careers.has(c))) return false;
  }

  if (state.styles.size) {
    const ws = e.work_styles || [];
    if (!ws.some((w) => state.styles.has(w))) return false;
  }

  const elig = eligibility(e, state.age);
  if (elig === 'excluded') return false;
  if (state.flags.has('eligible') && elig !== 'eligible') return false;
  if (state.flags.has('free') && e.is_free !== true) return false;
  if (state.flags.has('saved') && !saved.has(e.id)) return false;
  if (state.flags.has('weekend') && s) {
    const d = s.getDay();
    if (d !== 0 && d !== 6) return false;
  }

  if (state.q) {
    const hay = [e.title, e.summary, e.venue_name, e.city, e.source_name,
      (e.careers || []).join(' '), (e.work_styles || []).join(' '),
      (e.topics || []).join(' ')]
      .filter(Boolean).join(' ').toLowerCase();
    for (const term of state.q.toLowerCase().split(/\s+/).filter(Boolean)) {
      if (!hay.includes(term)) return false;
    }
  }
  return true;
}

function sortEvents(list) {
  const by = state.sort;
  const cmp = {
    date: (a, b) => (a.start || '9999').localeCompare(b.start || '9999'),
    title: (a, b) => a.title.localeCompare(b.title, 'en-GB'),
    price: (a, b) => (a.price_from ?? (a.is_free ? 0 : 1e9)) - (b.price_from ?? (b.is_free ? 0 : 1e9)),
    city: (a, b) => (a.city || '').localeCompare(b.city || '') ||
                    (a.start || '').localeCompare(b.start || ''),
    source: (a, b) => a.source_name.localeCompare(b.source_name) ||
                      (a.start || '').localeCompare(b.start || ''),
  }[by] || ((a, b) => 0);
  return [...list].sort(cmp);
}

/* ---------- rendering ---------- */

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function badge(parent, cls, text) {
  if (!text) return;
  parent.appendChild(el('span', 'badge ' + cls, text));
}

function renderCard(e) {
  const node = document.getElementById('card-tpl').content.firstElementChild.cloneNode(true);
  node.dataset.id = e.id;
  if (saved.has(e.id)) node.classList.add('saved');

  node.querySelector('.when').textContent = whenLabel(e);
  const badges = node.querySelector('.badges');
  const elig = eligibility(e, state.age);
  if (elig === 'eligible') badge(badges, 'age-eligible', e.age_text || `Suits ${state.age}`);
  else badge(badges, 'age-unknown', 'No stated age limit');
  /* Audience labels combine as a union, so a show tagged "Children 12 and
   * under, Families" admits a 14-year-old rather than excluding her. That is
   * the right rule for eligibility but loses the pitch, so say it plainly
   * instead of filtering her out of something she may still enjoy. */
  const aud = e.audiences || [];
  if (state.age >= 13 && aud.includes('children') && !aud.includes('teens')) {
    badge(badges, 'age-unknown', 'aimed at younger children');
  }
  if (e.anytime) badge(badges, '', 'No fixed date');
  else if (e.ongoing) badge(badges, '', 'Runs over several days');
  if (e.is_free === true) badge(badges, 'free', 'Free');
  if (e.online) badge(badges, '', 'Online option');
  if (e.status && e.status !== 'scheduled') badge(badges, 'alert', e.status.replace('_', ' '));
  if (e.link_status && e.link_status !== 200) badge(badges, 'alert', 'link ' + e.link_status);
  if (e.link_warning) badge(badges, 'alert', e.link_warning);
  if (isStale(e)) badge(badges, 'alert', 'not reconfirmed recently');
  const ver = verificationState(e);
  if (ver && ver.state !== 'ok') badge(badges, 'age-unknown', ver.label);
  else if (ver) badge(badges, 'verified', ver.label);

  const link = node.querySelector('.title');
  link.textContent = e.title;
  const href = safeUrl(e.url);
  if (href) { link.href = href; link.target = '_blank'; }

  const bits = [e.venue_name, e.city].filter(Boolean);
  if (e.price_text) bits.push(e.price_text);
  if (e.time_text && !e.all_day) bits.push(e.time_text);
  node.querySelector('.meta').textContent = bits.join(' · ');

  node.querySelector('.summary').textContent = e.summary || '';

  const tags = node.querySelector('.tags');
  (e.careers || []).forEach((c) => tags.appendChild(el('span', 'tag', c)));
  (e.work_styles || []).forEach((w) => tags.appendChild(el('span', 'tag style', w)));
  tags.appendChild(el('span', 'tag source', e.source_name));

  const bookHref = safeUrl(e.booking_url);
  const book = node.querySelector('.book');
  if (bookHref) { book.href = bookHref; book.target = '_blank'; }
  else book.remove();

  const saveBtn = node.querySelector('.save');
  saveBtn.setAttribute('aria-pressed', String(saved.has(e.id)));
  saveBtn.textContent = saved.has(e.id) ? 'Saved' : 'Save';
  saveBtn.addEventListener('click', () => {
    if (saved.has(e.id)) saved.delete(e.id); else saved.add(e.id);
    saveStore(); render();
  });
  node.querySelector('.hide').addEventListener('click', () => {
    hidden.add(e.id); saved.delete(e.id); saveStore(); render();
  });
  const cal = node.querySelector('.cal');
  if (canGoInACalendar(e)) cal.addEventListener('click', () => downloadIcs([e]));
  else cal.remove();
  return node;
}

function renderTable(list) {
  const scroll = el('div', 'table-scroll');
  const t = el('table');
  const head = el('tr');
  ['When', 'Event', 'Where', 'Age', 'Price', 'Subject', 'Ways of working', 'Source']
    .forEach((h) => {
    head.appendChild(el('th', null, h));
  });
  t.appendChild(el('thead')).appendChild(head);
  const body = el('tbody');
  list.forEach((e) => {
    const tr = el('tr');
    tr.appendChild(el('td', null, whenLabel(e)));
    const td = el('td');
    const a = el('a', null, e.title);
    const href = safeUrl(e.url);
    if (href) { a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer'; }
    td.appendChild(a);
    tr.appendChild(td);
    tr.appendChild(el('td', null, [e.venue_name, e.city].filter(Boolean).join(', ')));
    tr.appendChild(el('td', null, e.age_text || '—'));
    tr.appendChild(el('td', null, e.price_text || (e.is_free ? 'Free' : '—')));
    tr.appendChild(el('td', null, (e.careers || []).join(', ')));
    tr.appendChild(el('td', null, (e.work_styles || []).join(', ')));
    tr.appendChild(el('td', null, e.source_name));
    body.appendChild(tr);
  });
  t.appendChild(body);
  scroll.appendChild(t);
  return scroll;
}

function render() {
  const shown = sortEvents(all.filter(matches));
  const results = document.getElementById('results');
  results.textContent = '';

  document.getElementById('count').textContent =
    `${shown.length} of ${all.length} events` +
    (hidden.size ? ` · ${hidden.size} hidden` : '') +
    (saved.size ? ` · ${saved.size} saved` : '');

  if (!shown.length) {
    results.appendChild(el('p', 'empty', 'Nothing matches these filters. Try widening the date window or clearing filters.'));
    updateChipCounts();
    return;
  }

  if (state.view === 'table') {
    results.appendChild(renderTable(shown));
  } else if (state.sort === 'date') {
    // A six-month exhibition that opened in January belongs under "On now",
    // not under a heading for a month that has already passed.
    const now = Date.now();
    const onNow = shown.filter(isRunningNow);
    const upcoming = shown.filter((e) => !isRunningNow(e));
    if (onNow.length) {
      results.appendChild(el('h2', 'month', 'On now'));
      const grid = el('div', 'cards');
      onNow.forEach((e) => grid.appendChild(renderCard(e)));
      results.appendChild(grid);
    }
    let month = null, grid = null;
    upcoming.forEach((e) => {
      const d = parseDate(e.start);
      const label = d ? FMT_MONTH.format(d) : 'Dates to confirm';
      if (label !== month) {
        month = label;
        results.appendChild(el('h2', 'month', label));
        grid = el('div', 'cards');
        results.appendChild(grid);
      }
      grid.appendChild(renderCard(e));
    });
  } else {
    const grid = el('div', 'cards');
    shown.forEach((e) => grid.appendChild(renderCard(e)));
    results.appendChild(grid);
  }
  updateChipCounts();
  writeHash();
}

/* ---------- chips ---------- */

function chip(container, label, key, set, count) {
  const b = el('button', 'chip');
  b.type = 'button';
  b.textContent = label;
  if (count != null) {
    const n = el('span', 'n', count);
    b.appendChild(n);
  }
  b.setAttribute('aria-pressed', String(set.has(key)));
  b.dataset.key = key;
  b.addEventListener('click', () => {
    if (set.has(key)) set.delete(key); else set.add(key);
    b.setAttribute('aria-pressed', String(set.has(key)));
    render();
  });
  container.appendChild(b);
  return b;
}

function buildChips() {
  /* The From/To inputs do nothing unless "Custom dates" is chosen -- the
   * window comes from the chip otherwise -- and left on show they made the
   * When box far taller than the three beside it, opening a band of empty
   * space across the filter row. */
  const dates = document.getElementById('dates');
  const showDates = () => { if (dates) dates.hidden = state.when !== 'custom'; };

  const when = document.getElementById('when');
  when.textContent = '';
  [['7', 'Next 7 days'], ['30', 'Next 30 days'], ['90', 'Next 3 months'],
   ['all', 'Everything'], ['custom', 'Custom dates']].forEach(([k, label]) => {
    const b = el('button', 'chip');
    b.type = 'button';
    b.textContent = label;
    b.setAttribute('aria-pressed', String(state.when === k));
    b.addEventListener('click', () => {
      state.when = k;
      [...when.children].forEach((c) => c.setAttribute('aria-pressed', 'false'));
      b.setAttribute('aria-pressed', 'true');
      showDates();
      render();
    });
    when.appendChild(b);
  });
  showDates();

  const shape = document.getElementById('shape');
  shape.textContent = '';
  const shapeCounts = {
    all: all.length,
    once: all.filter((e) => !runsOverTime(e)).length,
    running: all.filter(runsOverTime).length,
  };
  [['all', 'Any'], ['once', 'One-off'], ['running', 'Runs over time']]
    .forEach(([k, label]) => {
      const b = el('button', 'chip');
      b.type = 'button';
      b.textContent = label;
      b.appendChild(el('span', 'n', shapeCounts[k]));
      b.dataset.key = k;
      b.setAttribute('aria-pressed', String(state.shape === k));
      b.addEventListener('click', () => {
        state.shape = k;
        [...shape.children].forEach((c) => c.setAttribute('aria-pressed', 'false'));
        b.setAttribute('aria-pressed', 'true');
        render();
      });
      shape.appendChild(b);
    });

  const cityCounts = new Map();
  all.forEach((e) => {
    const key = e.online ? 'Online' : (e.city || 'Other');
    cityCounts.set(key, (cityCounts.get(key) || 0) + 1);
  });
  const cities = document.getElementById('cities');
  cities.textContent = '';
  [...cityCounts.entries()].sort((a, b) => b[1] - a[1])
    .forEach(([c, n]) => chip(cities, c, c, state.cities, n));

  const flags = document.getElementById('flags');
  flags.textContent = '';
  /* Named for what it does: ineligible events are always excluded, so this
   * only controls whether events with *unknown* eligibility are shown. */
  [['eligible', 'Only confirmed suitable'], ['free', 'Free only'],
   ['weekend', 'Weekends only'], ['saved', 'Saved only'],
   ['all-status', 'Include cancelled']].forEach(([k, label]) => {
    chip(flags, label, k, state.flags);
  });

  const careerCounts = new Map();
  all.forEach((e) => (e.careers || []).forEach((c) => {
    careerCounts.set(c, (careerCounts.get(c) || 0) + 1);
  }));
  const careers = document.getElementById('careers');
  careers.textContent = '';
  [...careerCounts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .forEach(([c, n]) => chip(careers, c, c, state.careers, n));

  const styleCounts = new Map();
  all.forEach((e) => (e.work_styles || []).forEach((w) => {
    styleCounts.set(w, (styleCounts.get(w) || 0) + 1);
  }));
  const styles = document.getElementById('styles');
  styles.textContent = '';
  [...styleCounts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .forEach(([w, n]) => chip(styles, w, w, state.styles, n));
}

function updateChipCounts() {
  const base = all.filter(matches);
  const counts = new Map();
  base.forEach((e) => (e.careers || []).forEach((c) => counts.set(c, (counts.get(c) || 0) + 1)));
  document.querySelectorAll('#careers .chip').forEach((b) => {
    const n = b.querySelector('.n');
    if (n) n.textContent = counts.get(b.dataset.key) || 0;
  });
  const styleCounts = new Map();
  base.forEach((e) => (e.work_styles || []).forEach((w) => {
    styleCounts.set(w, (styleCounts.get(w) || 0) + 1);
  }));
  document.querySelectorAll('#styles .chip').forEach((b) => {
    const n = b.querySelector('.n');
    if (n) n.textContent = styleCounts.get(b.dataset.key) || 0;
  });
  const restore = document.getElementById('restore');
  if (restore) {
    restore.hidden = hidden.size === 0;
    restore.textContent = `Restore ${hidden.size} hidden`;
  }
}

/* ---------- calendar export ---------- */

function icsEscape(s) {
  return String(s || '').replace(/\\/g, '\\\\').replace(/;/g, '\\;')
    .replace(/,/g, '\\,').replace(/\r?\n/g, '\\n');
}

function icsStamp(d) {
  return d.toISOString().replace(/[-:]/g, '').replace(/\.\d{3}/, '');
}

function icsDate(d) {
  // Date-only value in London terms, for all-day entries.
  return new Intl.DateTimeFormat('en-CA', {
    year: 'numeric', month: '2-digit', day: '2-digit', timeZone: 'Europe/London',
  }).format(d).replace(/-/g, '');
}

/* A standing offer -- a gallery open on weekdays, a weekly tour -- has no date.
 * The record carries today's date as a sort key so the date window can place
 * it, but that is a placeholder, not a claim, and writing it into a calendar
 * turned "Old Bailey public galleries" into an appointment for this afternoon.
 * There is no honest VEVENT for "open Monday to Friday", so there is none. */
function canGoInACalendar(e) {
  return !e.anytime && !!parseDate(e.start);
}

function downloadIcs(list) {
  const datable = list.filter(canGoInACalendar);
  if (!datable.length) {
    alert(list.length === 1
      ? 'This one has no fixed date — see "When" on the card and the venue\u2019s page.'
      : 'None of these has a fixed date, so there is nothing to put in a calendar.');
    return;
  }
  const skipped = list.length - datable.length;
  const lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//events_app//EN', 'CALSCALE:GREGORIAN'];
  datable.forEach((e) => {
    const s = parseDate(e.start);
    if (!s) return;
    const end = parseDate(e.end) || new Date(s.getTime() + 2 * 3600 * 1000);
    lines.push('BEGIN:VEVENT');
    lines.push('UID:' + icsEscape(e.id) + '@events_app');
    lines.push('DTSTAMP:' + icsStamp(new Date()));
    if (e.all_day || e.ongoing || e.anytime) {
      // An exhibition is an all-day range, not a 24/7 appointment: a timed
      // DTSTART/DTEND here would block out months of the viewer's calendar.
      // DTEND is exclusive in RFC 5545, hence the extra day.
      lines.push('DTSTART;VALUE=DATE:' + icsDate(s));
      lines.push('DTEND;VALUE=DATE:' + icsDate(new Date(end.getTime() + DAY)));
    } else {
      lines.push('DTSTART:' + icsStamp(s));
      lines.push('DTEND:' + icsStamp(end));
    }
    lines.push('SUMMARY:' + icsEscape(e.title));
    lines.push('LOCATION:' + icsEscape([e.venue_name, e.city].filter(Boolean).join(', ')));
    const desc = [e.summary, e.price_text, e.age_text, safeUrl(e.url)].filter(Boolean).join('\n');
    lines.push('DESCRIPTION:' + icsEscape(desc));
    const u = safeUrl(e.url);
    if (u) lines.push('URL:' + u);
    lines.push('END:VEVENT');
  });
  lines.push('END:VCALENDAR');
  const blob = new Blob([lines.join('\r\n')], { type: 'text/calendar;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = datable.length === 1 ? 'event.ics' : 'whats-on.ics';
  a.click();
  URL.revokeObjectURL(a.href);
  if (skipped) {
    const n = document.getElementById('count');
    if (n) {
      n.textContent = `${datable.length} exported; ${skipped} with no fixed date left out.`;
    }
  }
}

/* ---------- freshness & health ---------- */

/* Adapter names are implementation detail; the panel says what the source
 * actually is, and what the reader would be opening. */
const KIND_LABEL = {
  drupal_jsonapi: 'JSON:API',
  html_css: 'HTML listing',
  ics: 'iCalendar feed',
  jsonld: 'schema.org markup',
  venue: 'venue page',
  curated: 'hand-written',
};

const FEED_LABEL = {
  drupal_jsonapi: 'the JSON:API response',
  html_css: 'the listing page',
  ics: 'the .ics feed',
  jsonld: 'the page markup',
  venue: 'the venue page',
  curated: 'the YAML file',
};

function linkCell(url, text) {
  const td = el('td');
  const href = safeUrl(url);
  if (href) {
    const a = el('a', null, text || href);
    a.href = href;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    td.appendChild(a);
  } else {
    td.textContent = text || '—';
  }
  return td;
}

/* Every source is listed here with the links needed to check it: the venue's
 * own listing, the raw feed the collector reads, and the terms it is used
 * under. Nothing reaches the page from a source absent from this table. */
function renderHealth() {
  const box = document.getElementById('sources');
  box.textContent = '';
  const t = el('table');
  const head = el('tr');
  ['Source', 'Kind', 'Events', 'Status', 'Last refreshed', 'Feed', 'Terms']
    .forEach((h) => head.appendChild(el('th', null, h)));
  t.appendChild(el('thead')).appendChild(head);
  const body = el('tbody');
  (meta.sources || []).forEach((s) => {
    const tr = el('tr');
    tr.appendChild(linkCell(s.homepage, s.name));
    tr.appendChild(el('td', null, KIND_LABEL[s.kind] || s.kind));

    /* The events cell says how the number was arrived at, which differs by
     * source: a scraped source reports what its detail pages added, and a
     * hand-written one reports how many of its claims a person has actually
     * confirmed — the only thing by which that source can be judged. */
    const counts = el('td');
    counts.appendChild(el('span', null, String(s.count)));
    if (s.detail_enabled) {
      counts.appendChild(el('span', 'sub',
        `+${s.detail_ages_added || 0} ages from detail pages`
        + (s.detail_failed ? `, ${s.detail_failed} page(s) failed` : '')));
    }
    if (s.unverified_entries != null) {
      const checked = s.verified_entries || 0;
      counts.appendChild(el('span', s.unverified_entries ? 'sub warn' : 'sub',
        `${checked} checked by hand, ${s.unverified_entries} unverified`));
    }
    tr.appendChild(counts);

    const st = el('td', s.status === 'ok' ? 'status-ok' : 'status-bad',
      s.status + (s.error ? ' — ' + s.error : '') +
      (s.carried_over ? ` (${s.carried_over} kept from last good run)` : ''));
    tr.appendChild(st);
    const ls = parseDate(s.last_success);
    tr.appendChild(el('td', null, ls ? ls.toLocaleString('en-GB') : 'never'));

    /* Named for what it actually is, and always a real link: a source whose
     * data cannot be opened is not "accessible", whatever the table says. */
    tr.appendChild(linkCell(s.raw_url || s.verify_url, FEED_LABEL[s.kind] || 'source data'));

    const terms = s.terms || '';
    tr.appendChild(linkCell(terms.startsWith('http') ? terms : null,
                            terms.startsWith('http') ? 'terms' : (terms || '—')));
    body.appendChild(tr);
  });
  t.appendChild(body);
  box.appendChild(t);

  const f = document.getElementById('freshness');
  const gen = parseDate(meta.generated_at);
  const hours = gen ? (Date.now() - gen) / 3600000 : Infinity;
  const stale = hours > (meta.stale_after_hours || 48);
  f.classList.toggle('stale', stale);
  f.textContent = gen
    ? `${meta.event_count} events · last collected ${gen.toLocaleString('en-GB')}`
      + (stale ? ' — this is out of date; the collector has not run recently.' : '')
    : 'Freshness unknown.';
}

/* ---------- URL hash ---------- */

let writingHash = false;

function writeHash() {
  const p = new URLSearchParams();
  if (state.q) p.set('q', state.q);
  if (state.age !== 14) p.set('age', state.age);
  if (state.sort !== 'date') p.set('sort', state.sort);
  if (state.view !== 'cards') p.set('view', state.view);
  if (state.when !== '90') p.set('when', state.when);
  if (state.shape !== 'all') p.set('shape', state.shape);
  if (state.from) p.set('from', state.from);
  if (state.to) p.set('to', state.to);
  if (state.cities.size) p.set('city', [...state.cities].join('|'));
  if (state.careers.size) p.set('work', [...state.careers].join('|'));
  if (state.styles.size) p.set('doing', [...state.styles].join('|'));
  if (state.flags.size) p.set('flag', [...state.flags].join('|'));
  const s = p.toString();
  const target = s ? '#' + s : location.pathname;
  if (location.hash === target || (!s && !location.hash)) return;
  writingHash = true;
  history.replaceState(null, '', target);
  writingHash = false;
}

function readHash() {
  const p = new URLSearchParams(location.hash.replace(/^#/, ''));
  if (p.get('q')) state.q = p.get('q');
  if (p.get('age')) state.age = Number(p.get('age')) || 14;
  if (p.get('sort')) state.sort = p.get('sort');
  if (p.get('view')) state.view = p.get('view');
  if (p.get('when')) state.when = p.get('when');
  if (p.get('shape')) state.shape = p.get('shape');
  if (p.get('from')) state.from = p.get('from');
  if (p.get('to')) state.to = p.get('to');
  if (p.get('city')) state.cities = new Set(p.get('city').split('|'));
  if (p.get('work')) state.careers = new Set(p.get('work').split('|'));
  if (p.get('doing')) state.styles = new Set(p.get('doing').split('|'));
  if (p.get('flag')) state.flags = new Set(p.get('flag').split('|'));
}

/* ---------- wiring ---------- */

function bind() {
  const q = document.getElementById('q');
  q.value = state.q;
  q.addEventListener('input', () => { state.q = q.value.trim(); render(); });

  const age = document.getElementById('age');
  age.value = state.age;
  age.addEventListener('change', () => { state.age = Number(age.value) || 0; render(); });

  const sort = document.getElementById('sort');
  sort.value = state.sort;
  sort.addEventListener('change', () => { state.sort = sort.value; render(); });

  const view = document.getElementById('view');
  view.value = state.view;
  view.addEventListener('change', () => { state.view = view.value; render(); });

  const from = document.getElementById('from');
  const to = document.getElementById('to');
  from.value = state.from; to.value = state.to;
  [from, to].forEach((inp) => inp.addEventListener('change', () => {
    state.from = from.value; state.to = to.value; state.when = 'custom';
    buildChips(); render();
  }));

  document.getElementById('reset').addEventListener('click', () => {
    state.q = ''; state.age = 14; state.sort = 'date'; state.view = 'cards';
    state.when = '90'; state.shape = 'all'; state.from = ''; state.to = '';
    state.cities = new Set(); state.careers = new Set();
    state.styles = new Set(); state.flags = new Set();
    // `hidden` is deliberately preserved: dismissing an event is a judgement,
    // not a filter, and clearing filters should not undo it.  "Restore hidden"
    // is the control for that.
    q.value = ''; age.value = 14; sort.value = 'date'; view.value = 'cards';
    from.value = ''; to.value = '';
    buildChips(); render();
  });

  const restore = document.getElementById('restore');
  restore.addEventListener('click', () => {
    hidden = new Set();
    saveStore();
    render();
  });

  document.getElementById('ics').addEventListener('click', () => {
    downloadIcs(sortEvents(all.filter(matches)));
  });

  document.getElementById('share').addEventListener('click', async (ev) => {
    writeHash();
    try {
      await navigator.clipboard.writeText(location.href);
      ev.target.textContent = 'Link copied';
      setTimeout(() => { ev.target.textContent = 'Copy link to this view'; }, 1800);
    } catch (e) {
      ev.target.textContent = location.href;
    }
  });
}

function applyHashToControls() {
  readHash();
  const set = (id, value) => { const n = document.getElementById(id); if (n) n.value = value; };
  set('q', state.q); set('age', state.age); set('sort', state.sort);
  set('view', state.view); set('from', state.from); set('to', state.to);
  buildChips();
  render();
}

async function init() {
  loadStore();
  readHash();
  try {
    const resp = await fetch(DATA_URL, { cache: 'no-cache' });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const doc = await resp.json();
    all = (doc.events || []).filter((e) => e && e.id && e.title);
    meta = doc;
  } catch (e) {
    document.getElementById('results').appendChild(
      el('p', 'empty', 'Could not load the event data (' + e.message + ').'));
    return;
  }
  bind();
  buildChips();
  renderHealth();
  render();
  window.addEventListener('hashchange', () => {
    if (!writingHash) applyHashToControls();
  });
}

init();
