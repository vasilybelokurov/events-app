"""Adapter tests against saved fixtures, plus one live smoke test.

Fixtures were cut from real responses (``tests/fixtures/``) so a change in a
venue's schema shows up here rather than as an empty page.
"""

from __future__ import annotations

import json

import pytest

from collector.adapters import curated, drupal_jsonapi, html_css, ics, jsonld
from collector.adapters import get as get_adapter


def test_registry_rejects_unknown_kinds():
    with pytest.raises(KeyError):
        get_adapter("carrier_pigeon")


# ---------------------------------------------------------------- Drupal ----

class TestDrupalJsonApi:
    def events(self, fixture_text, sources):
        return drupal_jsonapi.parse(fixture_text("rigb_events.json"), sources["rigb"])

    def test_parses_the_fixture(self, fixture_text, sources):
        evs = self.events(fixture_text, sources)
        assert len(evs) >= 3
        for e in evs:
            assert e.title and e.start and e.url.startswith("https://www.rigb.org")
            assert e.source == "rigb"

    def test_start_carries_a_london_offset(self, fixture_text, sources):
        for e in self.events(fixture_text, sources):
            assert e.start.endswith(("+01:00", "+00:00"))

    def test_age_taxonomy_becomes_audiences(self, fixture_text, sources):
        evs = self.events(fixture_text, sources)
        assert any(e.audiences for e in evs)
        for e in evs:
            assert set(e.audiences) <= {"children", "families", "teens", "adults", "schools"}

    def test_query_filters_on_start_or_end(self):
        """An exhibition that opened last month but runs into next year must
        not be filtered out by a start-date-only query."""
        url = drupal_jsonapi.build_url(
            {"endpoint": "https://x.example/jsonapi/node/event"}, since="2026-09-21")
        assert "conjunction%5D=OR" in url
        assert "field_dates.value" in url
        assert "field_dates.end_value" in url

    def test_cancelled_wording_sets_status(self, sources):
        doc = {"data": [{
            "attributes": {
                "title": "CANCELLED: An evening of physics",
                "field_dates": {"value": "2026-12-01T19:00:00+00:00"},
                "path": {"alias": "/whats-on/x"},
            },
            "relationships": {},
        }]}
        e = drupal_jsonapi.parse(json.dumps(doc), sources["rigb"])[0]
        assert e.status == "cancelled"

    def test_times_fill_a_midnight_start(self, sources):
        doc = {"data": [{
            "attributes": {
                "title": "Tour",
                "field_dates": {"value": "2026-12-01T00:00:00+00:00"},
                "field_times": "2.30pm – 3.45pm",
                "path": {"alias": "/whats-on/tour"},
            },
            "relationships": {},
        }]}
        e = drupal_jsonapi.parse(json.dumps(doc), sources["rigb"])[0]
        assert e.start == "2026-12-01T14:30:00+00:00"
        assert e.end == "2026-12-01T15:45:00+00:00"

    def test_late_end_rolls_over_midnight(self, sources):
        doc = {"data": [{
            "attributes": {
                "title": "Late show",
                "field_dates": {"value": "2026-12-01T23:00:00+00:00"},
                "field_times": "11.00pm – 1.00am",
                "path": {"alias": "/whats-on/late"},
            },
            "relationships": {},
        }]}
        e = drupal_jsonapi.parse(json.dumps(doc), sources["rigb"])[0]
        assert e.end.startswith("2026-12-02T01:00")


# --------------------------------------------------------------- html_css ---

class TestHtmlCss:
    def events(self, fixture_text, sources):
        return html_css.parse(fixture_text("cam_museums.html"), sources["cam_museums"])

    def test_parses_the_fixture(self, fixture_text, sources):
        evs = self.events(fixture_text, sources)
        assert len(evs) >= 3
        for e in evs:
            assert e.title and e.start
            assert e.url.startswith("https://www.museums.cam.ac.uk")
            assert e.city == "Cambridge"

    def test_uk_dates_are_day_first(self, fixture_text, sources):
        """17/01/2026 is January, not 1 October."""
        for e in self.events(fixture_text, sources):
            assert e.start[:2] == "20"

    def test_multi_day_runs_are_marked_ongoing(self, fixture_text, sources):
        evs = self.events(fixture_text, sources)
        assert any(e.ongoing for e in evs)

    def test_missing_selector_yields_nothing_rather_than_garbage(self, sources):
        cfg = dict(sources["cam_museums"])
        out = html_css.parse("<html><body><div class='other'>x</div></body></html>", cfg)
        assert out == []


# -------------------------------------------------------------------- ICS ---

ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:1@example.org
SUMMARY:Spring talk
DTSTART:20260328T120000
DURATION:P1D
DESCRIPTION:Suitable for ages 11-16
END:VEVENT
BEGIN:VEVENT
UID:2@example.org
SUMMARY:Gone away
STATUS:CANCELLED
DTSTART:20261201T190000
END:VEVENT
BEGIN:VEVENT
UID:3@example.org
RECURRENCE-ID:20261208T190000
SUMMARY:Weekly tour
DTSTART:20261208T190000
DTEND:20261208T200000
RRULE:FREQ=WEEKLY
END:VEVENT
BEGIN:VEVENT
UID:4@example.org
SUMMARY:All day thing
DTSTART;VALUE=DATE:20261225
END:VEVENT
BEGIN:VEVENT
UID:5@example.org
SUMMARY:Folded desc
DESCRIPTION:one line
  continued here
DTSTART:20261210T100000Z
END:VEVENT
"""


class TestIcs:
    cfg = {"key": "t", "name": "Test feed"}

    def parsed(self):
        return {e.title: e for e in ics.parse(ICS, self.cfg)}

    def test_all_vevents_parsed(self):
        assert len(self.parsed()) == 5

    def test_duration_across_the_clock_change(self):
        """DTSTART noon GMT + P1D is noon BST the next day, not 11:00."""
        e = self.parsed()["Spring talk"]
        assert e.start == "2026-03-28T12:00:00+00:00"
        assert e.end == "2026-03-29T12:00:00+01:00"

    def test_age_is_read_from_the_description(self):
        e = self.parsed()["Spring talk"]
        assert (e.age_min, e.age_max) == (11, 16)

    def test_cancelled_status(self):
        assert self.parsed()["Gone away"].status == "cancelled"

    def test_recurring_occurrence_identity_uses_recurrence_id(self):
        """Two modified instances of one series share a UID, so the id must
        include RECURRENCE-ID or they collide."""
        base = ICS.replace("RECURRENCE-ID:20261208T190000",
                           "RECURRENCE-ID:20261215T190000")
        a = {e.title: e for e in ics.parse(ICS, self.cfg)}["Weekly tour"]
        b = {e.title: e for e in ics.parse(base, self.cfg)}["Weekly tour"]
        assert a.id != b.id

    def test_recurring_event_says_so_honestly(self):
        e = self.parsed()["Weekly tour"]
        assert e.ongoing is True
        assert "series start" in e.provenance

    def test_date_only_start_is_all_day(self):
        e = self.parsed()["All day thing"]
        assert e.all_day is True
        assert e.start.startswith("2026-12-25T00:00")

    def test_utc_start_is_converted_to_london(self):
        e = self.parsed()["Folded desc"]
        assert e.start == "2026-12-10T10:00:00+00:00"

    def test_folded_lines_are_joined(self):
        assert self.parsed()["Folded desc"].summary == "one line continued here"

    def test_no_vevents_is_empty_not_an_error(self):
        assert ics.parse("BEGIN:VCALENDAR\nEND:VCALENDAR", self.cfg) == []


# ----------------------------------------------------------------- JSON-LD --

JSONLD = """<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
 {"@type":"Event","name":"Design workshop","startDate":"2026-10-28T10:00:00+00:00",
  "endDate":"2026-10-28T16:00:00+00:00","url":"/events/design",
  "description":"Stage design for ages 11-14","typicalAgeRange":"11-14",
  "eventAttendanceMode":"https://schema.org/OfflineEventAttendanceMode",
  "location":{"@type":"Place","name":"the Design Museum",
    "address":{"addressLocality":"London"},
    "geo":{"latitude":"51.4995","longitude":"-0.205"}},
  "offers":[{"@type":"Offer","price":"25","priceCurrency":"GBP","url":"https://book.example/1"}]},
 {"@type":"Event","name":"Dollar talk","startDate":"2026-11-02T19:00:00+00:00",
  "offers":{"price":"20","priceCurrency":"USD"}},
 {"@type":"Event","name":"Called off","startDate":"2026-11-03T19:00:00+00:00",
  "eventStatus":"https://schema.org/EventCancelled"},
 {"@type":"ExhibitionEvent","name":"Long run","startDate":"2026-10-15",
  "endDate":"2027-01-10"}]}
</script>
<script type="application/ld+json">{ this is not json }</script>
</head><body></body></html>"""


class TestJsonLd:
    cfg = {"key": "dm", "name": "Design Museum", "site": "https://designmuseum.org",
           "url": "https://designmuseum.org/whats-on"}

    def parsed(self):
        return {e.title: e for e in jsonld.parse(JSONLD, self.cfg)}

    def test_all_event_types_found_despite_a_broken_block(self):
        assert set(self.parsed()) == {"Design workshop", "Dollar talk", "Called off", "Long run"}

    def test_relative_url_is_resolved(self):
        assert self.parsed()["Design workshop"].url == "https://designmuseum.org/events/design"

    def test_place_and_geo(self):
        e = self.parsed()["Design workshop"]
        assert e.venue_name == "the Design Museum"
        assert e.city == "London"
        assert (round(e.lat, 4), round(e.lon, 3)) == (51.4995, -0.205)

    def test_age_range_and_booking(self):
        e = self.parsed()["Design workshop"]
        assert (e.age_min, e.age_max) == (11, 14)
        assert e.booking_url == "https://book.example/1"
        assert e.booking == "required"

    def test_sterling_price(self):
        e = self.parsed()["Design workshop"]
        assert (e.is_free, e.price_from, e.currency) == (False, 25.0, "GBP")
        assert e.price_text == "from £25"

    def test_foreign_currency_is_not_relabelled_as_pounds(self):
        e = self.parsed()["Dollar talk"]
        assert e.currency == "USD"
        assert "£" not in (e.price_text or "")

    def test_event_status_is_read(self):
        assert self.parsed()["Called off"].status == "cancelled"

    def test_multi_day_exhibition_is_ongoing_and_all_day(self):
        e = self.parsed()["Long run"]
        assert e.ongoing is True
        assert e.all_day is True

    def test_attendance_mode_drives_the_online_flag(self):
        assert self.parsed()["Design workshop"].online is False

    def test_page_without_structured_data_is_empty(self):
        assert jsonld.parse("<html><body>nothing</body></html>", self.cfg) == []


# ----------------------------------------------------------------- curated --

class TestCurated:
    def test_the_repo_file_parses(self, sources):
        cfg = dict(sources["curated_teen_careers"])
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        raw = (root / cfg["path"]).read_text(encoding="utf-8")
        evs = curated.parse(raw, cfg)
        assert len(evs) >= 5
        for e in evs:
            assert e.title and e.url.startswith("https://")
            assert e.provenance, f"{e.title} has no provenance"
            assert e.start

    def test_explicit_bounds_override_the_text_parser(self):
        raw = """
        events:
          - id: x
            title: Court visit
            url: https://example.org
            start: 2026-10-01
            age_text: under-14s are not admitted
            age_min: 14
        """
        e = curated.parse(raw, {"key": "c", "name": "C"})[0]
        assert (e.age_min, e.age_max) == (14, None)
