"""Verify every source in the registry, on demand.

    python -m collector.verify                # human-readable table
    python -m collector.verify --json         # machine-readable
    python -m collector.verify --markdown     # for the weekly review issue
    python -m collector.verify --only rigb    # one source

Every source in ``sources.yaml`` must be reachable and parseable, and every
hand-written claim must carry a verification date that has not expired.  This
module is the thing that proves it.  Exit status is 0 only when every source
passed and nothing is overdue, so it can be used as a check in CI.

The three questions asked of each source, kept deliberately separate:

**Reachable?**  Does ``verify_url`` answer, and with what status.
**Parseable?**  Does the adapter still understand the response, and how many
events come out.  A source that answers 200 with a redesigned page is reachable
but not parseable, and that distinction is the whole point.
**Current?**  For hand-written entries: when did a human last confirm the claim,
and is that within :data:`VERIFY_AFTER_DAYS`.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from .adapters import get as get_adapter
from .build import load_sources
from .http import check_link, fetch
from .models import UK

#: A hand-written claim goes stale after this long, however healthy its link.
VERIFY_AFTER_DAYS = 90

#: Statuses that mean "we were refused", not "the page is gone".  Several
#: venues (the Science Museum among them) serve 403 to datacentre addresses, so
#: the same URL reads 200 from a laptop and 403 from a CI runner.  Calling that
#: a broken link would produce a false alarm every week and train the reader to
#: ignore the report, which is worse than not reporting at all.
BLOCKED_STATUSES = frozenset({401, 403, 429})

ROOT = Path(__file__).resolve().parent.parent


def _iso_date(value) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def verify_source(cfg: dict, *, root: Path = ROOT, check_entries: bool = True) -> dict:
    """Check one source end to end.  Never raises: failures are the result."""
    result = {
        "key": cfg.get("key"),
        "name": cfg.get("name"),
        "kind": cfg.get("kind"),
        "homepage": cfg.get("homepage"),
        "verify_url": cfg.get("verify_url"),
        "terms": cfg.get("terms"),
        "reachable": None,
        "http_status": None,
        "parseable": None,
        "events": None,
        "adapter_verified_on": cfg.get("verified"),
        "problems": [],
        # Things worth saying that are not failures, such as a venue refusing
        # automated checks.  Kept apart from `problems` so the exit status and
        # the weekly issue stay trustworthy.
        "notes": [],
        "entries": [],
    }

    # --- the registry contract -------------------------------------------
    for required in ("key", "name", "kind", "homepage"):
        if not cfg.get(required):
            result["problems"].append(f"registry entry is missing {required!r}")
    if not cfg.get("terms"):
        result["problems"].append("no terms/licence recorded")

    try:
        adapter = get_adapter(cfg["kind"])
    except KeyError as exc:
        result["problems"].append(str(exc))
        return result

    # --- reachable? -------------------------------------------------------
    verify_url = cfg.get("verify_url")
    if verify_url:
        probe = check_link(verify_url)
        result["http_status"] = probe["status"]
        result["reachable"] = probe["status"] == 200
        if probe["redirected_to_root"]:
            result["problems"].append(
                f"verify_url redirects to the site home page ({probe['final_url']})")
        elif probe["status"] in BLOCKED_STATUSES:
            # Whether the adapter can still read the source is settled by the
            # parse check below, which is the authoritative one.
            result["notes"].append(
                f"verify_url refused automated checking (HTTP {probe['status']}); "
                "judged by the parse check instead")
        elif not result["reachable"]:
            result["problems"].append(f"verify_url returned {probe['status']}")
    elif cfg.get("path"):
        local = root / cfg["path"]
        result["reachable"] = local.exists()
        if not result["reachable"]:
            result["problems"].append(f"curated file missing: {cfg['path']}")
    else:
        result["problems"].append("no verify_url and no path: source is unverifiable")

    # --- parseable? -------------------------------------------------------
    local_cfg = dict(cfg)
    if "path" in local_cfg:
        local_cfg["path"] = str(root / local_cfg["path"])
    try:
        import inspect
        takes_now = "now" in inspect.signature(adapter.fetch_raw).parameters
        raw = (adapter.fetch_raw(local_cfg, now=datetime.now(UK))
               if takes_now else adapter.fetch_raw(local_cfg))
        events = adapter.parse(raw, local_cfg)
        result["parseable"] = True
        result["events"] = len(events)
        if not events:
            # A knowingly dormant list (a society that has not yet published
            # next term) would otherwise fail the verifier every week, and a
            # standing false alarm is worse than no alarm.
            if cfg.get("expect_events", True):
                result["problems"].append("parsed successfully but produced no events")
            else:
                result["notes"].append(
                    "no events published yet; the registry says to expect that")
        fetch_outcome = getattr(adapter, "last_fetch", {}) or {}
        if fetch_outcome.get("reachable") is False:
            result["reachable"] = False
            result["notes"].append(
                "the venue refused automated access "
                f"({fetch_outcome.get('reason')}); the record was built from "
                "configuration only and nothing was confirmed today")
    except Exception as exc:                            # noqa: BLE001
        result["parseable"] = False
        result["problems"].append(f"{type(exc).__name__}: {exc}")
        return result

    # --- current?  (hand-written claims only) -----------------------------
    # The registry decides, with the adapter kind as the default: a `curated`
    # source is hand-written unless it says otherwise, and any other source can
    # opt in with `hand_written: true`.  Data is only a fallback signal, so an
    # entry that forgets both `provenance` and `verified_on` cannot escape the
    # per-entry checks by omission.
    hand_written = cfg.get("hand_written", cfg["kind"] == "curated") or any(
        e.provenance or e.verified_on for e in events)
    if hand_written and check_entries:
        today = datetime.now(UK).date()
        for e in events:
            verified = _iso_date(e.verified_on)
            age_days = (today - verified).days if verified else None
            probe = check_link(e.url) if e.url else {"status": None,
                                                     "redirected_to_root": False,
                                                     "final_url": None}
            entry = {
                "id": e.id,
                "title": e.title,
                "url": e.url,
                "http_status": probe["status"],
                "redirected_to_root": probe["redirected_to_root"],
                "verified_on": verified.isoformat() if verified else None,
                "verified_days_ago": age_days,
                "provenance": e.provenance,
                "state": "ok",
            }
            if probe["status"] in BLOCKED_STATUSES:
                entry["state"] = "link_blocked"
            elif probe["status"] != 200:
                entry["state"] = "link_broken"
            elif probe["redirected_to_root"]:
                entry["state"] = "link_redirected"
            elif verified is None:
                entry["state"] = "never_verified"
            elif age_days > VERIFY_AFTER_DAYS:
                entry["state"] = "verification_expired"
            result["entries"].append(entry)

        overdue = [x for x in result["entries"]
                   if x["state"] not in ("ok", "link_blocked")]
        blocked = [x for x in result["entries"] if x["state"] == "link_blocked"]
        if blocked:
            result["blocked"] = len(blocked)
        if overdue:
            result["problems"].append(
                f"{len(overdue)} of {len(result['entries'])} entries need attention")
    return result


def verify_all(sources_path: Path, *, root: Path = ROOT,
               only: list[str] | None = None, check_entries: bool = True) -> dict:
    sources = load_sources(sources_path)
    if only:
        sources = [s for s in sources if s["key"] in only]
    results = [verify_source(cfg, root=root, check_entries=check_entries)
               for cfg in sources]
    return {
        "checked_at": datetime.now(UK).isoformat(),
        "verify_after_days": VERIFY_AFTER_DAYS,
        "source_count": len(results),
        "ok": all(not r["problems"] for r in results),
        "sources": results,
    }


# ---------------------------------------------------------------- output ----

_STATE_TEXT = {
    "ok": "ok",
    "link_broken": "LINK BROKEN",
    "link_blocked": "link refused automated checking (needs a human to open it)",
    "link_redirected": "LINK REDIRECTED to home page",
    "never_verified": "never verified by a human",
    "verification_expired": "verification expired",
}


def format_text(report: dict) -> str:
    lines = [f"Sources in the registry: {report['source_count']}",
             f"Checked at: {report['checked_at'][:19]}", ""]
    for r in report["sources"]:
        mark = "PASS" if not r["problems"] else "FAIL"
        if not r["problems"] and r["notes"]:
            mark = "PASS*"
        lines.append(f"[{mark}] {r['key']}  ({r['kind']})")
        lines.append(f"       name       {r['name']}")
        lines.append(f"       homepage   {r['homepage']}")
        lines.append(f"       verify     {r['verify_url'] or '(local file)'}"
                     f"  -> HTTP {r['http_status']}")
        lines.append(f"       parsed     {r['events']} events"
                     f"   adapter last confirmed {r['adapter_verified_on']}")
        lines.append(f"       terms      {r['terms']}")
        for p in r["problems"]:
            lines.append(f"       PROBLEM    {p}")
        for n in r["notes"]:
            lines.append(f"       note       {n}")
        for e in r["entries"]:
            if e["state"] == "ok":
                continue
            age = ("never" if e["verified_days_ago"] is None
                   else f"{e['verified_days_ago']}d ago")
            lines.append(f"       - {_STATE_TEXT[e['state']]:<52} {e['title'][:40]}"
                         f"  (HTTP {e['http_status']}, verified {age})")
        lines.append("")
    due = sum(1 for r in report["sources"] for e in r["entries"] if e["state"] != "ok")
    lines.append(f"Hand-written entries needing attention: {due}")
    lines.append("All sources verified." if report["ok"] else "SOME SOURCES NEED ATTENTION.")
    return "\n".join(lines)


def format_markdown(report: dict) -> str:
    """Body for the weekly review issue."""
    out = ["## Source verification",
           "",
           f"Checked {report['checked_at'][:16]} · "
           f"{report['source_count']} sources in the registry · "
           f"hand-written claims expire after {report['verify_after_days']} days.",
           "",
           "| Source | Kind | Reachable | Parsed | Verified | Problems |",
           "|---|---|---|---|---|---|"]
    for r in report["sources"]:
        reach = f"{r['http_status'] or 'local'}"
        cell = "; ".join(r["problems"])
        if r["notes"]:
            cell = "; ".join(filter(None, [cell, "_" + "; ".join(r["notes"]) + "_"]))
        out.append(f"| [{r['name']}]({r['homepage']}) | `{r['kind']}` | {reach} | "
                   f"{r['events']} | {r['adapter_verified_on']} | {cell or '—'} |")
    todo = [(r, e) for r in report["sources"] for e in r["entries"]
            if e["state"] != "ok"]
    if todo:
        out += ["", "## Hand-written entries to re-check", ""]
        for r, e in todo:
            detail = _STATE_TEXT[e["state"]]
            if e["http_status"] not in (200, None):
                detail += f" (HTTP {e['http_status']})"
            if e["verified_days_ago"] is not None:
                detail += f", last verified {e['verified_days_ago']} days ago"
            out.append(f"- [ ] **{e['title']}** — {detail}. "
                       f"[Open the venue page]({e['url']}) and confirm the dates, "
                       f"price and age rule, then set `verified_on` in "
                       f"`{r['homepage'].rsplit('/', 1)[-1] if r['kind'] == 'curated' else r['key']}`.")
    else:
        out += ["", "Nothing to re-check.", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sources", type=Path, default=ROOT / "collector/sources.yaml")
    ap.add_argument("--only", nargs="*", help="only these source keys")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--no-entry-checks", action="store_true",
                    help="skip the per-entry link and expiry checks")
    args = ap.parse_args(argv)

    report = verify_all(args.sources, only=args.only,
                        check_entries=not args.no_entry_checks)
    if args.json:
        print(json.dumps(report, indent=1))
    elif args.markdown:
        print(format_markdown(report))
    else:
        print(format_text(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
