"""Live source tests: they hit the real venue sites.

Run them deliberately::

    pytest -m network

They are the early warning that a source has changed shape, which fixtures
cannot give: a fixture keeps passing forever while the live page rots.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from collector.adapters import drupal_jsonapi, html_css
from collector.models import UK

pytestmark = pytest.mark.network


def _future_only(events, *, slack_days: int = 400):
    now = datetime.now(UK)
    horizon = now + timedelta(days=slack_days)
    for e in events:
        assert e.start, f"{e.title} has no start date"
        start = datetime.fromisoformat(e.start)
        end = datetime.fromisoformat(e.end) if e.end else start
        assert end >= now - timedelta(days=1), f"{e.title} is in the past"
        assert start <= horizon, f"{e.title} is implausibly far ahead: {e.start}"


class TestRoyalInstitution:
    @pytest.fixture(scope="class")
    @staticmethod
    def events(sources):
        cfg = sources["rigb"]
        return drupal_jsonapi.parse(drupal_jsonapi.fetch_raw(cfg), cfg)

    def test_returns_a_plausible_number_of_events(self, events):
        assert len(events) >= 10, "the RI publishes far more than this; schema change?"

    def test_every_event_has_the_essentials(self, events):
        for e in events:
            assert e.title
            assert e.url.startswith("https://www.rigb.org/")
            assert e.start.endswith(("+00:00", "+01:00"))

    def test_dates_are_current(self, events):
        _future_only(events)

    def test_the_age_taxonomy_is_still_being_published(self, events):
        """This is the field the whole age filter depends on."""
        tagged = [e for e in events if e.audiences]
        assert len(tagged) >= len(events) // 2
        assert any("teens" in e.audiences for e in tagged), \
            "no 'Young people 13+' events found; has the taxonomy been renamed?"

    def test_prices_parse_into_numbers(self, events):
        priced = [e for e in events if e.price_text]
        assert priced, "no prices at all; field_prices may have been renamed"
        assert any(e.price_from is not None for e in priced)


class TestCambridgeMuseums:
    @pytest.fixture(scope="class")
    @staticmethod
    def events(sources):
        cfg = sources["cam_museums"]
        return html_css.parse(html_css.fetch_raw(cfg), cfg)

    def test_selectors_still_match(self, events):
        assert len(events) >= 20, "selector drift: the listing markup has changed"

    def test_every_event_has_the_essentials(self, events):
        for e in events:
            assert e.title
            assert e.url.startswith("https://www.museums.cam.ac.uk/")
            assert e.city == "Cambridge"

    def test_dates_are_current(self, events):
        _future_only(events)


class TestBritishLibrary:
    @pytest.fixture(scope="class")
    @staticmethod
    def events(sources):
        cfg = sources["british_library"]
        return html_css.parse(html_css.fetch_raw(cfg), cfg)

    def test_selectors_still_match(self, events):
        assert len(events) >= 40, "selector drift: the listing markup has changed"

    def test_pagination_reached_the_end_of_the_listing(self, events):
        """A truncated walk publishes a silently short list."""
        assert html_css.last_fetch.get("pagination_complete") is True
        assert html_css.last_fetch.get("pages_fetched", 0) > 1

    def test_every_event_has_the_essentials(self, events):
        for e in events:
            assert e.title
            assert e.url.startswith("https://events.bl.uk/")
            assert e.city == "London"
            assert e.start.endswith(("+00:00", "+01:00"))

    def test_dates_are_current(self, events):
        _future_only(events, slack_days=800)

    def test_the_business_programme_is_still_there(self, events):
        """It is the reason this source is in the registry at all."""
        assert any("Business & entrepreneurship" in e.careers for e in events), \
            "no business events found; has the programme moved off this listing?"


class TestCuratedLinks:
    def test_every_curated_url_resolves(self, sources):
        """A curated claim whose page has gone is worse than no claim."""
        from pathlib import Path

        from collector.adapters import curated
        from collector.http import check_link

        # The registry currently holds no hand-written source -- every record
        # is collected.  The check stays so that it covers the next one added.
        hand_written = [c for c in sources.values() if c["kind"] == "curated"]
        if not hand_written:
            pytest.skip("no curated sources registered")
        cfg = dict(hand_written[0])
        root = Path(__file__).resolve().parent.parent
        events = curated.parse((root / cfg["path"]).read_text(), cfg)
        bad = []
        for e in events:
            probe = check_link(e.url)
            if probe["status"] != 200 or probe["redirected_to_root"]:
                bad.append((e.title, probe["status"], probe["redirected_to_root"]))
        assert not bad, f"curated entries with dead or redirected links: {bad}"
