"""Build-pipeline tests, run entirely offline against a stub adapter.

These lock the publication policy: a broken source must not be able to erase
its own records, quietly publish a hole, or acquire a fresh "last refreshed"
timestamp it did not earn.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from collector import adapters as adapters_mod
from collector import build as build_mod
from collector.models import UK, Event, make_id

ROOT = Path(__file__).resolve().parent.parent

STUB_KIND = "stub"


class StubAdapter:
    """Adapter double whose behaviour each test sets."""

    def __init__(self):
        self.events: list[Event] = []
        self.error: Exception | None = None
        self.meta: dict = {"pagination_complete": True}

    def fetch_raw(self, cfg, now=None):
        if self.error:
            raise self.error
        return json.dumps({"meta": self.meta})

    def parse(self, raw, cfg):
        return [Event(**{**vars(e), "source": cfg["key"],
                         "source_name": cfg["name"]}) for e in self.events]


@pytest.fixture
def stub(monkeypatch):
    adapter = StubAdapter()
    monkeypatch.setitem(adapters_mod.REGISTRY, STUB_KIND, adapter)
    # No network in these tests.
    monkeypatch.setattr(build_mod, "check_link",
                        lambda url: {"status": 200, "final_url": url,
                                     "redirected_to_root": False})
    return adapter


@pytest.fixture
def paths(tmp_path):
    src = tmp_path / "sources.yaml"
    src.write_text(yaml.safe_dump({"sources": [
        {"key": "stubby", "name": "Stub source", "kind": STUB_KIND, "priority": 10},
    ]}))
    return src, tmp_path / "out" / "events.json", tmp_path


def future(days: int, hour: int = 19) -> str:
    d = datetime.now(UK) + timedelta(days=days)
    return d.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


def ev(n: int, **kw) -> Event:
    base = dict(id=make_id("stubby", str(n)), title=f"Event {n}",
                url=f"https://example.org/{n}", source="stubby",
                source_name="Stub source", start=future(n + 1))
    base.update(kw)
    return Event(**base)


def run(paths, **kw):
    src, out, root = paths
    return build_mod.build(src, out, root=root, link_check=False, **kw)


def run_cli(paths, *extra):
    """Invoke the command line, whose exit status is the alarm."""
    src, out, _ = paths
    return build_mod.main(["--sources", str(src), "--out", str(out),
                           "--no-link-check", *extra])


class TestHappyPath:
    def test_writes_events_and_a_source_report(self, stub, paths):
        stub.events = [ev(1), ev(2)]
        doc = run(paths)
        assert doc["event_count"] == 2
        assert doc["sources"][0]["status"] == "ok"
        assert doc["sources"][0]["last_success"]
        assert paths[1].exists()

    def test_output_is_valid_json_with_the_expected_shape(self, stub, paths):
        stub.events = [ev(1)]
        run(paths)
        doc = json.loads(paths[1].read_text())
        assert set(doc) >= {"generated_at", "timezone", "event_count", "sources",
                            "events", "stale_after_hours"}
        assert doc["events"][0]["id"].startswith("stubby-")
        assert doc["events"][0]["last_seen"]

    def test_no_temporary_file_is_left_behind(self, stub, paths):
        stub.events = [ev(1)]
        run(paths)
        assert not list(paths[1].parent.glob("*.tmp"))


class TestHorizonAndPastEvents:
    def test_past_events_are_dropped(self, stub, paths):
        stub.events = [ev(1, start=future(-30)), ev(2)]
        doc = run(paths)
        assert doc["event_count"] == 1

    def test_a_run_that_has_started_but_not_finished_is_kept(self, stub, paths):
        """An exhibition that opened last month and closes in January is
        current, even though its start date is in the past."""
        stub.events = [ev(1, start=future(-30), end=future(60), ongoing=True)]
        assert run(paths)["event_count"] == 1

    def test_events_beyond_the_horizon_are_dropped(self, stub, paths):
        stub.events = [ev(1, start=future(900))]
        assert run(paths)["event_count"] == 0


class TestFailurePolicy:
    """Degrade, do not freeze.

    An earlier version refused to write anything when any source misbehaved,
    so a one-hour outage at one venue froze the whole page for a day.  The
    policy now is: publish the healthy sources, carry the broken one over,
    and raise the alarm through the exit code.
    """

    def test_fetch_error_is_recorded_not_raised(self, stub, paths):
        stub.events = [ev(1)]
        run(paths)                                  # establish a good build
        stub.error = RuntimeError("venue down")
        doc = run(paths)
        report = doc["sources"][0]
        assert report["status"] == "fetch_error"
        assert "venue down" in report["error"]

    def test_records_are_carried_over_when_a_source_fails(self, stub, paths):
        stub.events = [ev(1), ev(2)]
        first = run(paths)
        original_seen = first["events"][0]["last_seen"]

        stub.error = RuntimeError("venue down")
        doc = run(paths)
        assert doc["event_count"] == 2, "a failed source must not erase its events"
        assert doc["sources"][0]["carried_over"] == 2
        assert doc["events"][0]["last_seen"] == original_seen, \
            "carried-over records must keep their original timestamp"

    def test_a_failed_source_does_not_get_a_fresh_success_stamp(self, stub, paths):
        stub.events = [ev(1)]
        good = run(paths)
        stamp = good["sources"][0]["last_success"]
        stub.error = RuntimeError("down")
        doc = run(paths)
        assert doc["sources"][0]["last_success"] == stamp

    def test_a_dead_source_does_not_stop_the_others_publishing(self, stub, paths, tmp_path):
        """The regression that matters: one venue being down must not freeze
        the whole page."""
        src, out, root = paths
        second = StubAdapter()
        second.events = [ev(50), ev(51)]
        import collector.adapters as adapters_mod
        adapters_mod.REGISTRY["stub2"] = second
        try:
            src.write_text(yaml.safe_dump({"sources": [
                {"key": "stubby", "name": "Stub source", "kind": STUB_KIND, "priority": 10},
                {"key": "healthy", "name": "Healthy source", "kind": "stub2", "priority": 20},
            ]}))
            stub.events = [ev(1)]
            run(paths)                              # both good

            stub.error = RuntimeError("down")
            doc = run(paths)
            by_key = {s["key"]: s for s in doc["sources"]}
            assert by_key["stubby"]["status"] == "fetch_error"
            assert by_key["healthy"]["status"] == "ok"
            assert by_key["healthy"]["last_success"] == doc["generated_at"]
            titles = {e["title"] for e in doc["events"]}
            assert {"Event 50", "Event 51"} <= titles, "healthy source was not published"
            assert "Event 1" in titles, "failed source's records were not carried over"
        finally:
            adapters_mod.REGISTRY.pop("stub2", None)

    def test_dropping_to_zero_is_flagged(self, stub, paths):
        stub.events = [ev(1), ev(2)]
        run(paths)
        stub.events = []
        doc = run(paths)
        assert doc["sources"][0]["status"] == "empty"
        assert any("returned 0 events" in w for w in doc["build_warnings"])

    def test_losing_most_events_is_flagged(self, stub, paths):
        stub.events = [ev(i) for i in range(10)]
        run(paths)
        stub.events = [ev(0), ev(1)]
        doc = run(paths)
        assert doc["sources"][0]["status"] == "shrunk"

    def test_a_suspicious_drop_keeps_the_previous_records(self, stub, paths):
        stub.events = [ev(i) for i in range(10)]
        run(paths)
        stub.events = []
        doc = run(paths)
        assert doc["event_count"] == 10, "records were dropped instead of carried over"
        assert doc["sources"][0]["carried_over"] == 10

    def test_a_dead_source_stops_showing_events_that_have_passed(self, stub, paths):
        """Carried records were never re-checked against the calendar.

        A venue that goes away for good keeps failing, so its records were
        carried for ever -- including after their own dates had gone by.
        """
        soon, later = ev(1), ev(2)
        soon.start = (datetime.now(UK) + timedelta(days=1)).isoformat()
        later.start = (datetime.now(UK) + timedelta(days=30)).isoformat()
        stub.events = [soon, later]
        run(paths)

        stub.error = RuntimeError("venue gone")
        doc = run(paths)                       # fails: both carried, both future
        assert doc["event_count"] == 2

        # A fortnight later the first one is in the past.
        doc = run(paths, now=datetime.now(UK) + timedelta(days=14))
        titles = {e["title"] for e in doc["events"]}
        assert titles == {"Event 2"}, "a finished event was carried anyway"
        assert doc["sources"][0]["carried_expired"] == 1
        assert doc["sources"][0]["count"] == 1

    def test_degraded_build_exits_nonzero(self, stub, paths, capsys):
        stub.events = [ev(1), ev(2)]
        assert run_cli(paths) == 0
        stub.events = []
        assert run_cli(paths) == 1, "a degraded source must turn the run red"
        assert "degraded" in capsys.readouterr().out

    def test_allow_drop_exits_zero(self, stub, paths):
        stub.events = [ev(1), ev(2)]
        run_cli(paths)
        stub.events = []
        assert run_cli(paths, "--allow-drop") == 0

    def test_allow_drop_actually_publishes_the_smaller_set(self, stub, paths):
        """It used to change only the exit code.

        The shrunken source still published its *old* records and the baseline
        never moved, so the next run raised the same alarm for ever and the
        documented "accepts a genuine shrinkage" was not true of the data.
        """
        stub.events = [ev(i) for i in range(10)]
        run(paths)
        stub.events = [ev(1), ev(2)]
        assert run_cli(paths, "--allow-drop") == 0
        doc = json.loads(paths[1].read_text())
        assert doc["event_count"] == 2, "the old records were carried anyway"
        src = doc["sources"][0]
        assert src["status"] == "ok"
        assert not src.get("carried_over")
        assert src["last_good_count"] == 2, "the baseline did not move"
        assert src["accepted_drop"] == {"from": 10, "to": 2}
        assert "build_warnings" not in doc

    def test_the_next_run_is_compared_against_the_accepted_count(self, stub, paths):
        stub.events = [ev(i) for i in range(10)]
        run(paths)
        stub.events = [ev(1), ev(2)]
        run_cli(paths, "--allow-drop")
        doc = run(paths)                      # same two events, no flag
        assert doc["sources"][0]["status"] == "ok"
        assert "build_warnings" not in doc

    def test_allow_drop_does_not_accept_a_truncated_crawl(self, stub, paths):
        """A known-incomplete walk is not a shrinkage anybody can accept."""
        stub.events = [ev(i) for i in range(10)]
        run(paths)
        stub.events = [ev(1)]
        stub.meta = {"pagination_complete": False}
        doc = run(paths, allow_drop=True)
        # The label is "shrunk" because the count check runs first; what
        # matters is that the flag did not accept it and nothing was lost.
        assert doc["sources"][0]["status"] != "ok"
        assert doc["event_count"] == 10, "a truncated crawl must still carry"

    def test_incomplete_pagination_is_flagged(self, stub, paths):
        stub.events = [ev(1)]
        stub.meta = {"pagination_complete": False}
        doc = run(paths)
        assert doc["sources"][0]["status"] == "partial"

    def test_first_ever_build_with_no_events_is_allowed(self, stub, paths):
        """Nothing to compare against yet, so an empty result is not a drop."""
        stub.events = []
        doc = run(paths)
        assert doc["event_count"] == 0
        assert doc["sources"][0]["status"] == "ok"


class TestRegistryMetadata:
    def test_source_links_are_published_for_the_page(self, stub, paths, tmp_path):
        """Every source must be listed *and* openable from the page itself."""
        src, out, root = paths
        src.write_text(yaml.safe_dump({"sources": [{
            "key": "stubby", "name": "Stub source", "kind": STUB_KIND,
            "homepage": "https://example.org/whats-on",
            "verify_url": "https://example.org/feed.ics",
            "terms": "https://example.org/terms",
            "verified": "2026-09-21", "priority": 10,
        }]}))
        stub.events = [ev(1)]
        report = run(paths)["sources"][0]
        assert report["homepage"] == "https://example.org/whats-on"
        assert report["verify_url"] == "https://example.org/feed.ics"
        assert report["terms"] == "https://example.org/terms"
        assert report["adapter_verified_on"] == "2026-09-21"


class TestFreshness:
    def test_oldest_source_success_is_reported(self, stub, paths):
        stub.events = [ev(1)]
        doc = run(paths)
        assert doc["oldest_source_success"] == doc["sources"][0]["last_success"]

    def test_stale_threshold_is_published_for_the_page(self, stub, paths):
        stub.events = [ev(1)]
        assert run(paths)["stale_after_hours"] == build_mod.STALE_AFTER_HOURS


class TestPartialResults:
    """A truncated crawl looks exactly like a successful one, so it must not
    replace a complete dataset or earn a fresh `last_success`."""

    def test_a_partial_crawl_keeps_the_previous_records(self, stub, paths):
        stub.events = [ev(i) for i in range(6)]
        good = run(paths)
        stamp = good["sources"][0]["last_success"]

        stub.events = [ev(0), ev(1), ev(2), ev(3)]      # not a >50% drop
        stub.meta = {"pagination_complete": False}
        doc = run(paths)
        report = doc["sources"][0]
        assert report["status"] == "partial"
        assert report["carried_over"] == 6, "the complete dataset was replaced"
        assert doc["event_count"] == 6
        assert report["last_success"] == stamp, \
            "an incomplete crawl must not advance last_success"

    def test_a_partial_crawl_is_a_warning(self, stub, paths):
        stub.events = [ev(1)]
        stub.meta = {"pagination_complete": False}
        doc = run(paths)
        assert any("pagination incomplete" in w for w in doc["build_warnings"])


class TestDegradationBaseline:
    """Two consecutive failures must not ratchet the baseline down to nothing.

    The bug: drop detection compared against the *previous run's* count, which
    a degraded run had already set to the bad candidate figure.  So 10 events,
    then 0 (carried over), then 1 looked like a rise on the failed run and was
    published as healthy -- ten good records silently replaced by one.
    """

    def settle(self, stub, paths, n=10):
        stub.events = [ev(i) for i in range(n)]
        doc = run(paths)
        assert doc["sources"][0]["last_good_count"] == n
        return doc

    def test_empty_then_small_does_not_establish_a_new_baseline(self, stub, paths):
        self.settle(stub, paths)

        stub.events = []
        doc = run(paths)
        assert doc["sources"][0]["status"] == "empty"
        assert doc["sources"][0]["last_good_count"] == 10, "baseline moved"

        stub.events = [ev(0)]
        doc = run(paths)
        report = doc["sources"][0]
        assert report["status"] == "shrunk", \
            "1 event against a 10-event baseline is a drop, not a recovery"
        assert report["count"] == 10, "the last good records were replaced"
        assert doc["event_count"] == 10

    def test_partial_then_smaller_partial_keeps_the_original_records(self, stub, paths):
        self.settle(stub, paths)

        # 6 of 10 is not a >50% drop, so `partial` is the reason it is held
        # back rather than `shrunk`.
        stub.events = [ev(i) for i in range(6)]
        stub.meta = {"pagination_complete": False}
        doc = run(paths)
        assert doc["sources"][0]["status"] == "partial"

        stub.events = [ev(0), ev(1)]
        doc = run(paths)
        assert doc["event_count"] == 10
        assert doc["sources"][0]["last_good_count"] == 10

    def test_fetch_error_then_a_small_success_is_still_a_drop(self, stub, paths):
        self.settle(stub, paths)

        stub.error = RuntimeError("venue down")
        doc = run(paths)
        assert doc["sources"][0]["status"] == "fetch_error"
        assert doc["sources"][0]["last_good_count"] == 10

        stub.error = None
        stub.events = [ev(0), ev(1)]
        doc = run(paths)
        assert doc["sources"][0]["status"] == "shrunk"
        assert doc["event_count"] == 10

    def test_a_genuine_recovery_resets_the_baseline(self, stub, paths):
        """The safeguard must not become a trap: a source that comes back
        properly is accepted and becomes the new baseline."""
        self.settle(stub, paths)
        stub.events = []
        run(paths)

        stub.events = [ev(i) for i in range(9)]
        doc = run(paths)
        report = doc["sources"][0]
        assert report["status"] == "ok"
        assert report["last_good_count"] == 9
        assert report["last_success"] == doc["generated_at"]
        assert doc["event_count"] == 9

    def test_candidate_and_published_counts_are_both_reported(self, stub, paths):
        self.settle(stub, paths)
        stub.events = [ev(0)]
        report = run(paths)["sources"][0]
        assert report["candidate_count"] == 1, "what the source offered"
        assert report["count"] == 10, "what was actually published"


class TestBlockedSource:
    """A source that could not be reached is never recorded as verified, even
    when the adapter can still publish something from its configuration."""

    def blocked(self, stub, paths, reason="HTTPError: 403"):
        stub.meta = {"reachable": False, "reason": reason}
        return run(paths)["sources"][0]

    def test_status_is_blocked_not_ok(self, stub, paths):
        stub.events = [ev(1)]
        assert self.blocked(stub, paths)["status"] == "blocked"

    def test_last_success_is_not_advanced(self, stub, paths):
        stub.events = [ev(1)]
        good = run(paths)
        stamp = good["sources"][0]["last_success"]

        report = self.blocked(stub, paths)
        assert report["last_success"] == stamp, \
            "an unreachable source must not claim a fresh verification"
        assert report["last_attempt"] != stamp, "the attempt should be recorded"

    def test_the_reason_is_recorded(self, stub, paths):
        stub.events = [ev(1)]
        assert "403" in self.blocked(stub, paths)["error"]

    def test_the_record_is_still_published(self, stub, paths):
        """The standing card remains useful; it just says it was not checked."""
        stub.events = [ev(1)]
        stub.meta = {"reachable": False, "reason": "HTTPError: 403"}
        doc = run(paths)
        assert doc["event_count"] == 1

    def test_being_blocked_is_not_a_build_warning(self, stub, paths):
        """A venue that always refuses robots would otherwise turn every run
        red, and a weekly false alarm trains the reader to ignore the report."""
        stub.events = [ev(1)]
        doc = run(paths)
        stub.meta = {"reachable": False, "reason": "HTTPError: 403"}
        doc = run(paths)
        assert not doc.get("build_warnings")
        assert run_cli(paths) == 0

    def test_a_blocked_run_does_not_reset_the_baseline(self, stub, paths):
        stub.events = [ev(i) for i in range(10)]
        run(paths)
        stub.events = [ev(0)]
        report = self.blocked(stub, paths)
        assert report["last_good_count"] == 10


class TestPublishedFileIntegrity:
    def test_the_published_file_is_valid_json_with_the_expected_shape(self, stub, paths):
        """Guard against a corrupted data file reaching the site: a `union`
        merge driver once interleaved two versions of events.json into
        something that parsed nowhere."""
        stub.events = [ev(1), ev(2)]
        run(paths)
        doc = json.loads(paths[1].read_text())
        assert isinstance(doc["events"], list)
        assert doc["event_count"] == len(doc["events"])
        for record in doc["events"]:
            assert record["id"] and record["title"] and record["url"]
            assert record["last_seen"].endswith("+00:00")


class TestTheRepositoryDataFile:
    """The committed data file must be valid.

    This exists because a corrupt `docs/data/events.json` was committed and
    pushed twice: a `union` merge driver interleaved two copies during a rebase,
    and nothing in the local suite noticed.  The file is a build artifact, so
    the fix is to regenerate it -- but the suite should refuse to pass while it
    is broken.
    """

    PATH = ROOT / "docs/data/events.json"

    @pytest.mark.skipif(not PATH.exists(), reason="no build has run here yet")
    def test_it_parses_and_is_internally_consistent(self):
        doc = json.loads(self.PATH.read_text(encoding="utf-8"))
        assert doc["event_count"] == len(doc["events"])
        assert doc["sources"], "no sources recorded"
        ids = [r["id"] for r in doc["events"]]
        assert len(ids) == len(set(ids)), "duplicate event ids"
        for record in doc["events"]:
            assert record["title"] and record["url"], record["id"]
            assert record["source"] in {s["key"] for s in doc["sources"]}, \
                f"{record['id']} comes from a source that is not in the registry"

    @pytest.mark.skipif(not PATH.exists(), reason="no build has run here yet")
    def test_every_published_source_is_in_the_registry(self, sources):
        doc = json.loads(self.PATH.read_text(encoding="utf-8"))
        for s in doc["sources"]:
            assert s["key"] in sources, \
                f"{s['key']} is published but not in collector/sources.yaml"
