"""Merge duplicate events arriving from more than one source.

Conservative by design.  A wrongly merged pair loses a real event (two
different tours on the same afternoon collapse into one), which is worse than
showing a near-duplicate, so two records are only treated as the same event
when the title, the *start time* and the city all agree.  Merging then fills
gaps only: it never overwrites a value that a source actually stated.
"""

from __future__ import annotations

import copy
import re
from collections import defaultdict

from .models import Event

#: Fields that are never copied from a loser onto the winner.
_NEVER_MERGE = frozenset({"id", "source", "source_name", "url", "provenance",
                          "link_status", "last_seen", "verified_on"})

#: Age bounds are copied as a pair or not at all: taking ``age_min`` from one
#: record and ``age_max`` from another can manufacture an impossible range.
_ATOMIC_GROUPS = (("age_min", "age_max", "age_text"),
                  ("price_text", "is_free", "price_from", "currency"))


def _norm_title(title: str) -> str:
    """Normalise a title for comparison, keeping parenthetical qualifiers.

    "Tour (BSL)" and "Tour (English)" are different events, so the bracketed
    text stays in the key; only punctuation, case and filler words go.
    """
    t = title.lower()
    t = re.sub(r"[^a-z0-9()]+", " ", t)
    t = re.sub(r"\b(the|a|an|of|and|for|with|to|in|on)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _key(e: Event) -> tuple:
    """Identity for cross-source matching.

    Includes the start *time* (to the minute), so two showings of the same talk
    on one day stay separate, and the city, so a touring event is not merged
    across venues.  An event with no start date is never merged.
    """
    if not e.start:
        return ("__unique__", e.id)
    if e.anytime:
        # Standing venue records all start "today" and are often titled after
        # the page ("What's on"), so title+time+city would merge two different
        # venues.  A venue is only ever itself.
        return ("__venue__", e.source, e.url)
    return (_norm_title(e.title), e.start[:16], (e.city or "").lower())


def _richness(e: Event) -> int:
    """How much usable detail a record carries."""
    score = 0
    for attr in ("summary", "venue_name", "price_text", "booking_url",
                 "time_text", "age_text", "end"):
        if getattr(e, attr):
            score += 1
    score += len(e.audiences) + len(e.topics)
    if e.lat is not None:
        score += 1
    return score


def merge(events: list[Event], priority: dict[str, int] | None = None) -> list[Event]:
    """Collapse duplicates, keeping the highest-priority record and filling gaps.

    ``priority`` maps source key -> rank (lower wins).  Priority is checked
    *before* richness so that the surviving event id does not change when a
    source merely adds a sentence of description; saved/dismissed state in the
    browser keys off that id.

    Inputs are not mutated.
    """
    priority = priority or {}
    groups: dict[tuple, list[Event]] = defaultdict(list)
    for e in events:
        groups[_key(e)].append(e)

    out: list[Event] = []
    for group in groups.values():
        group = sorted(group, key=lambda e: (priority.get(e.source, 50), -_richness(e),
                                             e.id))
        winner = copy.deepcopy(group[0])
        for other in group[1:]:
            for attr, value in vars(other).items():
                if attr in _NEVER_MERGE:
                    continue
                current = getattr(winner, attr)
                if isinstance(value, list):
                    setattr(winner, attr, sorted(set(current) | set(value)))
                elif current is None and value is not None:
                    group_of = next((g for g in _ATOMIC_GROUPS if attr in g), None)
                    if group_of and any(getattr(winner, a) is not None
                                        for a in group_of):
                        continue          # partially filled: do not mix sources
                    setattr(winner, attr, value)
        if len(group) > 1:
            extra = sorted({e.source for e in group[1:]})
            winner.provenance = "; ".join(
                filter(None, [winner.provenance, "also listed by " + ", ".join(extra)])
            )
        out.append(winner)
    out.sort(key=lambda e: (e.start or "9999", e.title))
    return out
