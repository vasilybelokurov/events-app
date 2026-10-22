"""The Exhibitions page: which records belong on it, and the galleries that feed it.

Adapter tests read fixtures cut from the real listings on 2026-10-22, and the
expected values below were read off those files, not typed from memory.  The
build tests use the offline stub adapter from ``test_build``; their events are
synthetic and exist only to drive the marking and carry-over code paths.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
import requests
import yaml

from collector import build as build_mod
from collector.adapters import drupal_jsonapi, html_css
from collector.models import UK, Event, parse_date_range

from test_build import STUB_KIND, ev, future, stub  # noqa: F401  (fixture)


def marked(events, cfg):
    return {e.title for e in events if build_mod.is_exhibition(e, cfg)}


# --------------------------------------------------------------------------
# The shared parser
# --------------------------------------------------------------------------

class TestRunsThatCrossNewYear:
    """Whitechapel writes the year once, on the closing date."""

    def test_the_opening_is_in_the_year_before(self):
        # Verbatim from whitechapelgallery.org/exhibitions/, 2026-10-22.
        assert parse_date_range("7 Oct - 14 Feb 2027") == \
            ("2026-10-07T00:00:00+01:00", "2027-02-14T00:00:00+00:00")

    def test_a_run_within_one_year_is_unchanged(self):
        assert parse_date_range("8 Oct - 8 Nov 2026") == \
            ("2026-10-08T00:00:00+01:00", "2026-11-08T00:00:00+00:00")

    def test_a_bare_day_borrows_the_month(self):
        """Verbatim from ica.art/exhibitions.  Before, this was read as
        30 October 2012 at 20:26."""
        assert parse_date_range("12 \u2013 30 October 2026") == \
            ("2026-10-12T00:00:00+01:00", "2026-10-30T00:00:00+00:00")

    def test_a_weekday_does_not_borrow_the_month(self):
        """Verbatim from events.bl.uk; the first fix invented a Monday in
        June for it.  The listing's own machine date is used for this row."""
        assert parse_date_range(
            "Monday \u2013 Thursday, 2 November 2026 \u2013 18 June 2027, London") \
            == (None, None)

    def test_a_time_range_is_still_not_a_run(self):
        """The regression the shared-parser rule in CLAUDE.md was written for."""
        assert parse_date_range("1 October 2026 1.05pm - 2pm") == \
            ("2026-10-01T13:05:00+01:00", None)


# --------------------------------------------------------------------------
# What counts as an exhibition
# --------------------------------------------------------------------------

class TestIsExhibition:
    def test_needs_the_sources_own_label(self):
        e = Event("i", "t", "u", "s", "S", ongoing=True, topics=["Gallery tour"])
        assert not build_mod.is_exhibition(e, {"exhibition_topics": ["Exhibition"]})

    def test_needs_a_run(self):
        """cam_museums files one-hour tours under its exhibitions topic."""
        e = Event("i", "t", "u", "s", "S", ongoing=False,
                  topics=["Exhibitions and displays"])
        assert not build_mod.is_exhibition(
            e, {"exhibition_topics": ["Exhibitions and displays"]})

    def test_a_standing_offer_is_not_one(self):
        e = Event("i", "t", "u", "s", "S", ongoing=True, anytime=True,
                  topics=["Exhibition"])
        assert not build_mod.is_exhibition(e, {"exhibition_topics": ["Exhibition"]})

    def test_a_source_without_the_key_has_none(self):
        e = Event("i", "t", "u", "s", "S", ongoing=True, topics=["Exhibition"])
        assert not build_mod.is_exhibition(e, {})


# --------------------------------------------------------------------------
# The galleries
# --------------------------------------------------------------------------

class TestPhotographersGallery:
    @pytest.fixture
    def events(self, fixture_text, sources):
        return drupal_jsonapi.parse(fixture_text("photographers_gallery.json"),
                                    sources["photographers_gallery"])

    def test_exhibitions_are_the_ones_typed_exhibition(self, events, sources):
        assert marked(events, sources["photographers_gallery"]) == {
            "Ghost in the loop", "Roots", "Sanlé Sory: Colour Years",
            "Snap Happy! How Children Became Photographers",
            "William Eggleston: All that goes unnoticed", "Sirkka-Liisa Konttinen",
        }

    def test_a_long_course_stays_on_the_events_page(self, events, sources):
        course = next(e for e in events if e.title == "Course | Collecting Photography 2026")
        assert course.ongoing
        assert not build_mod.is_exhibition(course, sources["photographers_gallery"])

    def test_its_own_field_names_are_read(self, events):
        tour = next(e for e in events if e.title == "Exhibition Tour | Sanlé Sory: Colour Years"
                    and e.start.startswith("2026-10-31"))
        assert tour.price_text == "£10/£7.50 concessions (members go free)"
        assert tour.booking_url and "ticketsolve" in tour.booking_url


class TestTate:
    @pytest.fixture
    def events(self, fixture_text, sources):
        raw = html_css.PAGE_BREAK.join(
            [fixture_text("tate_p1.html"), fixture_text("tate_p2.html")])
        return html_css.parse(raw, sources["tate"])

    def test_only_the_london_tates(self, events):
        assert events
        assert {e.venue_name for e in events} == {"Tate Modern", "Tate Britain"}

    def test_featured_cards_collapse_by_url(self, events):
        assert len({e.url for e in events}) == len(events)
        assert sum(e.title == "Frida: The Making of an Icon" for e in events) == 1

    def test_an_open_run_keeps_the_venues_wording(self, events):
        frida = next(e for e in events if e.title == "Frida: The Making of an Icon")
        assert frida.when_text == "Until 3 Jan 2027"
        assert frida.end.startswith("2027-01-03")

    def test_a_dated_run(self, events):
        monet = next(e for e in events if e.title == "Monet: Painting Time")
        assert (monet.start[:10], monet.end[:10]) == ("2027-02-25", "2027-06-27")
        assert monet.when_text is None

    def test_free_for_members_is_not_free(self, events):
        frida = next(e for e in events if e.title == "Frida: The Making of an Icon")
        assert frida.is_free is not True


def test_whitechapel_runs_across_new_year(fixture_text, sources):
    evs = html_css.parse(fixture_text("whitechapel.html"), sources["whitechapel"])
    living = next(e for e in evs if e.title == "Cecilia Vicuña: Living Threads")
    assert (living.start[:10], living.end[:10]) == ("2026-10-07", "2027-02-14")
    assert marked(evs, sources["whitechapel"]) == {e.title for e in evs}


def test_ica_keeps_its_one_night_events_off_the_page(fixture_text, sources):
    evs = html_css.parse(fixture_text("ica.html"), sources["ica"])
    assert marked(evs, sources["ica"]) == {
        "Autumn Knight: I Can't Complain",
        "Frieze × ICA Artists’ Film Programme 2026 (online)",
    }
    assert any(e.title == "party done: Performance by Samra Mayanja" for e in evs)


def test_british_museum_reads_only_free_as_a_price(fixture_text, sources):
    evs = html_css.parse(fixture_text("british_museum.html"), sources["british_museum"])
    prices = {e.title: e.price_text for e in evs}
    assert prices["Korea"] is None, "'Book now' is not a price"
    assert prices["Declaring independence USA 250"] == "Free"


@pytest.mark.parametrize("key, title", [
    ("courtauld", "Salman Toor: Someone Like You"),
    ("estorick", "Wanda and Marion Wulz: Avant-garde Photography in 20th-Century Italy"),
])
def test_single_exhibition_galleries(fixture_text, sources, key, title):
    evs = html_css.parse(fixture_text(f"{key}.html"), sources[key])
    assert marked(evs, sources[key]) == {title}


# --------------------------------------------------------------------------
# Pagination that ends in a 404
# --------------------------------------------------------------------------

def test_a_404_past_the_last_page_is_the_end(monkeypatch, fixture_text, sources):
    """Tate answers 404 one past its last page.  The pages served here are the
    real fixtures; only the 404 is simulated."""
    pages = {1: fixture_text("tate_p1.html"), 2: fixture_text("tate_p2.html")}

    def fake_fetch(url, **_):
        n = int(url.rsplit("page=", 1)[1]) if "page=" in url else 1
        if n in pages:
            return pages[n]
        resp = requests.Response()
        resp.status_code = 404
        raise requests.HTTPError(response=resp)

    monkeypatch.setattr(html_css, "fetch", fake_fetch)
    raw = html_css.fetch_raw(sources["tate"])
    assert html_css.last_fetch == {"pagination_complete": True, "pages_fetched": 2}
    assert raw.count(html_css.PAGE_BREAK) == 1


def test_other_errors_on_a_later_page_still_raise(monkeypatch, fixture_text, sources):
    def fake_fetch(url, **_):
        if "page=" not in url:
            return fixture_text("tate_p1.html")
        resp = requests.Response()
        resp.status_code = 503
        raise requests.HTTPError(response=resp)

    monkeypatch.setattr(html_css, "fetch", fake_fetch)
    with pytest.raises(requests.HTTPError):
        html_css.fetch_raw(sources["tate"])


# --------------------------------------------------------------------------
# The build
# --------------------------------------------------------------------------

@pytest.fixture
def gallery(tmp_path):
    src = tmp_path / "sources.yaml"
    src.write_text(yaml.safe_dump({"sources": [{
        "key": "stubby", "name": "Stub gallery", "kind": STUB_KIND,
        "verify_url": "https://example.org", "exhibition_topics": ["Exhibition"],
    }]}))
    return src, tmp_path / "out" / "events.json", tmp_path


def _run(paths, **kw):
    src, out, root = paths
    return build_mod.build(src, out, root=root, link_check=False, **kw)


class TestBuildMarksExhibitions:
    def _events(self):
        # Synthetic: one run labelled as an exhibition, one unlabelled run,
        # and a one-off event carrying the label.
        return [
            ev(1, title="A show", ongoing=True, topics=["Exhibition"],
               end=future(60)),
            ev(2, title="A course", ongoing=True, topics=["Course"], end=future(60)),
            ev(3, title="A tour", topics=["Exhibition"]),
        ]

    def test_the_flag_is_published(self, stub, gallery):  # noqa: F811
        stub.events = self._events()
        doc = _run(gallery)
        flags = {e["title"]: e["exhibition"] for e in doc["events"]}
        assert flags == {"A show": True, "A course": False, "A tour": False}

    def test_carried_records_are_marked_by_todays_rule(self, stub, gallery):  # noqa: F811
        """A file written before the flag existed must not leave a carried-over
        exhibition on the events page while its source is down."""
        stub.events = self._events()
        _run(gallery)
        out = gallery[1]
        doc = json.loads(out.read_text())
        for e in doc["events"]:
            e.pop("exhibition")
        out.write_text(json.dumps(doc))

        stub.error = RuntimeError("venue down")
        doc = _run(gallery)
        assert doc["sources"][0]["carried_over"] == 3
        flags = {e["title"]: e.get("exhibition") for e in doc["events"]}
        assert flags == {"A show": True, "A course": False, "A tour": False}


def test_the_published_file_has_exhibitions_on_both_sides_of_the_split():
    """Neither page may be empty."""
    from pathlib import Path
    data = Path(__file__).resolve().parent.parent / "docs/data/events.json"
    events = json.loads(data.read_text(encoding="utf-8"))["events"]
    exhibitions = [e for e in events if e.get("exhibition")]
    assert exhibitions and len(exhibitions) < len(events)
    assert all(e.get("ongoing") for e in exhibitions)
