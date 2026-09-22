"""Career and work-style tagging: the false positives found so far.

The tags are a discovery aid, not evidence, but a wrong one sends a parent to
the wrong event, so each verified false positive stays as a test.
"""

from __future__ import annotations

import pytest

from collector.careers import infer_careers, infer_work_styles


@pytest.mark.parametrize("text,must_not", [
    ("Civil service careers fair", "Engineering"),
    ("Doctor Who at the museum", "Medicine & health"),
    ("Court tennis in Tudor England", "Law & justice"),
    ("Plant machinery of the industrial age", "Biology, ecology & vets"),
    ("Cart workshop for beginners", "Arts & performance"),
    ("Old Bailey public gallery: criminal trials", "Arts & performance"),
    # "ancient magma chambers" is geoscience, not archaeology.
    ("From Magma to Magnets: ancient magma chambers in Greenland",
     "History & archaeology"),
    # A woodworking class is not computing.  The tag came from the V&A's
    # registry topic list, which is what the venue programmes overall.
    ("Woodworking Saturdays", "Computing & AI"),
    # "sensory needs" matched the stem "sensor".
    ("Studio Sunday Relaxed Session. A relaxed session aimed at children and "
     "families with additional sensory needs.", "Computing & AI"),
    # A business growing "in a way that feels sustainable" is not climate work.
    ("Business Fundamentals: Sales. Grow your creative business in a way "
     "that feels sustainable and aligned.", "Environment & climate"),
])
def test_verified_false_positives_stay_fixed(text, must_not):
    assert must_not not in infer_careers(text)


@pytest.mark.parametrize("text,expected", [
    ("AI: a talk", "Computing & AI"),
    ("Setting up a Staff Sustainability Network", "Environment & climate"),
    ("Innovation and energy for a sustainable future", "Environment & climate"),
    ("From Magma to Magnets: ancient magma chambers", "Earth & geoscience"),
    ("The Operating Theatre: 250 Years of Surgery", "Medicine & health"),
    ("Discover Engineering family workshop", "Engineering"),
    ("Ancient Rome: daily life", "History & archaeology"),
])
def test_true_positives(text, expected):
    assert expected in infer_careers(text)


class TestWorkStyles:
    """The second axis: what you would be doing, not what it is about."""

    def test_a_subject_label_is_not_a_work_style(self):
        assert infer_work_styles("An evening lecture") == []

    def test_one_event_can_span_several_ways_of_working(self):
        got = infer_work_styles(
            "Battery research: designing and modelling new materials")
        assert {"Research & discovery", "Design & making",
                "Data & quantitative analysis"} <= set(got)

    def test_a_court_visit_is_argument_not_making(self):
        got = infer_work_styles("Watch criminal trials from the public gallery")
        assert "Argument & advocacy" in got
        assert "Design & making" not in got

    def test_conservation_is_fieldwork(self):
        got = infer_work_styles("Veterinary science, conservation and animal behaviour")
        assert "Fieldwork & outdoors" in got
        assert "Caring & clinical work" in got

    def test_the_two_axes_are_independent(self):
        text = "A chemistry lecture with hands-on experiments"
        assert "Chemistry & materials" in infer_careers(text)
        assert "Research & discovery" in infer_work_styles(text)
