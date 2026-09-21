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
data/curated/       hand-written entries for things no feed lists
docs/               the published site (GitHub Pages root)
  data/events.json  the only thing the page loads
tests/              160 offline tests + 9 live source checks
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

A resolving link does not prove a price, an opening time or an age rule still
holds. So every curated entry carries `verified_on`, the build link-checks it
on every run, and after 90 days the page badges it **needs re-checking** and
the Monday issue puts it on a checklist. An entry that has never been confirmed
shows as **unverified claim** from the start — including the eight that came
from a ChatGPT conversation and have only had their URLs machine-checked.

## Sources

**Every source lives in `collector/sources.yaml`, and nothing reaches the page
from a source that is not in it.** Each entry must declare a `homepage` a
person can open, a `verify_url` the code can fetch, where the interface is
documented, and the terms it is used under. The published page renders all of
it, so any claim on the site can be traced back to its origin.

| Key | Source | Kind | Events | Notes |
|---|---|---|---|---|
| `rigb` | [Royal Institution](https://www.rigb.org/whats-on) | `drupal_jsonapi` | ~30 | Public JSON:API with a real age taxonomy (`Young people 13+`, `Families`, `Adults`, `Children 12 and under`), topics, prices and booking links. The venue maintains it; nothing here needs upkeep. |
| `cam_museums` | [University of Cambridge Museums](https://www.museums.cam.ac.uk/whats-on) | `html_css` | ~100 | Fitzwilliam, Whipple, Sedgwick, Kettle's Yard, Polar Museum, Museum of Zoology, Botanic Garden. CSS selectors plus detail-page enrichment — the fragile one. |
| `talks_cam_darwin` | [Darwin College Lecture Series](https://talks.cam.ac.uk/show/index/5358) | `ics` | ~8 | Free public lecture series. See below: this one key unlocks ~2400 Cambridge lists. |
| `curated_teen_careers` | [Hand-checked YAML](data/curated/cambridge_london_teen_careers.yaml) | `curated` | 9 | Standing offers with no feed: Old Bailey public gallery, Bank of England Museum, Supreme Court tours, long exhibitions. |

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
  `price`). No Python required.
* **`drupal_jsonapi`** — many UK institutions run Drupal; check
  `https://<host>/jsonapi` for a `node--event` resource before writing
  selectors. It is far more reliable than scraping.

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
* **"Kind of work it shows" is a keyword guess.** It is a discovery aid, not
  evidence. The keyword lists are in `collector/careers.py` and every
  false positive found so far is a test case.
* **Prices are indicative.** `is_free` is only true when nothing on the
  listing costs money: a free child ticket alongside a paid adult ticket is
  not a free outing.
* **Curated entries are dated claims.** Each carries `provenance`, and the
  build HTTP-checks every URL: a dead link or a redirect to a site home page
  is shown as a warning badge rather than trusted. Two entries suggested by a
  ChatGPT conversation were dropped outright because their URLs did not
  resolve at all.
* **Recurring events show one occurrence.** ICS `RRULE` is not expanded; the
  record says so rather than inventing dates.
* **Cancellations are only as good as the feed.** Events marked cancelled,
  postponed or sold out are hidden unless you tick "Include cancelled", but a
  venue that does not publish the cancellation cannot be second-guessed.
  Always open the link before booking or travelling.

## Front end

`docs/index.html` + `assets/app.js` + `assets/styles.css`, no build step and no
dependencies.

* Filters: full-text search, age, date window (7/30/90 days, everything, or
  custom), city/online, audience and access flags, career theme.
* Sorts: date (grouped by month), title, cheapest first, place, source.
* Views: cards or table.
* Saved and "not interested" lists in `localStorage`; filter state in the URL
  hash, so a view can be shared with "Copy link to this view".
* `.ics` export for one event or for everything currently shown.
* Collector output is inserted with `textContent` only, and every URL is
  checked against an `http`/`https` allowlist before it becomes an `href`.

## Design notes

* **Times** are ISO-8601 with an explicit Europe/London offset. Naive input is
  treated as London local; a time in the spring-forward gap is moved forward
  rather than silently mis-offset; durations are added in local terms so an
  event spanning a clock change keeps its wall-clock time.
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
