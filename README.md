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
data/curated/       hand-written entries for things no feed lists
docs/               the published site (GitHub Pages root)
  data/events.json  the only thing the page loads
tests/              116 offline tests + 9 live source checks
```

## Quick run

```bash
source ~/Work/venvs/.venv/bin/activate
pip install -r requirements.txt

export EVENTS_CONTACT="you@example.org"        # goes in the User-Agent
python -m collector.build -v                   # writes docs/data/events.json
python -m pytest -q                            # offline suite
python -m pytest -m network -q                 # check the live sources

python -m http.server 8000 --directory docs    # then open localhost:8000
```

## How it stays up to date

`.github/workflows/refresh.yml` runs the collector daily at 06:15 UTC, commits
the refreshed `docs/data/events.json`, and **explicitly deploys the Pages
artifact** — a commit made with `GITHUB_TOKEN` does not itself trigger a Pages
build, so a commit-only workflow would never update the live site.

`.github/workflows/tests.yml` runs the offline suite on every push and the
live source checks every Monday. Source drift — a venue redesigning its
listing page — is the main way this project breaks, and the Monday run is what
catches it.

### What happens when a source breaks

Silence is the enemy, so the build distinguishes three outcomes per source
(*ok*, *fetch error*, *suspicious result*) and:

* **carries over** the last good records for a failed source, keeping their
  original `last_seen`, so the page shows stale-but-labelled data instead of
  losing half its content;
* **refuses to publish** when a source that previously returned events now
  returns none, or loses more than half of them, unless you pass
  `--allow-drop`;
* writes the file **atomically**, so a crash cannot leave a half-written
  `events.json`;
* publishes `last_success` per source, and the page renders a staleness
  warning from it — a cron job that never ran cannot report its own absence.

## Sources

| Key | Source | Kind | Notes |
|---|---|---|---|
| `rigb` | Royal Institution | `drupal_jsonapi` | Public JSON:API. Carries a real age taxonomy (`Young people 13+`, `Families`, `Adults`, `Children 12 and under`), topics, prices and booking links. The best source here by a distance. |
| `cam_museums` | University of Cambridge Museums | `html_css` | Fitzwilliam, Whipple, Sedgwick, Kettle's Yard, Polar Museum, Museum of Zoology, Botanic Garden. No feed, so CSS selectors — the fragile one. |
| `curated_teen_careers` | Hand-checked | `curated` | Standing offers with no feed: Old Bailey public gallery, Bank of England Museum, Supreme Court tours, long exhibitions. |

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
