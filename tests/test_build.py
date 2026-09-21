"""Build-pipeline tests, run entirely offline against a stub adapter.

These lock the publication policy: a broken source must not be able to erase
its own records, quietly publish a hole, or acquire a fresh "last refreshed"
timestamp it did not earn.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
import yaml

from collector import adapters as adapters_mod
from collector import build as build_mod
from collector.models import UK, Event, make_id

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
    def test_fetch_error_is_recorded_not_raised(self, stub, paths):
        stub.events = [ev(1)]
        run(paths)                                  # establish a good build
        stub.error = RuntimeError("venue down")
        doc = run(paths, allow_drop=True)
        report = doc["sources"][0]
        assert report["status"] == "fetch_error"
        assert "venue down" in report["error"]

    def test_records_are_carried_over_when_a_source_fails(self, stub, paths):
        stub.events = [ev(1), ev(2)]
        first = run(paths)
        original_seen = first["events"][0]["last_seen"]

        stub.error = RuntimeError("venue down")
        doc = run(paths, allow_drop=True)
        assert doc["event_count"] == 2, "a failed source must not erase its events"
        assert doc["sources"][0]["carried_over"] == 2
        assert doc["events"][0]["last_seen"] == original_seen, \
            "carried-over records must keep their original timestamp"

    def test_a_failed_source_does_not_get_a_fresh_success_stamp(self, stub, paths):
        stub.events = [ev(1)]
        good = run(paths)
        stamp = good["sources"][0]["last_success"]
        stub.error = RuntimeError("down")
        doc = run(paths, allow_drop=True)
        assert doc["sources"][0]["last_success"] == stamp

    def test_dropping_to_zero_refuses_to_publish(self, stub, paths):
        stub.events = [ev(1), ev(2)]
        run(paths)
        stub.events = []
        with pytest.raises(SystemExit) as exc:
            run(paths)
        assert "returned 0 events" in str(exc.value)

    def test_losing_most_events_refuses_to_publish(self, stub, paths):
        stub.events = [ev(i) for i in range(10)]
        run(paths)
        stub.events = [ev(0), ev(1)]
        with pytest.raises(SystemExit):
            run(paths)

    def test_allow_drop_publishes_with_a_warning(self, stub, paths):
        stub.events = [ev(i) for i in range(10)]
        run(paths)
        stub.events = [ev(0)]
        doc = run(paths, allow_drop=True)
        assert doc["build_warnings"]
        assert doc["sources"][0]["status"] == "shrunk"

    def test_a_refused_build_leaves_the_previous_file_intact(self, stub, paths):
        stub.events = [ev(1), ev(2)]
        run(paths)
        before = paths[1].read_text()
        stub.events = []
        with pytest.raises(SystemExit):
            run(paths)
        assert paths[1].read_text() == before

    def test_incomplete_pagination_is_flagged(self, stub, paths):
        stub.events = [ev(1)]
        stub.meta = {"pagination_complete": False}
        doc = run(paths, allow_drop=True)
        assert doc["sources"][0]["status"] == "partial"

    def test_first_ever_build_with_no_events_is_allowed(self, stub, paths):
        """Nothing to compare against yet, so an empty result is not a drop."""
        stub.events = []
        doc = run(paths)
        assert doc["event_count"] == 0
        assert doc["sources"][0]["status"] == "ok"


class TestFreshness:
    def test_oldest_source_success_is_reported(self, stub, paths):
        stub.events = [ev(1)]
        doc = run(paths)
        assert doc["oldest_source_success"] == doc["sources"][0]["last_success"]

    def test_stale_threshold_is_published_for_the_page(self, stub, paths):
        stub.events = [ev(1)]
        assert run(paths)["stale_after_hours"] == build_mod.STALE_AFTER_HOURS
