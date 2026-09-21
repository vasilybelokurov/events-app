"""De-duplication tests.

The failure mode to guard against is not "a duplicate slipped through" but
"two different events were merged into one, and a fact from one of them was
attributed to the other".
"""

from __future__ import annotations

import copy

from collector.dedup import merge
from collector.models import Event


def ev(**kw) -> Event:
    base = dict(id="a-1", title="Talk", url="https://a.example/1", source="a",
                source_name="A", start="2026-11-13T18:00:00+00:00", city="London")
    base.update(kw)
    return Event(**base)


def test_identical_events_from_two_sources_collapse():
    out = merge([ev(source="a", id="a-1"), ev(source="b", id="b-1")],
                {"a": 10, "b": 20})
    assert len(out) == 1
    assert "also listed by b" in out[0].provenance


def test_two_sessions_on_the_same_day_stay_separate():
    morning = ev(id="a-1", start="2026-10-31T14:30:00+00:00")
    evening = ev(id="a-2", start="2026-10-31T19:00:00+00:00")
    assert len(merge([morning, evening])) == 2


def test_same_title_different_city_stays_separate():
    a = ev(id="a-1", city="London")
    b = ev(id="b-1", city="Cambridge", source="b")
    assert len(merge([a, b])) == 2


def test_parenthetical_qualifiers_are_not_stripped():
    bsl = ev(id="a-1", title="Museum tour (BSL)")
    eng = ev(id="a-2", title="Museum tour (English)")
    assert len(merge([bsl, eng])) == 2


def test_undated_events_never_merge():
    a = ev(id="a-1", start=None)
    b = ev(id="b-1", start=None, source="b")
    assert len(merge([a, b])) == 2


def test_paid_does_not_become_free():
    paid = ev(id="a-1", price_text="£16", is_free=False, price_from=16.0)
    free = ev(id="b-1", source="b", price_text="Free", is_free=True, price_from=0.0)
    out = merge([paid, free], {"a": 10, "b": 20})
    assert out[0].is_free is False
    assert out[0].price_from == 16.0


def test_a_legitimate_zero_is_not_treated_as_missing():
    """0 == False in Python; a free price or a zero coordinate must survive."""
    zero = ev(id="a-1", price_from=0.0, is_free=True, lat=0.0)
    other = ev(id="b-1", source="b", price_from=99.0, is_free=False, lat=51.5)
    out = merge([zero, other], {"a": 10, "b": 20})
    assert out[0].price_from == 0.0
    assert out[0].is_free is True
    assert out[0].lat == 0.0


def test_age_bounds_are_copied_as_a_pair():
    """Mixing age_min from one record with age_max from another can produce an
    impossible range such as 14..12."""
    a = ev(id="a-1", age_min=14, age_text="14+")
    b = ev(id="b-1", source="b", age_max=12, age_text="12 and under")
    out = merge([a, b], {"a": 10, "b": 20})
    assert out[0].age_min == 14
    assert out[0].age_max is None


def test_gaps_are_filled_from_the_other_source():
    thin = ev(id="a-1", summary=None, venue_name=None)
    rich = ev(id="b-1", source="b", summary="A good talk", venue_name="Ri")
    out = merge([thin, rich], {"a": 10, "b": 20})
    assert out[0].summary == "A good talk"
    assert out[0].venue_name == "Ri"


def test_lists_are_unioned():
    a = ev(id="a-1", careers=["Engineering"], topics=["Physics"])
    b = ev(id="b-1", source="b", careers=["Physics & space"], topics=["Physics"])
    out = merge([a, b], {"a": 10, "b": 20})
    assert out[0].careers == ["Engineering", "Physics & space"]
    assert out[0].topics == ["Physics"]


def test_winning_id_follows_source_priority_not_richness():
    """The surviving id must not change just because a source added prose:
    saved/dismissed state in the browser is keyed on it."""
    thin_primary = ev(id="a-1", source="a")
    rich_secondary = ev(id="b-1", source="b", summary="lots of detail",
                        venue_name="Ri", price_text="£16", booking_url="https://x.example")
    out = merge([thin_primary, rich_secondary], {"a": 10, "b": 20})
    assert out[0].id == "a-1"
    assert out[0].source == "a"


def test_inputs_are_not_mutated():
    a = ev(id="a-1", summary=None)
    b = ev(id="b-1", source="b", summary="added later")
    before = copy.deepcopy(a)
    merge([a, b], {"a": 10, "b": 20})
    assert a == before


def test_output_is_sorted_by_start():
    late = ev(id="a-2", start="2026-12-01T19:00:00+00:00")
    early = ev(id="a-1", start="2026-10-01T19:00:00+01:00", title="Other")
    out = merge([late, early])
    assert [e.id for e in out] == ["a-1", "a-2"]
