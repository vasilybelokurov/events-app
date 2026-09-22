"""Build ``docs/data/events.json`` from every configured source.

Run it locally or from the GitHub Actions cron job::

    python -m collector.build --out docs/data/events.json

**Failure policy: degrade, do not freeze.** A silent failure is worse than a
loud one, and worse still is a failure that looks like news.  So:

* every source gets ``last_attempt`` and ``last_success`` timestamps, and the
  three outcomes *ok*, *fetch error* and *suspicious result* are recorded
  separately;
* when a source fails, its records from the previous build are **carried over
  with their original ``last_seen``**, so the page shows stale-but-labelled
  data rather than losing half its content;
* **the healthy sources are still published.**  One venue being down must not
  stop the other two refreshing: an earlier version refused to write anything
  when any source misbehaved, which meant a one-hour outage at one venue froze
  the whole page for a day;
* the alarm is the **exit code**, not a withheld file.  ``build()`` always
  writes; :func:`main` returns non-zero when any source is degraded, which is
  what turns the scheduled run red and emails whoever owns the repository.
  ``--allow-drop`` says "yes, that shrinkage is genuine": it publishes what
  the source actually returned, rebases the baseline on that count and exits
  zero.  It refuses to accept a crawl known to have been cut short.
* the page itself renders a staleness warning from ``last_success``, because a
  cron job that never ran cannot report its own absence.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from .adapters import get as get_adapter
from .dedup import merge
from .http import check_link
from .models import UK, Event

LOG = logging.getLogger("build")

#: A source that previously worked may not lose more than this fraction of its
#: events without the build failing.
DROP_TOLERANCE = 0.5

#: How long a source's data may go unrefreshed before the page flags it.
STALE_AFTER_HOURS = 48

#: How long a hand-written claim stands before it needs re-checking by a human.
#: Kept here as well as in :mod:`collector.verify` so the published file tells
#: the page what the policy is.
VERIFY_AFTER_DAYS = 90


def load_sources(path: Path) -> list[dict]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return doc.get("sources", [])


def load_previous(out_path: Path) -> dict:
    """The last published document, used for carry-over and drop detection."""
    if not out_path.exists():
        return {}
    try:
        return json.loads(out_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def collect_source(cfg: dict, *, now: datetime, root: Path) -> tuple[list[Event], dict]:
    adapter = get_adapter(cfg["kind"])
    local = dict(cfg)
    if "path" in local:                       # curated files are repo-relative
        local["path"] = str(root / local["path"])
    raw = adapter.fetch_raw(local, now=now) if _takes_now(adapter) \
        else adapter.fetch_raw(local)
    meta = {}
    if isinstance(raw, str) and raw.lstrip().startswith("{"):
        try:
            meta = (json.loads(raw).get("meta") or {})
        except json.JSONDecodeError:
            meta = {}
    events = adapter.parse(raw, local)
    # Adapters that fetch extra pages report what that achieved, so a silent
    # enrichment failure shows up in the health panel rather than as missing
    # age data nobody notices.
    meta.update(getattr(adapter, "last_enrichment", {}) or {})
    # Adapters that can publish something despite a failed fetch say so here,
    # so the build does not record a success the fetch never earned.
    meta.update(getattr(adapter, "last_fetch", {}) or {})
    return events, meta


def _takes_now(adapter) -> bool:
    import inspect
    return "now" in inspect.signature(adapter.fetch_raw).parameters


def is_current(e: Event, *, now: datetime, grace_hours: int = 6) -> bool:
    """Keep future events, and ongoing runs whose end date has not passed."""
    if e.anytime:
        return True
    cutoff = now - timedelta(hours=grace_hours)
    for value in (e.end, e.start):
        if not value:
            continue
        try:
            if datetime.fromisoformat(value) >= cutoff:
                return True
        except ValueError:
            continue
    return False


def build(sources_path: Path, out_path: Path, *, root: Path,
          only: list[str] | None = None, link_check: bool = True,
          horizon_days: int = 400, allow_drop: bool = False,
          now: datetime | None = None) -> dict:
    # Injectable so the carry-over policy can be tested across time: whether a
    # record still belongs on the page depends entirely on today's date.
    now = now or datetime.now(UK)
    stamp = now.astimezone(timezone.utc).isoformat()
    sources = load_sources(sources_path)
    if only:
        sources = [s for s in sources if s["key"] in only]

    previous = load_previous(out_path)
    prev_reports = {s["key"]: s for s in previous.get("sources", [])}
    prev_events: dict[str, list[dict]] = {}
    for raw_event in previous.get("events", []):
        prev_events.setdefault(raw_event.get("source", ""), []).append(raw_event)

    all_events: list[Event] = []
    carried: list[dict] = []
    reports: list[dict] = []
    failures: list[str] = []
    horizon = (now + timedelta(days=horizon_days)).isoformat()

    for cfg in sources:
        prev = prev_reports.get(cfg["key"], {})
        # The baseline for drop detection is the last count this source is
        # known to have produced *successfully*, carried across degraded runs.
        prev_good = prev.get("last_good_count")
        if prev_good is None:
            prev_good = prev.get("count", 0) if prev.get("status") == "ok" else 0
        prev_records = prev_events.get(cfg["key"], [])

        report = {
            "key": cfg["key"], "name": cfg["name"], "kind": cfg["kind"],
            # Carried from the registry so every source is listed *and*
            # openable from the page itself, not just from the repository.
            "homepage": cfg.get("homepage"),
            "verify_url": cfg.get("verify_url"),
            "raw_url": cfg.get("raw_url") or cfg.get("verify_url"),
            "docs": cfg.get("docs"),
            "terms": cfg.get("terms"),
            "adapter_verified_on": str(cfg.get("verified")) if cfg.get("verified") else None,
            "count": 0,              # how many records were published
            "candidate_count": None,  # how many the source offered this run
            "last_good_count": prev_good,
            "status": "ok", "error": None,
            "last_attempt": stamp,
            "last_success": prev.get("last_success"),
            "pagination_complete": None,
        }
        try:
            events, meta = collect_source(cfg, now=now, root=root)
            report["pagination_complete"] = meta.get("pagination_complete")
            for k in ("detail_enabled", "detail_attempted", "detail_enriched",
                      "detail_failed", "detail_ages_added"):
                if k in meta:
                    report[k] = meta[k]
        except Exception as exc:                        # noqa: BLE001
            report.update(status="fetch_error", error=f"{type(exc).__name__}: {exc}")
            failures.append(f"{cfg['key']}: {report['error']}")
            LOG.error("source %s failed: %s", cfg["key"], exc)
            carried.extend(prev_records)
            report["count"] = len(prev_records)
            report["carried_over"] = len(prev_records)
            reports.append(report)
            continue

        candidate = [e for e in events
                     if is_current(e, now=now) and (e.start or "") <= horizon]
        report["candidate_count"] = len(candidate)
        report["fetched"] = len(events)

        # A source the adapter could not reach may still yield a useful record
        # from its configuration, but it has not been verified today.
        blocked = meta.get("reachable") is False
        if blocked:
            report["status"] = "blocked"
            report["error"] = meta.get("reason")
        elif prev_good and len(candidate) == 0:
            report["status"] = "empty"
            failures.append(
                f"{cfg['key']}: returned 0 events, last good count was {prev_good}")
        elif prev_good and len(candidate) < prev_good * (1 - DROP_TOLERANCE):
            report["status"] = "shrunk"
            failures.append(
                f"{cfg['key']}: returned {len(candidate)} events, "
                f"last good count was {prev_good}")
        elif report["pagination_complete"] is False:
            report["status"] = "partial"
            failures.append(f"{cfg['key']}: pagination incomplete")

        # Never accept a drop from a crawl we know was cut short: the smaller
        # number is an artefact of the truncation, not the venue's programme.
        accepted_drop = (allow_drop and report["status"] in ("empty", "shrunk")
                         and report["pagination_complete"] is not False)
        if accepted_drop:
            # `--allow-drop` has to mean something.  It used to change only the
            # exit code, so the shrunken source still published its *old*
            # records and the baseline never moved -- the next run raised the
            # same alarm, and the README's claim that the flag "accepts a
            # genuine shrinkage" was false.  Accepting means publishing what
            # the source actually returned and rebasing on it.
            report["status"] = "ok"
            report["accepted_drop"] = {"from": prev_good, "to": len(candidate)}
            failures[:] = [f for f in failures if not f.startswith(f"{cfg['key']}: ")]

        if not accepted_drop and report["status"] in ("empty", "shrunk", "partial"):
            # Suspicious, or known-incomplete: keep the previous records rather
            # than publish a hole, and leave the baseline where it was so the
            # next run is still compared against a healthy figure.
            carried.extend(prev_records)
            report["carried_over"] = len(prev_records)
            report["count"] = len(prev_records)
        else:
            for e in candidate:
                e.last_seen = stamp
            all_events.extend(candidate)
            report["count"] = len(candidate)
            if blocked:
                # Published, but neither a success nor a new baseline.
                report["last_good_count"] = prev_good or len(candidate)
            else:
                report["last_success"] = stamp
                report["last_good_count"] = len(candidate)
        reports.append(report)

    priority = {s["key"]: s.get("priority", 50) for s in sources}
    events = merge(all_events, priority)
    records = [e.to_dict() for e in events]

    # Exactly the sources the registry declares hand-written, rather than a
    # guess from the key's spelling.
    hand_written_keys = {s["key"] for s in sources
                         if s.get("hand_written", s["kind"] == "curated")}

    if link_check:
        checked: dict[str, dict] = {}
        for rec in records:
            if rec.get("source") not in hand_written_keys or not rec.get("url"):
                continue
            url = rec["url"]
            if url not in checked:
                checked[url] = check_link(url)
            probe = checked[url]
            rec["link_status"] = probe["status"]
            if probe["redirected_to_root"]:
                rec["link_warning"] = "redirects to the site home page"

    # A hand-written source's row is only meaningful with its verification
    # tally, since that is the thing a reader has to judge it on.
    today = now.date()
    for report in reports:
        if report["key"] not in hand_written_keys:
            continue
        mine = [r for r in records if r.get("source") == report["key"]]
        fresh = 0
        for rec in mine:
            stamp = rec.get("verified_on")
            if not stamp:
                continue
            try:
                age = (today - date.fromisoformat(str(stamp)[:10])).days
            except ValueError:
                continue
            if age <= VERIFY_AFTER_DAYS:
                fresh += 1
        report["verified_entries"] = fresh
        report["unverified_entries"] = len(mine) - fresh

    # Carried-over records keep their original last_seen so the UI can age
    # them -- but they are still subject to the calendar.  Carrying them
    # unconditionally meant a venue that went away for good kept its events on
    # the page after the dates had passed: the records were never re-checked
    # because the source never succeeded again, and nothing expired them.
    known = {r["id"] for r in records}
    kept, expired = [], 0
    for r in carried:
        if r.get("id") in known:
            continue
        if is_current(Event(**{k: v for k, v in r.items()
                              if k in Event.__dataclass_fields__}), now=now):
            kept.append(r)
        else:
            expired += 1
    records.extend(kept)
    if expired:
        by_source: dict[str, int] = {}
        for r in carried:
            if r.get("id") not in known and r not in kept:
                by_source[r.get("source", "")] = by_source.get(r.get("source", ""), 0) + 1
        for report in reports:
            n = by_source.get(report["key"])
            if n:
                report["carried_expired"] = n
                report["count"] = max(0, report["count"] - n)
    records.sort(key=lambda r: (r.get("start") or "9999", r.get("title", "")))

    oldest_success = min(
        (s["last_success"] for s in reports if s.get("last_success")),
        default=None)
    doc = {
        "generated_at": stamp,
        "timezone": "Europe/London",
        "stale_after_hours": STALE_AFTER_HOURS,
        "verify_after_days": VERIFY_AFTER_DAYS,
        "event_count": len(records),
        "oldest_source_success": oldest_success,
        "sources": reports,
        "events": records,
    }
    if failures:
        doc["build_warnings"] = failures

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(out_path)                     # atomic publish
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent.parent
    ap.add_argument("--sources", type=Path, default=root / "collector/sources.yaml")
    ap.add_argument("--out", type=Path, default=root / "docs/data/events.json")
    ap.add_argument("--only", nargs="*", help="only these source keys")
    ap.add_argument("--no-link-check", action="store_true")
    ap.add_argument("--allow-drop", action="store_true",
                    help="accept a genuine shrinkage: publish the smaller "
                         "set, rebase the baseline on it and exit 0")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    doc = build(args.sources, args.out, root=root, only=args.only,
                allow_drop=args.allow_drop,
                link_check=not args.no_link_check)
    print(f"{doc['event_count']} events -> {args.out}")
    for s in doc["sources"]:
        flag = "" if s["status"] == "ok" else f"  [{s['status']}: {s['error'] or ''}]"
        carried = f" (+{s['carried_over']} carried over)" if s.get("carried_over") else ""
        print(f"  {s['count']:4d}  {s['key']}{flag}{carried}")
    warnings = doc.get("build_warnings", [])
    for w in warnings:
        print(f"  WARNING {w}")
    if warnings and not args.allow_drop:
        # The data is published regardless; a non-zero exit is what raises the
        # alarm (a red scheduled run, and an email to the repository owner).
        print("\nOne or more sources are degraded. The healthy ones were still\n"
              "published and the degraded ones kept their last good records.\n"
              "Re-run with --allow-drop to accept this as the new normal.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
