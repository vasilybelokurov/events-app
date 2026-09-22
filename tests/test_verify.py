"""Tests for the source verifier.

The requirement these lock down: **every source must be listed in the registry
and must be verifiable on demand.** A source that cannot be checked is itself a
failure, not a gap in the report.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import yaml

from collector import adapters as adapters_mod
from collector import verify as verify_mod
from collector.models import UK, Event


class StubAdapter:
    def __init__(self, events=None, error=None):
        self.events = events or []
        self.error = error

    def fetch_raw(self, cfg):
        if self.error:
            raise self.error
        return "{}"

    def parse(self, raw, cfg):
        return list(self.events)


def ev(**kw) -> Event:
    base = dict(id="c-1", title="Old Bailey gallery", url="https://example.org/ob",
                source="curated_x", source_name="Curated", start="2026-10-01T10:00:00+01:00")
    base.update(kw)
    return Event(**base)


@pytest.fixture
def stub(monkeypatch):
    adapter = StubAdapter()
    monkeypatch.setitem(adapters_mod.REGISTRY, "stub", adapter)
    monkeypatch.setattr(verify_mod, "check_link",
                        lambda url: {"status": 200, "final_url": url,
                                     "redirected_to_root": False})
    return adapter


def cfg(**kw) -> dict:
    base = dict(key="stubby", name="Stub", kind="stub",
                homepage="https://example.org/whats-on",
                verify_url="https://example.org/feed",
                terms="https://example.org/terms",
                verified="2026-09-21")
    base.update(kw)
    return base


class TestRegistryContract:
    def test_a_complete_entry_passes(self, stub):
        stub.events = [ev()]
        r = verify_mod.verify_source(cfg())
        assert r["problems"] == []
        assert r["reachable"] is True and r["parseable"] is True
        assert r["events"] == 1

    @pytest.mark.parametrize("missing", ["name", "homepage", "kind"])
    def test_a_missing_registry_field_is_a_problem(self, stub, missing):
        stub.events = [ev()]
        broken = cfg()
        broken[missing] = None
        r = verify_mod.verify_source(broken)
        assert any(missing in p for p in r["problems"])

    def test_missing_terms_is_reported(self, stub):
        stub.events = [ev()]
        r = verify_mod.verify_source(cfg(terms=None))
        assert any("terms" in p for p in r["problems"])

    def test_a_source_with_no_way_to_check_it_fails(self, stub):
        """'Unverifiable' is a failure in its own right."""
        stub.events = [ev()]
        r = verify_mod.verify_source(cfg(verify_url=None))
        assert any("unverifiable" in p for p in r["problems"])

    def test_an_unknown_adapter_is_reported(self):
        r = verify_mod.verify_source(cfg(kind="carrier_pigeon"))
        assert any("carrier_pigeon" in p for p in r["problems"])


class TestReachableVersusParseable:
    def test_a_200_that_no_longer_parses_is_distinguished(self, stub):
        """A redesigned page answers 200 but yields nothing: reachable, not
        parseable. Collapsing the two would hide exactly this case."""
        stub.error = ValueError("selectors matched nothing")
        r = verify_mod.verify_source(cfg())
        assert r["reachable"] is True
        assert r["parseable"] is False
        assert any("ValueError" in p for p in r["problems"])

    def test_unreachable_is_reported_with_its_status(self, stub, monkeypatch):
        monkeypatch.setattr(verify_mod, "check_link",
                            lambda url: {"status": 404, "final_url": url,
                                         "redirected_to_root": False})
        stub.events = [ev()]
        r = verify_mod.verify_source(cfg())
        assert r["reachable"] is False
        assert any("404" in p for p in r["problems"])

    def test_redirect_to_home_page_is_not_counted_as_healthy(self, stub, monkeypatch):
        monkeypatch.setattr(verify_mod, "check_link",
                            lambda url: {"status": 200, "final_url": "https://example.org/",
                                         "redirected_to_root": True})
        stub.events = [ev()]
        r = verify_mod.verify_source(cfg())
        assert any("home page" in p for p in r["problems"])

    def test_parsing_to_zero_events_is_reported(self, stub):
        stub.events = []
        r = verify_mod.verify_source(cfg())
        assert any("no events" in p for p in r["problems"])


class TestCuratedClaimExpiry:
    """A resolving link does not prove a price or an age rule still holds."""

    def test_a_curated_entry_cannot_escape_checks_by_omitting_its_fields(self, stub):
        """An entry with neither provenance nor verified_on is still checked,
        because the registry declares the source hand-written."""
        stub.events = [ev(verified_on=None, provenance=None)]
        r = verify_mod.verify_source(cfg(key="curated_x", hand_written=True))
        assert r["entries"] and r["entries"][0]["state"] == "never_verified"

    def _check(self, stub, verified_on):
        stub.events = [ev(verified_on=verified_on)]
        return verify_mod.verify_source(
            cfg(key="curated_x", hand_written=True))["entries"][0]

    def test_recently_verified_is_ok(self, stub):
        today = datetime.now(UK).date()
        assert self._check(stub, today.isoformat())["state"] == "ok"

    def test_never_verified_is_flagged(self, stub):
        assert self._check(stub, None)["state"] == "never_verified"

    def test_expired_verification_is_flagged(self, stub):
        old = (datetime.now(UK).date()
               - timedelta(days=verify_mod.VERIFY_AFTER_DAYS + 1)).isoformat()
        entry = self._check(stub, old)
        assert entry["state"] == "verification_expired"
        assert entry["verified_days_ago"] > verify_mod.VERIFY_AFTER_DAYS

    def test_just_inside_the_window_is_ok(self, stub):
        edge = (datetime.now(UK).date()
                - timedelta(days=verify_mod.VERIFY_AFTER_DAYS)).isoformat()
        assert self._check(stub, edge)["state"] == "ok"

    def test_broken_link_outranks_a_fresh_verification(self, stub, monkeypatch):
        monkeypatch.setattr(verify_mod, "check_link",
                            lambda url: {"status": 404, "final_url": url,
                                         "redirected_to_root": False})
        today = datetime.now(UK).date().isoformat()
        assert self._check(stub, today)["state"] == "link_broken"

    def test_entry_problems_roll_up_to_the_source(self, stub):
        stub.events = [ev(id="a", verified_on=None), ev(id="b", verified_on=None)]
        r = verify_mod.verify_source(cfg(key="curated_x", hand_written=True))
        assert any("2 of 2 entries" in p for p in r["problems"])


class TestWholeRegistry:
    def test_verify_all_reports_every_source(self, stub, tmp_path):
        path = tmp_path / "sources.yaml"
        path.write_text(yaml.safe_dump({"sources": [cfg(key="a"), cfg(key="b")]}))
        stub.events = [ev()]
        report = verify_mod.verify_all(path)
        assert report["source_count"] == 2
        assert [s["key"] for s in report["sources"]] == ["a", "b"]
        assert report["ok"] is True

    def test_exit_status_is_nonzero_when_something_needs_attention(self, stub, tmp_path):
        path = tmp_path / "sources.yaml"
        path.write_text(yaml.safe_dump({"sources": [cfg(terms=None)]}))
        stub.events = [ev()]
        assert verify_mod.main(["--sources", str(path)]) == 1

    def test_exit_status_is_zero_when_all_pass(self, stub, tmp_path):
        path = tmp_path / "sources.yaml"
        path.write_text(yaml.safe_dump({"sources": [cfg()]}))
        stub.events = [ev()]
        assert verify_mod.main(["--sources", str(path)]) == 0

    def test_markdown_output_lists_sources_and_todo_items(self, stub, tmp_path, capsys):
        path = tmp_path / "sources.yaml"
        path.write_text(yaml.safe_dump({"sources": [cfg(key="curated_x",
                                                        hand_written=True)]}))
        stub.events = [ev(verified_on=None)]
        verify_mod.main(["--sources", str(path), "--markdown"])
        out = capsys.readouterr().out
        assert "## Source verification" in out
        assert "https://example.org/whats-on" in out
        assert "Old Bailey gallery" in out
        assert "- [ ]" in out, "overdue entries should be a checklist"


class TestTheRealRegistry:
    """The registry shipped in this repository must satisfy its own contract."""

    def test_every_source_declares_what_the_verifier_needs(self, sources):
        for key, cfg_ in sources.items():
            for field in ("key", "name", "kind", "homepage", "docs", "terms", "priority"):
                assert cfg_.get(field) is not None, f"{key} is missing {field}"
            assert "verify_url" in cfg_, f"{key} has no verify_url field"
            assert cfg_.get("verify_url") or cfg_.get("path"), \
                f"{key} is unverifiable: no verify_url and no local path"

    def test_source_keys_are_unique_and_prefix_event_ids(self, sources):
        assert len(sources) == len(set(sources))
        for key in sources:
            assert key.replace("_", "").isalnum(), f"{key} is not id-safe"


class TestBlockedVersusBroken:
    """Several venues serve 403 to datacentre addresses, so the same URL reads
    200 from a laptop and 403 from a CI runner.  Reporting that as a broken
    link every Monday would train the reader to ignore the report."""

    def _entry(self, stub, status):
        stub.events = [ev(verified_on=None)]
        return verify_mod.verify_source(cfg(key="curated_x", hand_written=True))["entries"][0]

    @pytest.mark.parametrize("status", [401, 403, 429])
    def test_refusal_is_reported_as_blocked(self, stub, monkeypatch, status):
        monkeypatch.setattr(verify_mod, "check_link",
                            lambda url: {"status": status, "final_url": url,
                                         "redirected_to_root": False})
        assert self._entry(stub, status)["state"] == "link_blocked"

    @pytest.mark.parametrize("status", [404, 410, 500])
    def test_gone_is_reported_as_broken(self, stub, monkeypatch, status):
        monkeypatch.setattr(verify_mod, "check_link",
                            lambda url: {"status": status, "final_url": url,
                                         "redirected_to_root": False})
        assert self._entry(stub, status)["state"] == "link_broken"

    def test_a_blocked_link_alone_does_not_fail_the_source(self, stub, monkeypatch):
        monkeypatch.setattr(verify_mod, "check_link",
                            lambda url: {"status": 403, "final_url": url,
                                         "redirected_to_root": False})
        today = datetime.now(UK).date().isoformat()
        stub.events = [ev(verified_on=today)]
        r = verify_mod.verify_source(cfg(key="curated_x", hand_written=True))
        assert r["problems"] == []
        assert r["blocked"] == 1

    def test_the_status_code_appears_in_the_report(self, stub, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(verify_mod, "check_link",
                            lambda url: {"status": 403, "final_url": url,
                                         "redirected_to_root": False})
        stub.events = [ev(verified_on=None)]
        path = tmp_path / "sources.yaml"
        path.write_text(yaml.safe_dump({"sources": [cfg(key="curated_x",
                                                        hand_written=True)]}))
        verify_mod.main(["--sources", str(path), "--markdown"])
        assert "HTTP 403" in capsys.readouterr().out


class TestHandWrittenDetection:
    """A note the collector wrote itself is not a human's signature."""

    def test_provenance_alone_does_not_make_a_source_hand_written(self, stub):
        """It flipped 100 scraped listings onto the re-checking list.

        The trigger was one venue publishing an end date before its own start,
        which the collector records in `provenance` when it drops it.
        """
        stub.events = [ev(provenance="the source gave an end before the start")]
        report = verify_mod.verify_source(cfg())
        assert not report["entries"]
        assert not report["problems"]

    def test_verified_on_still_makes_it_hand_written(self, stub):
        """A dated human claim must stay on the checklist."""
        stub.events = [ev(verified_on="2020-01-01")]
        report = verify_mod.verify_source(cfg())
        assert report["entries"], "a dated human claim escaped the per-entry check"
