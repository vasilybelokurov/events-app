# CLAUDE.md — events_app

Project-specific working rules. The general ones are in `../CLAUDE.md`; this
file only covers what is true *here* and cannot be guessed from the code.
`README.md` is the reference for how the thing works — this is the operating
manual for changing it.

## What this is for

A static site plus a scheduled collector, listing Cambridge and London events
for a 14-year-old, tagged by **the kinds of work they put on display**. Breadth
of occupation is the point. "Another science feed" is worth less than a source
that shows an archivist, a patent agent, a stage manager or a rail engineer.

Live at <https://vasilybelokurov.github.io/events-app/>.

## Commands

```bash
source ~/Work/venvs/.venv/bin/activate
export EVENTS_CONTACT="you@example.org"        # goes in the User-Agent

python -m collector.build -v      # writes docs/data/events.json
python -m collector.verify        # every source fetched, parsed, reported
python -m pytest -q               # 301 offline tests (README repeats this; keep both current)
python -m pytest -m network -q    # 14 live source checks
python -m http.server 8080 --directory docs
```

Every Bash call starts a fresh shell, so activate the venv **in the same
command** or the imports fail. `EVENTS_CONTACT` is not required — `http.py`
falls back to the repository's issues URL — but set it so venues see a contact
that is yours.

## Non-negotiables

These are the project's claims about its own honesty. Breaking one is worse
than shipping nothing.

* **Nothing is typed in by hand.** Every record comes from a source registered
  in `collector/sources.yaml`. There are currently no curated entries.
* **Never fabricate a date.** A recurrence description is not a date:
  `dateutil`'s fuzzy mode reads "Most Fridays at 11.30" as the 11th, and that
  put an invented event on the page. A date is believed only when the text
  names a day *and* a month, or is numeric. Dropping an event beats inventing
  one.
* **A claim cannot outlive the sentence it came from.** The `venue` adapter's
  `confirm` phrases must still appear on the page or the claim is dropped and
  the omission recorded. Do not restore a claim because it is "obviously true"
  or widely repeated — see the Supreme Court tour price in the README.
* **Degrade, do not freeze.** A failed source keeps its last good records and
  the healthy ones still publish. The alarm is the exit code, never a withheld
  file.
* **`assume_future_dates` is earned per source.** A yearless date means "next
  year" only on a listing that advertises what is coming. Switched on globally
  it invented six 2027 events from the Cambridge Festival's 2025 programme.

## docs/data/events.json is a build artifact

Never hand-edit it, and never resolve a merge conflict in it. `.gitattributes`
marks it `-merge` on purpose: a union driver once interleaved two copies into a
file that parsed nowhere. The daily workflow rewrites and pushes it, so a local
commit *will* collide. On conflict:

```bash
python -m collector.build && git add docs/data/events.json
```

## Reading CI

Three workflows: `Tests`, `Refresh events and deploy`, `Weekly source review`.

**A red `Refresh events and deploy` usually means a degraded source, not a
broken deploy.** Publication happens first and `Report collector health` turns
the run red afterwards. Read that step before assuming anything is broken: a
venue serving a transient 503 on its `robots.txt`, or a 403 to datacentre
addresses, reddens the run while the site updates normally.

`refresh.yml` deploys the Pages artifact explicitly, because a commit made with
`GITHUB_TOKEN` does not itself trigger a Pages build.

## After pushing, check the site, not the workflow

GitHub Pages serves `index.html` and the assets with `cache-control:
max-age=600`, and the asset URLs carry no version stamp. For ten minutes a
browser can pair **new HTML with old JavaScript**, which renders a control's
container with nothing in it and looks exactly like a broken feature. So:
push, wait for both workflows, then `curl` the live URL for a string only the
new version contains. Report "committed but not pushed" as the headline when
that is the situation — it is the only fact that matters.

## Front end

`docs/index.html` + `assets/app.js` + `assets/styles.css`. No build step and
no third-party libraries; `app.js` is a classic script with no module exports,
which is why the tests evaluate it as a function body.

* **Render it before calling a visual change done.** Tests cannot see layout.
  Adding one fieldset to the filter row overflowed the date inputs through
  their border and opened a band of empty space; 299 tests passed throughout,
  and every fault was obvious in the first screenshot.

  ```bash
  CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  "$CHROME" --headless --disable-gpu --no-sandbox --hide-scrollbars \
    --virtual-time-budget=5000 --window-size=1440,900 \
    --screenshot=/tmp/shot.png "http://localhost:8080/"
  ```

  Then read the PNG, at 1440, 1100, 768 and 414.
* Tests live in `tests/js/` and run through Node against the **shipped**
  `app.js`, evaluated as a function body with `init()` stripped. A copy of the
  logic in a test would pass while the real page was broken.
* Collector output is inserted with `textContent` only, and every URL is
  checked against an `http`/`https` allowlist before it becomes an `href`.

## Changing a shared parser

`collector/models.py` is used by every adapter, so a "fix" for one source
silently corrupts others. Before concluding anything, diff the old and new
implementations over **real** strings from every source:

```python
old = {}
baseline = "HEAD"        # the commit *before* your change, not origin/main:
                         # once you have pushed, origin/main contains it
exec(compile(subprocess.check_output(
    ["git", "show", f"{baseline}:collector/models.py"]).decode(), "o", "exec"), old)
# then parse the same live strings with both and report lost / gained / changed
```

That check found two regressions a full green suite did not: two LSE events
lost outright, and a range whose event ended the instant it started.

## Adding a source

A venue should be a registry entry, not new code. Try `jsonld` first (it also
reads a CMS JSON island or a plain JSON API), then `ics`, then `html_css`
selectors, then `drupal_jsonapi` (check `https://<host>/jsonapi`). Declare
`homepage`, `verify_url`, `docs` and `terms`; add a fixture and a test; run
`pytest -m network`.

Record what you probed and rejected in the README, with the reason and the
date. Several venues have been probed more than once because nobody wrote it
down.

## Second-opinion reviews

`/ask-codex` runs read-only **without this project's dependencies**, so it
cannot execute anything and its findings are static analysis. Reproduce each
one before repeating it: of eight findings on the parser work, six were real,
one was refuted, and it missed the two that only appear when code runs.

## Paths

`/Users/vasilybelokurov/Work/Code/events_app` and the Dropbox path are the
**same checkout** (same inodes), not two copies. Do not try to sync them.
