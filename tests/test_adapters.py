"""Adapter tests against saved fixtures, plus one live smoke test.

Fixtures were cut from real responses (``tests/fixtures/``) so a change in a
venue's schema shows up here rather than as an empty page.
"""

from __future__ import annotations

import json

import pytest

from collector.adapters import (curated, drupal_jsonapi, html_css, ics,
                                jsonld, venue)
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

    def test_a_midnight_end_does_not_beat_the_printed_time_range(self, sources):
        """A date-only end field resolves to midnight on the day.

        For a 3pm event that end precedes its own start, and it was published
        as a backwards range.  The listing also prints "3:00 PM - 3:30 PM",
        which is the better answer than dropping the end entirely.
        """
        cfg = dict(sources["cam_museums"])
        html = """<div class='views-row'>
          <div class='views-field-title'><a href='/events/x'>A tour</a></div>
          <div class='views-field-field-date'><div class='field-content'>07/10/2026</div></div>
          <div class='views-field-field-event-time'><div class='field-content'>3:00 PM - 3:30 PM</div></div>
          <div class='views-field-field-end-date'><time datetime='2026-10-07T00:00:00'>7 Oct</time></div>
        </div>"""
        cfg["detail"] = {"enabled": False}
        e = html_css.parse(html, cfg)[0]
        assert e.start.startswith("2026-10-07T15:00")
        assert e.end.startswith("2026-10-07T15:30")
        assert e.provenance is None

    def test_missing_selector_yields_nothing_rather_than_garbage(self, sources):
        cfg = dict(sources["cam_museums"])
        out = html_css.parse("<html><body><div class='other'>x</div></body></html>", cfg)
        assert out == []


class TestPaginatedHtmlCss:
    """The British Library: two fixture pages, sharing two pinned rows."""

    def raw(self, fixture_text):
        return html_css.PAGE_BREAK.join([
            fixture_text("british_library_p1.html"),
            fixture_text("british_library_p2.html"),
        ])

    def events(self, fixture_text, sources):
        return html_css.parse(self.raw(fixture_text), sources["british_library"])

    def test_parses_both_pages(self, fixture_text, sources):
        evs = self.events(fixture_text, sources)
        titles = {e.title for e in evs}
        assert "Agatha Christie: A World of Mystery" in titles
        assert any("social media" in t for t in titles)

    def test_a_row_pinned_to_every_page_is_published_once(self, fixture_text, sources):
        evs = self.events(fixture_text, sources)
        pinned = [e for e in evs if e.title.startswith("Agatha Christie")]
        assert len(pinned) == 1
        assert len({e.id for e in evs}) == len(evs)

    def test_machine_markup_is_preferred_to_prose(self, fixture_text, sources):
        """The row carries both; the ISO attribute is the one to believe."""
        evs = self.events(fixture_text, sources)
        conquest = next(e for e in evs if e.title.startswith("Conquest"))
        assert conquest.start.startswith("2027-10-01")
        assert conquest.end.startswith("2028-02-27")
        assert conquest.ongoing

    def test_prose_dates_still_parse_when_there_is_no_markup(self, fixture_text, sources):
        evs = self.events(fixture_text, sources)
        agatha = next(e for e in evs if e.title.startswith("Agatha Christie"))
        assert agatha.start.startswith("2026-10-30")
        assert agatha.end.startswith("2027-06-20")

    def test_an_undated_row_is_dropped_not_invented(self, fixture_text, sources):
        """"Daily" and "Available Fridays and Sundays" are not dates."""
        titles = {e.title for e in self.events(fixture_text, sources)}
        assert "Treasures Tour" not in titles
        assert "Building Tour" not in titles

    def test_free_is_only_claimed_when_the_row_says_so(self, fixture_text, sources):
        evs = self.events(fixture_text, sources)
        free = next(e for e in evs if "social media" in e.title)
        assert free.is_free is True
        assert all(e.is_free is not True for e in evs if e.title.startswith("Conquest"))

    def test_links_are_absolute(self, fixture_text, sources):
        for e in self.events(fixture_text, sources):
            assert e.url.startswith("https://events.bl.uk")

    def _yearless_row(self, when):
        return ('<ol><li class="o-grid__item">'
                '<a class="c-media c-media--event" href="https://events.bl.uk/e/x">'
                '<h3 class="c-media__title">A talk</h3>'
                f'<time class="c-media__datetime">{when}</time>'
                '</a></li></ol>')

    def test_a_yearless_date_is_rolled_forward_only_where_declared(self, sources):
        """The flag is the whole difference between a listing and an archive."""
        from datetime import date, timedelta
        long_past = date.today() - timedelta(days=182)
        html = self._yearless_row(long_past.strftime("%A %-d %B 11.00"))

        cfg = dict(sources["british_library"])
        assert cfg["assume_future_dates"] is True
        assert html_css.parse(html, cfg)[0].start[:4] == str(long_past.year + 1)

        archive = dict(cfg)
        del archive["assume_future_dates"]
        assert html_css.parse(html, archive)[0].start[:4] == str(long_past.year)

    def test_a_recurrence_row_is_dropped(self, sources):
        """"Most Fridays at 11.30" is not a date, so there is no event."""
        cfg = dict(sources["british_library"])
        assert html_css.parse(self._yearless_row("Most Fridays at 11.30"), cfg) == []


class TestPagination:
    """fetch_raw's walk, driven by fakes rather than the live site."""

    def _cfg(self):
        return {"key": "k", "name": "n", "url": "https://x.test/list",
                "pages": {"param": "page", "max_pages": 4},
                "selectors": {"item": "li", "title": "a", "link": "a"}}

    def _page(self, *slugs):
        rows = "".join(f'<li><a href="/e/{s}">{s}</a></li>' for s in slugs)
        return f"<html><body><ul>{rows}</ul></body></html>"

    def test_stops_when_a_page_adds_nothing_new(self, monkeypatch):
        pages = {1: self._page("a", "b"), 2: self._page("a", "c"), 3: self._page("a")}
        calls = []

        def fake_fetch(url, **kw):
            n = int(url.split("page=")[1]) if "page=" in url else 1
            calls.append(n)
            return pages.get(n, self._page("a"))

        monkeypatch.setattr(html_css, "fetch", fake_fetch)
        raw = html_css.fetch_raw(self._cfg())
        assert calls == [1, 2, 3]
        assert raw.count(html_css.PAGE_BREAK) == 1      # pages 1 and 2 kept
        assert html_css.last_fetch["pagination_complete"] is True

    def test_a_truncated_walk_is_reported_not_hidden(self, monkeypatch):
        calls = []

        def fake_fetch(url, **kw):
            n = int(url.split("page=")[1]) if "page=" in url else 1
            calls.append(n)
            return self._page(f"row{n}")               # every page is new

        monkeypatch.setattr(html_css, "fetch", fake_fetch)
        html_css.fetch_raw(self._cfg())
        assert html_css.last_fetch["pagination_complete"] is False
        assert html_css.last_fetch["pages_fetched"] == 4
        # No probe beyond the budget: its own failure would have taken the
        # whole source down instead of reporting a partial walk.
        assert calls == [1, 2, 3, 4]

    def test_a_blank_page_does_not_end_the_walk(self, monkeypatch):
        """An interstitial served as HTTP 200 looks just like the last page.

        Treating it as the end published a truncated list and called the
        source healthy -- the one failure this project exists to avoid.
        """
        pages = {1: self._page("a", "b"), 2: "<html><body>Unavailable</body></html>",
                 3: self._page("c", "d"), 4: self._page("e")}
        calls = []

        def fake_fetch(url, **kw):
            n = int(url.split("page=")[1]) if "page=" in url else 1
            calls.append(n)
            return pages[n]

        monkeypatch.setattr(html_css, "fetch", fake_fetch)
        raw = html_css.fetch_raw(self._cfg())
        assert calls == [1, 2, 3, 4]                   # walked past the blank
        assert "/e/c" in raw and "/e/e" in raw
        assert html_css.last_fetch["pagination_complete"] is False

    def test_two_blank_pages_stop_the_walk(self, monkeypatch):
        calls = []

        def fake_fetch(url, **kw):
            n = int(url.split("page=")[1]) if "page=" in url else 1
            calls.append(n)
            return self._page("a") if n == 1 else "<html><body>nothing</body></html>"

        monkeypatch.setattr(html_css, "fetch", fake_fetch)
        html_css.fetch_raw(self._cfg())
        assert calls == [1, 2, 3]
        assert html_css.last_fetch["pagination_complete"] is False

    def test_the_sentinel_cannot_arrive_from_a_page(self, monkeypatch):
        """A page containing the page-break comment split into two chunks."""
        poisoned = (self._page("a").replace("</ul>", "")
                    + html_css.PAGE_BREAK
                    + '<li><a href="/e/z">z</a></li></ul></body></html>')
        monkeypatch.setattr(html_css, "fetch", lambda url, **kw: poisoned)
        raw = html_css.fetch_raw(self._cfg())
        assert html_css.PAGE_BREAK not in raw
        assert "/e/z" in raw

    def test_a_site_that_ignores_the_parameter_is_not_crawled_forever(self, monkeypatch):
        """Rows that an earlier page already had *is* a genuine end."""
        monkeypatch.setattr(html_css, "fetch", lambda url, **kw: self._page("a", "b"))
        html_css.fetch_raw(self._cfg())
        assert html_css.last_fetch["pages_fetched"] == 1
        assert html_css.last_fetch["pagination_complete"] is True

    def test_an_unpaginated_source_makes_one_request(self, monkeypatch):
        calls = []
        monkeypatch.setattr(html_css, "fetch",
                            lambda url, **kw: calls.append(url) or self._page("a"))
        cfg = self._cfg()
        del cfg["pages"]
        html_css.fetch_raw(cfg)
        assert calls == ["https://x.test/list"]

    def test_the_page_parameter_replaces_an_existing_one(self):
        assert html_css._with_page("https://x.test/l?page=9&q=1", "page", 3) \
            in ("https://x.test/l?q=1&page=3", "https://x.test/l?page=3&q=1")


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
    """The `curated` adapter is retained for a hand-written entry, but no
    hand-typed event claims ship: the registry collects from venues instead
    (see TestVenue and collector/adapters/venue.py)."""

    CFG = {"key": "curated_x", "name": "Curated"}

    def test_parses_a_hand_written_entry(self):
        raw = """
        events:
          - id: x
            title: Court visit
            url: https://example.org/court
            start: 2027-10-01
            time_text: "2.30pm - 3.45pm"
            age_text: under-14s are not admitted
            verified_on: 2026-09-21
            provenance: checked against the venue page
        """
        e = curated.parse(raw, self.CFG)[0]
        assert e.title == "Court visit"
        assert e.start == "2027-10-01T14:30:00+01:00"
        assert e.verified_on == "2026-09-21"
        assert e.provenance

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
        e = curated.parse(raw, self.CFG)[0]
        assert (e.age_min, e.age_max) == (14, None)

    def test_an_unverified_entry_says_so(self):
        raw = """
        events:
          - id: x
            title: Something
            url: https://example.org
            start: 2027-01-01
        """
        assert curated.parse(raw, self.CFG)[0].verified_on is None

    def test_no_hand_typed_event_claims_are_shipped(self, sources):
        """The point of the venue adapter: claims come from venue pages."""
        assert not any(cfg["kind"] == "curated" for cfg in sources.values()), \
            "a curated source is back in the registry; every claim it makes " \
            "needs a human to re-check it"


# ------------------------------------------------------- detail enrichment --

DETAIL_PAGE = """<html><body>
  <div class="field field--name-field-for-whom field--label-above">
    <div class="field__label">Who</div>
    <div class="field__items">
      <div class="field__item">12+</div>, <div class="field__item">Adults (18+)</div>,
      <div class="field__item">All ages</div>, <div class="field__item">Families</div>
    </div>
  </div>
  <div class="field field--name-field-price-in-sidebar-">
    <div class="field__label">Price</div>Free
  </div>
  <div class="field field--name-field-event-type">
    <div class="field__label">What</div>
    <div class="field__item">Talks and lectures</div>
  </div>
  <div class="field field--name-body"><p>A talk about engineering careers.</p></div>
</body></html>"""

ADULTS_ONLY_PAGE = DETAIL_PAGE.replace(
    '<div class="field__item">12+</div>, <div class="field__item">Adults (18+)</div>,\n'
    '      <div class="field__item">All ages</div>, <div class="field__item">Families</div>',
    '<div class="field__item">Adults (18+)</div>')

LISTING = """<html><body>
  <div class="views-row">
    <div class="views-field views-field-title">
      <a href="/events/one">An event</a></div>
    <div class="views-field views-field-field-date">
      <div class="field-content">17/01/2027</div></div>
  </div>
</body></html>"""


class TestDetailEnrichment:
    """The Cambridge listing page carries no ages at all, which left the age
    filter blind over the largest source.  The detail page has them."""

    cfg = {
        "key": "cam", "name": "Cam", "site": "https://example.org",
        "url": "https://example.org/whats-on", "city": "Cambridge",
        "selectors": {"item": ".views-row", "title": ".views-field-title a",
                      "link": ".views-field-title a",
                      "date": ".views-field-field-date .field-content"},
        "detail": {"enabled": True, "selectors": {
            "age": ".field--name-field-for-whom",
            "price": ".field--name-field-price-in-sidebar-",
            "topics": ".field--name-field-event-type",
            "summary": ".field--name-body"}},
    }

    def parsed(self, monkeypatch, page=DETAIL_PAGE):
        monkeypatch.setattr(html_css, "fetch", lambda url, **kw: page)
        return html_css.parse(LISTING, self.cfg)

    def test_ages_come_from_the_detail_page(self, monkeypatch):
        e = self.parsed(monkeypatch)[0]
        assert e.age_text == "12+, Adults (18+), All ages, Families"
        assert html_css.last_enrichment["detail_ages_added"] == 1

    def test_alternative_audiences_are_unioned_not_intersected(self, monkeypatch):
        """"12+, Adults (18+), All ages" must not exclude a 14-year-old by
        taking the 18+ bound from one of the alternatives."""
        e = self.parsed(monkeypatch)[0]
        assert (e.age_min, e.age_max) == (None, None)
        assert e.suits_age(14) is True
        assert e.eligibility(14) == "eligible"

    def test_an_adults_only_page_does_exclude_a_teenager(self, monkeypatch):
        e = self.parsed(monkeypatch, ADULTS_ONLY_PAGE)[0]
        assert e.age_min == 18
        assert e.eligibility(14) == "excluded"

    def test_separator_commas_do_not_become_values(self, monkeypatch):
        e = self.parsed(monkeypatch)[0]
        assert ",," not in (e.age_text or "")
        assert all(part.strip() for part in e.age_text.split(","))

    def test_labels_are_stripped_from_values(self, monkeypatch):
        e = self.parsed(monkeypatch)[0]
        assert not e.age_text.startswith("Who")
        assert e.price_text == "Free"
        assert e.is_free is True

    def test_careers_are_rederived_from_enriched_text(self, monkeypatch):
        """The detail page supplies words the listing row omitted."""
        e = self.parsed(monkeypatch)[0]
        assert "Engineering" in e.careers

    def test_a_failing_detail_page_does_not_lose_the_event(self, monkeypatch):
        def boom(url, **kw):
            raise RuntimeError("404")
        monkeypatch.setattr(html_css, "fetch", boom)
        events = html_css.parse(LISTING, self.cfg)
        assert len(events) == 1, "the listing event must survive"
        assert html_css.last_enrichment["detail_failed"] == 1

    def test_enrichment_is_opt_in(self, monkeypatch):
        monkeypatch.setattr(html_css, "fetch",
                            lambda url, **kw: pytest.fail("should not fetch"))
        cfg = {**self.cfg, "detail": {"enabled": False}}
        events = html_css.parse(LISTING, cfg)
        assert len(events) == 1
        assert html_css.last_enrichment == {"detail_enabled": False}

    def test_max_pages_caps_the_fetching(self, monkeypatch):
        calls = []
        monkeypatch.setattr(html_css, "fetch",
                            lambda url, **kw: calls.append(url) or DETAIL_PAGE)
        cfg = {**self.cfg, "detail": {**self.cfg["detail"], "max_pages": 0}}
        html_css.parse(LISTING, cfg)
        assert calls == []


# ------------------------------------------------------------------ venue ---

VENUE_PAGE = """<html><head>
  <title>Central Criminal Court | City of London</title>
  <meta property="og:title" content="Central Criminal Court">
  <meta property="og:description" content="The most famous criminal court in the world.">
</head><body>
  <h1>Central Criminal Court</h1>
  <p>Visitors may watch proceedings from the public galleries.
     There is no admission for children under 14, and security officers may
     request proof of age. Access is subject to official photographic
     identification.</p>
  <script>var tracking = "no admission for children under 99";</script>
</body></html>"""

REPURPOSED_PAGE = """<html><head><title>Cost of living support</title></head>
  <body><h1>Cost of living support</h1><p>Nothing to do with courts.</p></body></html>"""


class TestVenue:
    """A venue record is built from the page as fetched, and may only claim
    what that page still says."""

    cfg = {
        "key": "old_bailey", "name": "Old Bailey public galleries",
        "homepage": "https://example.org/court",
        "title": "Old Bailey public galleries (Central Criminal Court)",
        "venue_name": "Central Criminal Court", "city": "London",
        "schedule": "Weekdays when the court is sitting",
        "topics": ["Law"],
        "expect": ["public galler"],
        "confirm": [
            {"phrase": "no admission for children under 14",
             "sets": {"age_min": 14, "age_text": "no admission for children under 14"}},
            {"phrase": "photographic identification", "sets": {"booking": "required"}},
        ],
    }

    def one(self, page=VENUE_PAGE, cfg=None):
        return venue.parse(page, cfg or self.cfg)[0]

    def test_one_standing_record_per_venue(self):
        assert len(venue.parse(VENUE_PAGE, self.cfg)) == 1

    def test_it_is_a_standing_offer_not_a_dated_event(self):
        e = self.one()
        assert e.anytime is True and e.all_day is True
        assert e.when_text == "Weekdays when the court is sitting"

    def test_the_registry_names_the_venue_not_the_page(self):
        """An h1 of "Tours" or "What's on" identifies nothing, and two such
        pages were previously de-duplicated into each other."""
        e = self.one()
        assert e.title == "Old Bailey public galleries (Central Criminal Court)"

    def test_the_page_title_is_still_reported(self):
        assert "Central Criminal Court" in self.one().provenance

    def test_a_confirmed_phrase_applies_its_claim(self):
        e = self.one()
        assert e.age_min == 14
        assert e.booking == "required"
        assert e.eligibility(14) == "eligible"
        assert e.eligibility(13) == "excluded"

    def test_confirmed_phrases_are_named_in_the_provenance(self):
        p = self.one().provenance
        assert "Confirmed on the page" in p
        assert "no admission for children under 14" in p

    def test_an_unconfirmed_claim_is_dropped_and_recorded(self):
        """The whole point: a claim cannot outlive the sentence it came from."""
        cfg = {**self.cfg, "confirm": [
            {"phrase": "under-16s go free", "sets": {"price_text": "Free"}}]}
        e = self.one(cfg=cfg)
        assert e.price_text is None
        assert e.is_free is None
        assert "NOT found on the page" in e.provenance
        assert "under-16s go free" in e.provenance

    def test_script_text_cannot_confirm_a_claim(self):
        """Phrases hidden in JavaScript are not published facts."""
        cfg = {**self.cfg, "confirm": [
            {"phrase": "no admission for children under 99",
             "sets": {"age_min": 99}}]}
        assert self.one(cfg=cfg).age_min is None

    def test_a_fetched_page_counts_as_verified_today(self):
        from datetime import datetime
        from collector.models import UK
        assert self.one().verified_on == datetime.now(UK).date().isoformat()

    def test_a_repurposed_page_is_not_verified(self):
        e = self.one(REPURPOSED_PAGE)
        assert e.verified_on is None
        assert "may have been repurposed" in e.provenance
        assert e.age_min is None, "claims must not survive a repurposed page"

    def test_an_unfetchable_page_yields_an_honest_record(self):
        e = self.one("<!-- unfetchable: HTTPError: 403 -->")
        assert e.verified_on is None
        assert "refused automated access" in e.provenance
        assert e.age_min is None
        assert e.title.startswith("Old Bailey")

    def test_fetch_raw_reraises_unless_blocking_is_allowed(self, monkeypatch):
        def boom(url, **kw):
            raise RuntimeError("403")
        monkeypatch.setattr(venue, "fetch", boom)
        with pytest.raises(RuntimeError):
            venue.fetch_raw(self.cfg)
        raw = venue.fetch_raw({**self.cfg, "allow_blocked": True})
        assert "unfetchable" in raw

    def test_a_confirm_rule_cannot_set_an_arbitrary_field(self):
        cfg = {**self.cfg, "confirm": [
            {"phrase": "public galler", "sets": {"start": "2027-01-01"}}]}
        with pytest.raises(venue.VenueConfigError):
            venue.parse(VENUE_PAGE, cfg)

    def test_careers_are_inferred_from_the_page(self):
        assert "Law & justice" in self.one().careers

    def test_the_id_is_stable_across_runs(self):
        assert venue.parse(VENUE_PAGE, self.cfg)[0].id == \
            venue.parse(REPURPOSED_PAGE, self.cfg)[0].id


class TestNoHandTypedClaims:
    def test_every_registry_source_collects_from_a_url(self, sources):
        """The requirement: the app goes through venues and collects, rather
        than shipping typed-in facts."""
        for key, cfg in sources.items():
            assert cfg.get("homepage", "").startswith("http"), key
            assert cfg["kind"] != "curated", \
                f"{key} ships hand-typed claims instead of collecting them"


class TestAudienceUnionInDrupal:
    """The RI age taxonomy lists the audiences an event suits, so the labels
    combine as a union.  Concatenating them and parsing the result gave
    "Children 12 and under, Families, Young people 13+" a minimum age of 13 --
    the opposite of what the venue means."""

    def node(self, ages):
        return {
            "data": [{
                "attributes": {
                    "title": "A show",
                    "field_dates": {"value": "2026-12-01T19:00:00+00:00"},
                    "path": {"alias": "/whats-on/show"},
                },
                "relationships": {
                    "field_age": {"data": [{"id": f"a{i}"} for i in range(len(ages))]},
                },
            }],
            "included": [{"id": f"a{i}", "type": "taxonomy_term--age",
                          "attributes": {"name": name}}
                         for i, name in enumerate(ages)],
        }

    def parse(self, ages, sources):
        return drupal_jsonapi.parse(json.dumps(self.node(ages)), sources["rigb"])[0]

    def test_children_and_teens_together_are_not_a_minimum_of_13(self, sources):
        e = self.parse(["Children 12 and under", "Families", "Young people 13+"], sources)
        assert (e.age_min, e.age_max) == (None, None)
        assert e.suits_age(9) and e.suits_age(14)

    def test_a_single_teen_label_still_sets_a_minimum(self, sources):
        e = self.parse(["Young people 13+"], sources)
        assert e.age_min == 13
        assert e.eligibility(12) == "excluded"

    def test_the_audience_list_is_still_populated(self, sources):
        e = self.parse(["Children 12 and under", "Families"], sources)
        assert set(e.audiences) == {"children", "families"}

    def test_the_raw_labels_are_kept_for_the_reader(self, sources):
        e = self.parse(["Adults", "Young people 13+"], sources)
        assert e.age_text == "Adults, Young people 13+"


class TestPostponedIsNotCancelled:
    def test_postponed_has_its_own_status(self, sources):
        doc = {"data": [{
            "attributes": {"title": "POSTPONED: A talk",
                           "field_dates": {"value": "2026-12-01T19:00:00+00:00"},
                           "path": {"alias": "/x"}},
            "relationships": {}}]}
        e = drupal_jsonapi.parse(json.dumps(doc), sources["rigb"])[0]
        assert e.status == "postponed", "postponed is still going to happen"

    def test_cancelled_is_unchanged(self, sources):
        doc = {"data": [{
            "attributes": {"title": "CANCELLED: A talk",
                           "field_dates": {"value": "2026-12-01T19:00:00+00:00"},
                           "path": {"alias": "/x"}},
            "relationships": {}}]}
        assert drupal_jsonapi.parse(json.dumps(doc), sources["rigb"])[0].status == "cancelled"


class TestEventIdentity:
    """Every record must have its own id: the browser's saved and dismissed
    lists are keyed on it.  A label-harvesting loop once shadowed the identity
    variable, giving all 17 events from one source the same id."""

    ISLAND = """<html><body><script type="application/json" id="__NEXT_DATA__">
    {"props": {"pageProps": {"events": [
      {"type": "Event", "id": "aaa", "title": "One",
       "format": {"label": "Workshop"},
       "times": [{"startDateTime": "2027-01-01T10:00:00.000Z"}]},
      {"type": "Event", "id": "bbb", "title": "Two",
       "format": {"label": "Discussion"},
       "times": [{"startDateTime": "2027-01-02T10:00:00.000Z"}]},
      {"type": "Event", "id": "ccc", "title": "Three",
       "audiences": [{"label": "14+"}],
       "times": [{"startDateTime": "2027-01-03T10:00:00.000Z"}]}]}}}
    </script></body></html>"""

    cfg = {"key": "isl", "name": "Island", "site": "https://example.org",
           "url": "https://example.org/events",
           "url_template": "https://example.org/events/{id}",
           "require_url": True}

    def parsed(self):
        return jsonld.parse(self.ISLAND, self.cfg)

    def test_events_are_found_inside_a_json_island(self):
        """They sit at props.pageProps.events, which a key-directed walk never
        reaches."""
        assert len(self.parsed()) == 3

    def test_each_event_gets_its_own_id(self):
        ids = [e.id for e in self.parsed()]
        assert len(set(ids)) == 3, "ids collided"

    def test_urls_are_built_from_the_template(self):
        assert {e.url for e in self.parsed()} == {
            "https://example.org/events/aaa",
            "https://example.org/events/bbb",
            "https://example.org/events/ccc"}

    def test_times_supply_the_start_when_there_is_no_startdate(self):
        e = next(x for x in self.parsed() if x.title == "One")
        assert e.start.startswith("2027-01-01T10:00")

    def test_labels_become_topics(self):
        e = next(x for x in self.parsed() if x.title == "One")
        assert "Workshop" in e.topics

    def test_audience_labels_set_the_age(self):
        e = next(x for x in self.parsed() if x.title == "Three")
        assert e.age_min == 14
        assert e.eligibility(14) == "eligible"
        assert e.eligibility(12) == "excluded"

    def test_require_url_drops_objects_with_no_identifier(self):
        raw = self.ISLAND.replace('"id": "aaa", ', '')
        assert len(jsonld.parse(raw, self.cfg)) == 2

    def test_a_plain_json_document_is_read_directly(self):
        raw = '{"results": [{"type": "Event", "id": "z", "title": "Solo", ' \
              '"times": [{"startDateTime": "2027-02-01T10:00:00.000Z"}]}]}'
        events = jsonld.parse(raw, self.cfg)
        assert len(events) == 1 and events[0].title == "Solo"


class TestPhraseNegation:
    """Phrase presence is not proof a claim still holds."""

    CFG = {"key": "v", "name": "Venue", "kind": "venue",
           "homepage": "https://x.test/", "title": "The place",
           "confirm": [{"phrase": "no admission for children under 14",
                        "sets": {"age_min": 14, "age_text": "no under 14s"}}]}

    def _age(self, body, cfg=None):
        from collector.adapters import venue
        return venue.parse(f"<html><body>{body}</body></html>", cfg or self.CFG)[0].age_min

    def test_a_plain_statement_is_confirmed(self):
        assert self._age("<p>There is no admission for children under 14.</p>") == 14

    @pytest.mark.parametrize("body", [
        "<p>The rule that there is no admission for children under 14 "
        "was dropped in 2025 and no longer applies.</p>",
        "<p>Until further notice there is no admission for children under 14 "
        "— this has been suspended.</p>",
        "<p>We used to say there is no admission for children under 14.</p>",
    ])
    def test_a_retired_rule_is_not_claimed(self, body):
        """"Free entry is no longer available" contains "Free entry"."""
        assert self._age(body) is None

    def test_an_unless_phrase_vetoes_the_claim(self):
        cfg = dict(self.CFG)
        cfg["confirm"] = [{**self.CFG["confirm"][0], "unless": ["all ages welcome"]}]
        body = "<p>There is no admission for children under 14. All ages welcome.</p>"
        assert self._age(body, cfg) is None

    def test_one_clean_mention_is_enough(self):
        """A negator elsewhere on a long page must not veto a clear statement."""
        body = ("<p>There is no admission for children under 14.</p>"
                + "<p>filler.</p>" * 40
                + "<p>The cafe is no longer open on Mondays.</p>")
        assert self._age(body) == 14


class TestSourceTopicsDoNotReachTheClassifier:
    """The same leak, closed in every adapter that could have it."""

    DOC = {"results": [{"@type": "Event", "name": "Woodworking Saturdays",
                        "startDate": "2026-10-03T10:00:00+01:00",
                        "url": "https://x.test/w",
                        "description": "A hands-on woodworking class."}]}

    def _parse(self, cfg):
        return jsonld.parse(json.dumps(self.DOC), cfg)[0]

    def test_jsonld_keeps_source_topics_off_the_career_tags(self):
        e = self._parse({"key": "k", "name": "n", "kind": "jsonld",
                         "url": "https://x.test/", "site": "https://x.test",
                         "topics": ["Design", "Digital media"]})
        assert e.careers == [], f"the venue's programme leaked in: {e.careers}"
        assert e.topics == ["Design", "Digital media"], "still shown on the page"

    def test_career_topics_are_honoured_when_declared(self):
        e = self._parse({"key": "k", "name": "n", "kind": "jsonld",
                         "url": "https://x.test/", "site": "https://x.test",
                         "topics": ["Design", "Digital media"],
                         "career_topics": ["Craft"]})
        assert "Design, making & architecture" in e.careers

    def test_an_events_own_labels_still_count(self):
        """`format`/`series` are the event's own, not the venue's programme."""
        doc = {"results": [{**self.DOC["results"][0],
                            "series": [{"label": "Chemistry"}]}]}
        cfg = {"key": "k", "name": "n", "kind": "jsonld",
               "url": "https://x.test/", "site": "https://x.test"}
        e = jsonld.parse(json.dumps(doc), cfg)[0]
        assert "Chemistry & materials" in e.careers
