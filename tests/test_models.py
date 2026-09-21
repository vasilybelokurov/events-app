"""Parser tests.

Every case here is a real failure that was found and fixed, either by me or by
the independent Codex review, so the file doubles as a regression list.
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from collector.models import (Event, parse_age_range, parse_time_text,
                              parse_uk_datetime, price_info, shift)


class TestAgeRange:
    @pytest.mark.parametrize("text,expected", [
        ("Suitable for ages 11-16", (11, 16)),
        ("ages 11–16", (11, 16)),
        ("Young people 13+", (13, None)),
        ("Age 12 plus", (12, None)),
        ("12 and over", (12, None)),
        ("Children 12 and under", (None, 12)),
        ("Adults", (None, None)),
        ("a lovely evening talk", (None, None)),
        ("", (None, None)),
        (None, (None, None)),
    ])
    def test_basic(self, text, expected):
        assert parse_age_range(text) == expected

    def test_exclusive_under_is_off_by_one(self):
        """'under 14s' excludes 14-year-olds; 'up to 14' does too."""
        assert parse_age_range("under 14s") == (None, 13)
        assert parse_age_range("up to 14") == (None, 13)

    def test_admission_bar_becomes_a_minimum(self):
        assert parse_age_range("under-14s are not admitted") == (14, None)
        assert parse_age_range("no under 16s") == (16, None)

    def test_supervision_clause_is_not_an_age_limit(self):
        """A chaperone rule must not turn into a minimum or maximum age."""
        assert parse_age_range("Under 16s must be accompanied by an adult") == (None, None)

    def test_supervision_and_exclusion_in_one_string(self):
        got = parse_age_range(
            "Under 16s must be accompanied; under 5s are not admitted")
        assert got == (5, None)

    def test_date_range_is_not_an_age_range(self):
        assert parse_age_range("10-12 June; ages 14+") == (14, None)
        assert parse_age_range("Runs 3-7 Nov") == (None, None)

    def test_contradiction_keeps_the_minimum(self):
        assert parse_age_range("14+; under 10s only") == (14, None)


class TestTimeText:
    @pytest.mark.parametrize("text,expected", [
        ("7.00pm - 8.30pm", (time(19, 0), time(20, 30))),
        ("7.00pm – 8.30pm", (time(19, 0), time(20, 30))),
        ("11.30am", (time(11, 30), None)),
        ("10.00am-4.00pm", (time(10, 0), time(16, 0))),
        ("2.30pm – 3.45pm", (time(14, 30), time(15, 45))),
        ("11:00–16:00", (time(11, 0), time(16, 0))),
        ("all afternoon", (None, None)),
        ("", (None, None)),
    ])
    def test_basic(self, text, expected):
        assert parse_time_text(text) == expected

    def test_trailing_meridiem_governs_a_bare_start(self):
        assert parse_time_text("7 - 8.30pm") == (time(19, 0), time(20, 30))
        assert parse_time_text("7.00 - 8.30pm") == (time(19, 0), time(20, 30))

    def test_doors_time_is_not_the_start(self):
        assert parse_time_text("Doors 6pm; talk 7pm-8pm") == (time(19, 0), time(20, 0))

    def test_timezone_suffix_is_ignored(self):
        assert parse_time_text("7.20pm – 8.45pm BST/GMT") == (time(19, 20), time(20, 45))


class TestDateTime:
    def test_iso_is_not_parsed_day_first(self):
        """The June bug: dayfirst=True turned 2026-06-01 into 6 January."""
        assert parse_uk_datetime("2026-06-01T12:00:00+05:00").startswith("2026-06-01")

    def test_offset_is_converted_to_london(self):
        assert parse_uk_datetime("2026-06-01T12:00:00+05:00") == "2026-06-01T08:00:00+01:00"

    def test_uk_style_dates_are_day_first(self):
        assert parse_uk_datetime("17/01/2026").startswith("2026-01-17")

    def test_naive_input_is_london_local(self):
        assert parse_uk_datetime("2026-07-01 19:00") == "2026-07-01T19:00:00+01:00"

    def test_summer_and_winter_offsets(self):
        assert parse_uk_datetime(date(2026, 3, 29), default_time=time(19)) \
            == "2026-03-29T19:00:00+01:00"
        assert parse_uk_datetime(date(2026, 10, 25), default_time=time(19)) \
            == "2026-10-25T19:00:00+00:00"

    def test_nonexistent_local_time_moves_forward(self):
        """01:30 does not exist on the spring-forward day."""
        assert parse_uk_datetime(datetime(2026, 3, 29, 1, 30)) == "2026-03-29T02:30:00+01:00"

    def test_unparseable_returns_none(self):
        assert parse_uk_datetime("sometime soon") is None
        assert parse_uk_datetime(None) is None

    def test_shift_respects_the_clock_change(self):
        """Noon + 1 day across spring-forward is noon, not 11:00."""
        assert shift("2026-03-28T12:00:00+00:00", days=1) == "2026-03-29T12:00:00+01:00"
        assert shift("2026-10-24T12:00:00+01:00", days=1) == "2026-10-25T12:00:00+00:00"


class TestPrice:
    @pytest.mark.parametrize("text,expected", [
        ("Free", (True, 0.0, None)),
        ("Free entry, booking required", (True, 0.0, None)),
        ("£16/£10/£7 Ri Members", (False, 7.0, "GBP")),
        ("Standard £400", (False, 400.0, "GBP")),
        ("", (None, None, None)),
        (None, (None, None, None)),
        ("Donations welcome", (None, None, None)),
    ])
    def test_basic(self, text, expected):
        assert price_info(text) == expected

    def test_negation_is_not_free(self):
        assert price_info("Not free") == (None, None, None)

    def test_thousands_separator(self):
        assert price_info("£1,200") == (False, 1200.0, "GBP")

    def test_free_child_ticket_does_not_make_the_outing_free(self):
        is_free, cheapest, currency = price_info("£0 child; £25 adult")
        assert is_free is False
        assert cheapest == 0.0
        assert currency == "GBP"

    def test_non_sterling_currency_is_recorded(self):
        assert price_info("$20") == (False, 20.0, "USD")


def ev(**kw) -> Event:
    base = dict(id="i", title="t", url="https://example.org", source="s",
                source_name="S")
    base.update(kw)
    return Event(**base)


class TestEligibility:
    def test_three_states(self):
        assert ev(age_min=13).eligibility(14) == "eligible"
        assert ev(age_min=16).eligibility(14) == "excluded"
        assert ev(age_max=12).eligibility(14) == "excluded"
        assert ev().eligibility(14) == "unknown"

    def test_children_only_audience_excludes_a_teenager(self):
        assert ev(audiences=["children"]).eligibility(14) == "excluded"

    def test_teen_audience_is_eligible_without_numbers(self):
        assert ev(audiences=["teens"]).eligibility(14) == "eligible"

    def test_adult_labelled_event_is_unknown_not_excluded(self):
        """RI adult talks admit younger attendees with a parent, so 'adults'
        alone must not filter a 14-year-old out."""
        e = ev(audiences=["adults"])
        assert e.eligibility(14) == "unknown"
        assert e.suits_age(14) is True

    def test_suits_age_accepts_unknown(self):
        assert ev().suits_age(14) is True
        assert ev(age_min=18).suits_age(14) is False


def test_to_dict_drops_empties():
    d = ev(summary=None, topics=[]).to_dict()
    assert "summary" not in d and "topics" not in d
    assert d["title"] == "t"
