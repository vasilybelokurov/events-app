# What's On — Cambridge & London events, filtered by the work they show

A static site plus a scheduled collector, live at
<https://vasilybelokurov.github.io/events-app/>.

The collector pulls events from public feeds and APIs, normalises them into one JSON file, and GitHub Pages
serves a dependency-free page that filters and sorts them in the browser.

The first use case: finding things in Cambridge and London to take a
14-year-old to, chosen for **breadth of occupation** rather than school
subject — so every event is tagged with the kinds of work it puts on display.

```
collector/          Python: adapters -> normalise -> de-duplicate -> events.json
  sources.yaml      the source registry: add a venue here, not in code
  adapters/         one module per kind of source
  verify.py         proves every source is listed, reachable and parseable
data/curated/       hand-written entries, for anything no feed lists (empty)
docs/               the published site (GitHub Pages root)
  data/events.json  the only thing the page loads
tests/              298 offline tests + 14 live source checks
  js/               the front end, driven through Node against app.js itself
```

## Quick run

```bash
source ~/Work/venvs/.venv/bin/activate
pip install -r requirements.txt

export EVENTS_CONTACT="you@example.org"        # goes in the User-Agent
python -m collector.build -v                   # writes docs/data/events.json
python -m collector.verify                     # check every source on demand
python -m pytest -q                            # offline suite
python -m pytest -m network -q                 # hit the live sources

python -m http.server 8000 --directory docs    # then open localhost:8000
```

## How it stays alive

Three scheduled jobs, each answering a different failure:

| Workflow | When | What it catches |
|---|---|---|
| `refresh.yml` | daily 06:15 UTC | New events; publishes and deploys. |
| `tests.yml` | every push, plus Mondays | Code regressions, and (Mondays) live source drift. |
| `review.yml` | Mondays 08:10 UTC | Files one rolling issue with every source verified and a checklist of hand-written claims whose verification has expired. |

`refresh.yml` deploys the Pages artifact **explicitly**, because a commit made
with `GITHUB_TOKEN` does not itself trigger a Pages build — a commit-only
workflow would never update the live site.

It also **captures the collector's exit code instead of failing on it**. The
collector exits non-zero when a source is degraded, but it has already written
the data by then; letting that stop the job blocked the commit, the artifact
and the deploy, so one broken venue froze the whole site — exactly what the
policy below exists to prevent. Publication happens first, and a final
`Report collector health` step turns the run red afterwards.

### When a source breaks: degrade, do not freeze

* Each source records `last_attempt` and `last_success`, and *ok*, *fetch
  error* and *suspicious result* are tracked separately.
* A failed source **keeps its last good records**, with their original
  `last_seen`, so the page shows stale-but-labelled data instead of a hole.
* **The healthy sources still publish.** One venue being down must not stop the
  others refreshing.
* The alarm is the **exit code**, not a withheld file: `collector.build` always
  writes, and exits non-zero when anything is degraded, which turns the
  scheduled run red and emails the repository owner. `--allow-drop` accepts a
  genuine shrinkage and exits zero.
* Writes are atomic, so a crash cannot leave a half-written `events.json`.
* The page renders its own staleness warning from `last_success`, because a
  cron job that never ran cannot report its own absence.

### When a hand-written claim goes stale

**There are currently none: every record on the site is collected from a
source.** The machinery stays, because the next awkward venue will need it.

A resolving link does not prove a price, an opening time or an age rule still
holds. So every curated entry carries `verified_on`, the build link-checks it
on every run, and after 90 days the page badges it **needs re-checking** and
the Monday issue puts it on a checklist. An entry that has never been confirmed
shows as **unverified claim** from the start.

## Sources

**Every source lives in `collector/sources.yaml`, and nothing reaches the page
from a source that is not in it.** Each entry must declare a `homepage` a
person can open, a `verify_url` the code can fetch, where the interface is
documented, and the terms it is used under. The published page renders all of
it, so any claim on the site can be traced to its origin in two clicks.

**Nothing is typed in by hand.** An earlier version carried nine hand-written
event records; a typed claim rots silently, and eight of the nine were never
confirmed by anyone. They are now nineteen collected sources:

| Source | Kind | What it gives |
|---|---|---|
| [Royal Institution](https://www.rigb.org/whats-on) | `drupal_jsonapi` | ~30 talks with a real age taxonomy, prices and booking links. The venue maintains it. |
| [University of Cambridge Museums](https://www.museums.cam.ac.uk/whats-on) | `html_css` | ~100 events across eight museums and the Botanic Garden, with ages read from each event's own page. |
| [Wellcome Collection](https://wellcomecollection.org/events) | `jsonld` | Medicine, health, psychology, society and the art/science boundary, from a public content API. Its audience vocabulary (`14+`, `14+ (adult required for under-18s)`, `Youth event`, `Schools`) is the best age metadata here after the Ri. |
| [V&A Young People](https://www.vam.ac.uk/whatson/programmes/young-people) | `html_css` | Workshops led by practising creatives: design, fashion, digital media, craft, architecture. **Explicitly for ages 13–26** — the strongest counterweight to "science therefore scientist". |
| [The Royal Society](https://royalsociety.org/science-events-and-lectures/public/) | `html_css` | Working research scientists and current research fields — different exposure from the Ri's science communication. |
| [LSE public events](https://www.lse.ac.uk/events/search-events) | `html_css` | Economics, public policy, law, politics, society. Fills the biggest gap: economist, lawyer, policy analyst, civil servant, statistician. |
| [Hunterian Museum](https://hunterianmuseum.org/whats-on/) | `html_css` | Surgery and medical history: exhibitions, family activities, curator tours. |
| [Darwin College Lectures](https://talks.cam.ac.uk/show/index/5358), [Major Public Lectures](https://talks.cam.ac.uk/show/index/5462), [CSAR](https://talks.cam.ac.uk/show/index/5366) | `ics` | Cambridge public lecture series. Three of ~2400 talks.cam lists, each a one-line registry entry. |
| [British Library](https://www.bl.uk/events/) | `html_css` | ~104 events over 16 paginated pages: the business programme (market research, intellectual property, payments, start-ups) beside writing, publishing, archives and conservation. The only source here that shows entrepreneur, publisher, editor, archivist or librarian. |
| [Cambridge Festival](https://www.festival.cam.ac.uk/events) | `html_css` | Dormant until the next programme is published, then it appears on its own. |
| [Old Bailey](https://www.cityoflondon.gov.uk/about-us/law-historic-governance/central-criminal-court), [Supreme Court](https://www.supremecourt.uk/tours), [Bank of England Museum](https://www.bankofengland.co.uk/museum), [Science Museum](https://www.sciencemuseum.org.uk/see-and-do/technicians-david-sainsbury-gallery), [Design Museum](https://designmuseum.org/whats-on), [Cambridge Museum of Technology](https://www.museumoftechnology.com/whats-on/), [Cambridge Engineering](https://www.eng.cam.ac.uk/outreach) | `venue` | Places with nothing to list. Visited every run; see below. |

Probed and rejected, so nobody repeats the work. Re-checked 2026-09-22, and
none of them publishes JSON-LD, an ICS feed or a JSON:API:

* **Renders its listing in the browser**, leaving nothing in the HTML to
  select: Gresham College, King's College London, the Royal Geographical
  Society, and the UCL `/events/` landing page (UCL's real calendar is
  elsewhere and worth another look).
* **Refuses anything that is not a desktop browser** (403 from here and from a
  CI runner alike): the Institute of Physics, Kew, the National Theatre, the
  London Transport Museum, the Southbank Centre and the Francis Crick
  Institute.
* **Server-rendered but with no usable structure**: the Institution of Civil
  Engineers (200 since it last refused, so its markup is worth a second look),
  the Natural History Museum (a `__NEXT_DATA__` island with no event objects),
  the Barbican, ZSL London Zoo, Royal Museums Greenwich, the National Archives
  and Cambridge Junction.

Any of them becomes a one-line registry entry the day it publishes a feed.

### Venues with nothing to list

A public gallery, a museum open on weekdays, a weekly tour: no feed, no
programme, and not really "events". The `venue` adapter visits the page on
every run and builds the record from what the page says **today**. Any extra
claim is declared with the phrase that must still appear:

```yaml
confirm:
  - phrase: "no admission for children under 14"
    sets:
      age_min: 14
      age_text: "no admission for children under 14; proof of age may be requested"
```

Find the phrase, apply the claim. Miss it, and the claim is dropped and the
omission recorded in `provenance`. **A claim can never outlive the sentence it
came from** — a stronger guarantee than a link check, which only proves a page
loads. `expect` phrases catch a page that has been repurposed, and
`allow_blocked` keeps a venue that refuses automated access (the Science
Museum serves 403 to anything but a desktop browser) in the list while saying
plainly that nothing was confirmed.

Worth knowing what this discipline costs: the widely repeated claim that the
Supreme Court runs Friday 2pm tours at GBP 10 with under-16s free is **not** on
the Court's own page, so the site does not say it. Two other claims
(the Science Museum's "ages 11-16", an earlier Old Bailey wording) were dropped
the same way.

### Verifying them

```bash
python -m collector.verify              # table: every source, fetched and parsed
python -m collector.verify --markdown   # the weekly review issue's body
python -m collector.verify --only rigb  # one source
python -m collector.verify --json       # machine-readable
```

Three questions are asked of each source and kept deliberately apart:

* **Reachable?** Does `verify_url` answer, and with what status.
* **Parseable?** Does the adapter still understand the response, and how many
  events come out. A redesigned page answers `200` and yields nothing — it is
  reachable but not parseable, and that distinction is the whole point.
* **Current?** For hand-written claims: when did a person last confirm this,
  and is that within 90 days.

Exit status is zero only when every source passes and nothing is overdue, so it
works as a CI check. A source with no `verify_url` and no local file is itself
reported as a failure: *unverifiable* is not an acceptable state.

**Refused is not the same as gone.** Several venues (the Science Museum among
them) serve `403` to datacentre addresses, so the same URL reads `200` from a
laptop and `403` from a CI runner. Those statuses are reported as *blocked*,
with a note rather than a failure, and the parse check decides whether the
source is actually usable. Crying wolf every Monday would train the reader to
ignore the report, which is worse than not reporting at all.

### Cambridge talks: ~2400 more lists, one line each

`talks.cam.ac.uk` publishes every list as iCalendar at `/show/ics/<list_id>`
([documented](https://talks.cam.ac.uk/document/syndicating_talks/)). To add one:
find it under `/index/lists/<letter>/`, take the id from its URL, and copy the
`talks_cam_darwin` block. Most lists are dormant or postgraduate-only, so check
the feed returns future events first.

### Adding a venue

Most venues need no new code. Add a block to `collector/sources.yaml`:

* **`jsonld`** — try this first. If the venue's listing page carries
  `<script type="application/ld+json">` with `"@type": "Event"`, you only need
  its URL.
* **`ics`** — if it publishes a calendar feed.
* **`html_css`** — otherwise: write CSS selectors for the listing rows
  (`item`, `title`, `link`, `date`, `time`, `end`, `summary`, `venue`,
  `price`). No Python required. Two things worth knowing:
  * **Any selector may be a list**, tried in order. That is how a listing
    that marks some rows up properly and writes the rest as prose is read:
    `date: ["time[itemprop=startDate]", ".c-media__datetime"]` with
    `date_attr: datetime` believes the machine value and falls back to the
    sentence.
  * **`pages: {param: page, max_pages: 25}`** walks a paginated listing,
    stopping when a page offers rows that earlier pages already had — the
    real end, and also what a site that serves page 1 for every number looks
    like. Rows pinned to every page are published once.
    A page with **no rows at all** is not treated as the end: an interstitial
    or an error page served as HTTP 200 looks exactly the same, so the walk
    steps over one and gives up after two, without claiming to have finished.
    Anything short of a confirmed end — that, or exhausting `max_pages` —
    reports `pagination_complete: false` and degrades the source, which keeps
    its last good records and turns the run red. Set `max_pages` comfortably
    above the real page count, because reaching it is treated as truncation.
* **`drupal_jsonapi`** — many UK institutions run Drupal; check
  `https://<host>/jsonapi` for a `node--event` resource before writing
  selectors. It is far more reliable than scraping.

The `jsonld` adapter is the one to try first, and it is broader than its name:
it reads a `<script type="application/ld+json">` block, a CMS JSON island such
as Next.js's `__NEXT_DATA__`, **or a plain JSON API response**, walking any of
them for objects typed as an event. Wellcome Collection needed no new code —
only `url_template` (to build a per-event link from the object's own id) and
`require_url` (to drop objects that would otherwise all link to the listing).

Then add a fixture and a test, and run `pytest -m network` to confirm the live
source behaves.

### Sources and permissions

The collector is polite by construction: `collector/http.py` checks
`robots.txt`, throttles to one request per host per second, retries with
backoff, identifies itself with a contact URL (`EVENTS_CONTACT`), and caches
responses so repeated local runs do not hammer venue sites.

Being *able* to fetch a page is not permission to republish it. This project
stores factual metadata — title, date, place, price, age guidance — and links
back to the venue's own page, which is the authority. It does not reproduce
descriptions or images wholesale. Prefer an official feed or API to scraping;
respecting `robots.txt` is a courtesy, not a licence
([RFC 9309](https://www.rfc-editor.org/rfc/rfc9309.html) says so explicitly),
and UK copyright and
[database rights](https://www.gov.uk/guidance/sui-generis-database-rights) can
still apply. If you add a commercial source, check its terms.

## What the page will and will not tell you

Honest limits, because a parent acting on wrong information wastes a day out:

* **Age eligibility has three states**, not two: *eligible* (the venue states
  an age range that includes yours), *excluded*, and *no stated age limit*.
  The last is shown as a warning badge, not as approval. The Royal Institution
  states that its ordinary talks are designed for 15+ but that younger
  visitors are welcome with parental permission, so an "Adults" label filters
  nothing out on its own.
* **The two taxonomies are keyword guesses.** *Subject* says what an event is
  about; *ways of working* says what you would actually be doing — research,
  design and making, argument and advocacy, fieldwork, caring, communication.
  The second axis is the one that matters here, because "this is chemistry"
  does not help a 14-year-old decide anything, whereas "this is research,
  making things and quantitative analysis" might. Both are discovery aids, not
  evidence; the keyword lists are in `collector/careers.py` and every verified
  false positive is a test case.
* **Prices are indicative.** `is_free` is only true when nothing on the
  listing costs money: a free child ticket alongside a paid adult ticket is
  not a free outing.
* **Curated entries are dated claims.** Each carries `provenance`, and the
  build HTTP-checks every URL: a dead link or a redirect to a site home page
  is shown as a warning badge rather than trusted. Two entries suggested by a
  ChatGPT conversation were dropped outright because their URLs did not
  resolve at all.
* **Recurring events show one occurrence.** ICS `RRULE` is not expanded; the
  record says so rather than inventing dates. So a weekly series arrives as a
  single dated occurrence and the "kind" filter counts it as **one-off**: the
  site does not know it repeats, and will not guess. Only a venue that
  publishes a run, or no date at all, is shown as running over time.
* **Cancellations are only as good as the feed.** Events marked cancelled,
  postponed or sold out are hidden unless you tick "Include cancelled", but a
  venue that does not publish the cancellation cannot be second-guessed.
  Always open the link before booking or travelling.

## Front end

`docs/index.html` + `assets/app.js` + `assets/styles.css`, no build step and no
dependencies.

* Filters: full-text search, age, date window (7/30/90 days, everything, or
  custom), **kind** (one-off or runs over time), city/online, audience and
  access flags, subject, ways of working.
* **Kind** answers the question a person actually asks: do I have to be
  somewhere at 18:30 on Tuesday, or can I go any time over the next six
  weeks? *One-off* is a single dated occurrence; *runs over time* covers both
  a multi-day exhibition and a standing offer with no fixed date at all, such
  as a public gallery open on weekdays. Cards say which they are.
  It is **not** a claim about recurrence — see below.
  "Only confirmed suitable" is named for what it does: ineligible events are
  always hidden, so the control decides whether events with *unknown*
  eligibility are shown.
* Sorts: date (grouped by month), title, cheapest first, place, source.
* Views: cards or table.
* Saved and "not interested" lists in `localStorage`; filter state in the URL
  hash, so a view can be shared with "Copy link to this view". Clearing filters
  does not discard dismissed events — "Restore hidden" does that.
* `.ics` export for one event or for everything currently shown.
* Collector output is inserted with `textContent` only, and every URL is
  checked against an `http`/`https` allowlist before it becomes an `href`.

## Design notes

* **Times** are ISO-8601 with an explicit Europe/London offset. Naive input is
  treated as London local; a time in the spring-forward gap is moved forward
  rather than silently mis-offset; durations are added in local terms so an
  event spanning a clock change keeps its wall-clock time.
* **A pattern is not a date.** Venues write "Most Fridays at 11.30" or "Once
  a month, Wednesdays" in the field where a date belongs, and a fuzzy date
  parser will happily return the 11th of this month for both. Those rows are
  **dropped**: a fabricated date on the page is worse than a missing event.
  A date is only believed when the text names a day *and* a month, or is
  written numerically.
* **A date with no year is read as this year, unless the source says
  otherwise.** "Saturday 3 January" seen in September means next January on a
  live what's-on listing, and the British Library's listing is one — it sets
  `assume_future_dates: true`. It stays off everywhere else, because the
  Cambridge Festival page still has the 2025 programme up, and rolling its
  yearless dates forward invented 2027 events for things that happened two
  years ago.
* **De-duplication is conservative.** Two records merge only when title,
  start time *and* city agree; two sessions on the same day stay separate, and
  `(BSL)` / `(English)` variants stay separate. Merging fills empty fields
  only — it never turns a paid event free, and it copies age bounds as a pair
  so it cannot manufacture an impossible range. The surviving event id follows
  source priority, not richness, so saved items do not lose their identity
  when a source adds a sentence.
* **Event ids are stable** across rebuilds (`source` + native id), which is
  what makes the browser's saved list survive a refresh.

## Review history

The architecture and the first implementation were reviewed by OpenAI Codex
(read-only, high effort). It found ten real parser defects, a destructive
merge, a missing pagination path, unhandled cancellations, currency confusion
in the JSON-LD adapter, and the `GITHUB_TOKEN`/Pages deployment trap. Each was
reproduced before being fixed, and each now has a test in
`tests/test_models.py`, `tests/test_dedup.py` or `tests/test_adapters.py`.
