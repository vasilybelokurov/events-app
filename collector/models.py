"""Canonical event record, plus parsers that normalise messy source data.

The pipeline is deliberately source-agnostic: every adapter emits ``Event``
objects, so filtering, de-duplication and the front end only ever see one
shape.  All times are stored as ISO-8601 strings with an explicit UTC offset
(Europe/London for UK sources) so the browser can render them without guessing.

The parsers here are the riskiest part of the project, because venue prose is
not data.  They therefore follow two rules:

* never invent precision -- an unparseable field stays ``None`` and the raw
  text is kept for display;
* never let a *restriction* become a *permission* -- an age or price rule that
  cannot be read confidently yields "unknown", not "fine".
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil import parser as dtparser

UK = ZoneInfo("Europe/London")

#: Controlled audience vocabulary.  Source labels are mapped onto these.
AUDIENCES = ("children", "families", "teens", "adults", "schools")

#: Attendance status.  Anything other than ``scheduled`` is shown as a badge
#: and excluded from the default view.
STATUSES = ("scheduled", "cancelled", "postponed", "sold_out")

_AUDIENCE_MAP = {
    "children 12 and under": "children",
    "children": "children",
    "family": "families",
    "families": "families",
    "young people 13+": "teens",
    "young people": "teens",
    "teens": "teens",
    "youth event": "teens",
    "youth": "teens",
    "adults": "adults",
    "adult": "adults",
    "schools": "schools",
    "school groups": "schools",
    "students": "schools",
}


def map_audience(label: str) -> str | None:
    """Map a free-text audience label onto :data:`AUDIENCES`."""
    key = re.sub(r"\s+", " ", (label or "").strip().lower())
    if key in _AUDIENCE_MAP:
        return _AUDIENCE_MAP[key]
    for needle, value in _AUDIENCE_MAP.items():
        if needle in key:
            return value
    return None


# --------------------------------------------------------------------------
# Ages
# --------------------------------------------------------------------------

_DASH = r"[-‐-―−]"
_MONTHS = (r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec")

#: Clauses about supervision are not age limits: "under 16s must be
#: accompanied" tells us nothing about whether a 14-year-old may attend.
_SUPERVISION = re.compile(r"accompan|supervis|with an adult|parent must", re.I)
_EXCLUSION = re.compile(r"not admitted|no under|not permitted|must be (?:aged )?over", re.I)


#: Clause separators.  A bare "." would split "7.00pm", so a full stop only
#: ends a clause when followed by whitespace or the end of the string.
_CLAUSE_SPLIT = re.compile("[;" + chr(0x2022) + chr(10) + "]|" + re.escape(".") + r"(?=\s|$)|\s{2,}")


def _clauses(text: str) -> list[str]:
    return [c for c in _CLAUSE_SPLIT.split(text) if c and c.strip()]


def parse_age_range(text: str | None) -> tuple[int | None, int | None]:
    """Extract an explicit numeric age range from free text.

    Returns ``(age_min, age_max)`` with ``None`` for an open end.  Only
    *explicit* numbers are used; vague labels such as "adults" are handled by
    :func:`map_audience` instead, because an adult-labelled talk is usually
    still open to a teenager with a parent.

    Each clause is read separately, so a supervision rule cannot masquerade as
    an age floor, and a date range cannot masquerade as an age range.

    >>> parse_age_range("Suitable for ages 11-16")
    (11, 16)
    >>> parse_age_range("Young people 13+")
    (13, None)
    >>> parse_age_range("Children 12 and under")
    (None, 12)
    >>> parse_age_range("under-14s are not admitted")
    (14, None)
    >>> parse_age_range("under 14s")
    (None, 13)
    >>> parse_age_range("Under 16s must be accompanied; under 5s are not admitted")
    (5, None)
    >>> parse_age_range("10-12 June; ages 14+")
    (14, None)
    >>> parse_age_range("a lovely evening talk")
    (None, None)
    """
    if not text:
        return (None, None)

    mins: list[int] = []
    maxs: list[int] = []

    for clause in _clauses(re.sub(r"\s+", " ", text)):
        c = clause.lower()
        supervision_only = bool(_SUPERVISION.search(c)) and not _EXCLUSION.search(c)

        # "under-14s are not admitted" -> nobody below 14 may attend.
        m = re.search(rf"under(?:{_DASH}|\s)?(\d{{1,2}})s?", c)
        if m and _EXCLUSION.search(c):
            mins.append(int(m.group(1)))
            continue
        if supervision_only:
            continue

        # Age ranges need an age word, otherwise "10-12 June" wins.
        m = re.search(
            rf"(?:age[sd]?|years?|yrs?|olds?)\D{{0,8}}(\d{{1,2}})\s*(?:{_DASH}|to)\s*(\d{{1,2}})",
            c)
        if not m:
            m = re.search(
                rf"(\d{{1,2}})\s*(?:{_DASH}|to)\s*(\d{{1,2}})\s*(?:year|yr)[- ]?olds?", c)
        if not m and not re.search(_MONTHS, c):
            m = re.search(rf"\b(\d{{1,2}})\s*{_DASH}\s*(\d{{1,2}})\b", c)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo <= hi <= 25:
                mins.append(lo)
                maxs.append(hi)
                continue

        # "13+", "age 12 plus", "12 and over"
        m = re.search(r"(?:age[sd]?\s*)?(\d{1,2})\s*(?:\+|plus\b|and over\b|and above\b|or over\b)", c)
        if m:
            mins.append(int(m.group(1)))
            continue

        # "12 and under" (inclusive) vs "under 12" / "up to 12" (exclusive).
        m = re.search(r"(\d{1,2})\s*(?:and|or)\s*(?:under|below|younger)", c)
        if m:
            maxs.append(int(m.group(1)))
            continue
        m = re.search(r"(?:under|below|up to)\s*(\d{1,2})", c)
        if m:
            maxs.append(int(m.group(1)) - 1)
            continue

    age_min = max(mins) if mins else None
    age_max = min(maxs) if maxs else None
    if age_min is not None and age_max is not None and age_min > age_max:
        # Contradictory prose: keep the restriction we are more sure of (the
        # explicit minimum) and admit we do not know the ceiling.
        age_max = None
    return (age_min, age_max)


# --------------------------------------------------------------------------
# Times and dates
# --------------------------------------------------------------------------

_TIME_TOKEN = re.compile(r"(?<![\d:.])(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?", re.I)
#: Times introduced by these words describe access, not the event itself.
_NOT_START = re.compile(r"(doors?|bar|refreshments?|arrive|registration)\s*(?:open\w*)?\s*$", re.I)


def parse_time_text(text: str | None) -> tuple[time | None, time | None]:
    """Pull a start/end clock time out of strings like ``"7.00pm - 8.30pm"``.

    A trailing meridiem governs an earlier bare time, ``doors open`` times are
    ignored, and a value is only accepted when it is unambiguous.

    >>> parse_time_text("7.00pm - 8.30pm")
    (datetime.time(19, 0), datetime.time(20, 30))
    >>> parse_time_text("11.30am")
    (datetime.time(11, 30), None)
    >>> parse_time_text("7 - 8.30pm")
    (datetime.time(19, 0), datetime.time(20, 30))
    >>> parse_time_text("7.00 - 8.30pm")
    (datetime.time(19, 0), datetime.time(20, 30))
    >>> parse_time_text("Doors 6pm; talk 7pm-8pm")
    (datetime.time(19, 0), datetime.time(20, 0))
    >>> parse_time_text("10.00am-4.00pm")
    (datetime.time(10, 0), datetime.time(16, 0))
    >>> parse_time_text("all afternoon")
    (None, None)
    """
    if not text:
        return (None, None)
    t = re.sub(rf"{_DASH}", "-", text.lower())

    best: list[tuple[int, int | None, str | None]] = []
    for clause in _clauses(t) or [t]:
        tokens: list[tuple[int, int | None, str | None]] = []
        for m in _TIME_TOKEN.finditer(clause):
            hh, mm, ap = int(m.group(1)), m.group(2), m.group(3)
            if hh > 23 or (mm is not None and int(mm) > 59):
                continue
            if _NOT_START.search(clause[:m.start()].rstrip()):
                continue
            tokens.append((hh, int(mm) if mm is not None else None,
                           ap.lower() if ap else None))
        # Prefer the clause that actually looks like the event's own time span.
        if len(tokens) > len(best) or (tokens and not best):
            best = tokens
        if len(best) >= 2:
            break

    if not best:
        return (None, None)
    tokens = best[:2]
    meridiems = [t_[2] for t_ in tokens if t_[2]]
    fallback = meridiems[-1] if meridiems else None

    def to_time(tok, is_end: bool) -> time | None:
        hh, mm, ap = tok
        ap = ap or fallback
        if ap is None and mm is None:
            return None            # a bare "7" with no context is ambiguous
        if ap == "pm" and hh < 12:
            hh += 12
        elif ap == "am" and hh == 12:
            hh = 0
        return time(hh, mm or 0)

    start = to_time(tokens[0], False)
    end = to_time(tokens[1], True) if len(tokens) > 1 else None
    if start and end and end <= start and fallback == "pm" and start.hour >= 12:
        # e.g. "19:00 - 8.30" where the end lost its meridiem: assume evening.
        if end.hour < 12:
            end = time(end.hour + 12, end.minute)
    return (start, end)


def combine_audience_values(values) -> tuple[int | None, int | None, list[str]]:
    """Combine a list of audience labels with **union** semantics.

    A venue that tags an exhibition "12+, Adults (18+), All ages, Families" is
    saying it suits any of those, not all of them at once.  Intersecting the
    bounds would take 18+ from one label and exclude a 14-year-old from an
    exhibition explicitly marked "All ages", so the widest range wins.

    Returns ``(age_min, age_max, audiences)``.

    >>> combine_audience_values(["12+", "Adults (18+)", "All ages", "Families"])
    (None, None, ['adults', 'children', 'families', 'teens'])
    >>> combine_audience_values(["Adults (18+)"])
    (18, None, ['adults'])
    >>> combine_audience_values(["5-11"])
    (5, 11, ['children'])
    >>> combine_audience_values(["12+"])  # spans a 12-year-old to an adult
    (12, None, ['adults', 'children', 'teens'])
    >>> combine_audience_values([])
    (None, None, [])
    """
    labels = [str(v).strip() for v in (values or []) if str(v).strip()]
    if not labels:
        return (None, None, [])

    bounds: list[tuple[int | None, int | None]] = []
    audiences: set[str] = set()
    for label in labels:
        low = label.lower()
        if "all age" in low or "everyone" in low or "any age" in low:
            bounds.append((None, None))
            audiences.add("families")
            continue
        lo, hi = parse_age_range(label)
        bounds.append((lo, hi))
        mapped = map_audience(label)
        if mapped:
            audiences.add(mapped)
        # Numeric labels such as "12+" or "5-11" carry no word to map, so the
        # audience is derived from the range itself.
        elif lo is not None or hi is not None:
            lo_eff = 0 if lo is None else lo
            hi_eff = 99 if hi is None else hi
            if lo_eff <= 12:
                audiences.add("children")
            if lo_eff <= 17 and hi_eff >= 13:
                audiences.add("teens")
            if hi_eff >= 18:
                audiences.add("adults")

    age_min = None if any(b[0] is None for b in bounds) else min(b[0] for b in bounds)
    age_max = None if any(b[1] is None for b in bounds) else max(b[1] for b in bounds)
    return (age_min, age_max, sorted(audiences))


def _localise(dt: datetime) -> datetime:
    """Attach Europe/London to a naive datetime, resolving DST edge cases.

    A time that does not exist (the spring-forward gap) is moved forward by an
    hour rather than silently given the wrong offset; an ambiguous time (the
    autumn repeat) takes the first of the two occurrences, which is what a
    venue listing means.
    """
    aware = dt.replace(tzinfo=UK, fold=0)
    # Round-tripping through UTC reveals a nonexistent local time.
    if aware.astimezone(timezone.utc).astimezone(UK).replace(tzinfo=None) != dt:
        aware = (dt + timedelta(hours=1)).replace(tzinfo=UK, fold=0)
    return aware


def parse_uk_datetime(value, *, default_time: time | None = None) -> str | None:
    """Parse a date/datetime into an ISO-8601 string anchored to Europe/London.

    ISO-8601 input is parsed as ISO-8601 (not day-first), offsets are converted
    to London, naive input is assumed to be London local time, and date-only
    input gets ``default_time`` (or midnight).  Returns ``None`` if
    unparseable.

    >>> parse_uk_datetime("2026-06-01T12:00:00+05:00")
    '2026-06-01T08:00:00+01:00'
    >>> parse_uk_datetime("17/01/2026")
    '2026-01-17T00:00:00+00:00'
    >>> parse_uk_datetime("2026-03-29T01:30:00")
    '2026-03-29T02:30:00+01:00'
    >>> parse_uk_datetime("not a date")
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
        had_time = True
    elif isinstance(value, date):
        dt = datetime.combine(value, default_time or time(0, 0))
        had_time = default_time is not None
    else:
        raw = str(value).strip()
        dt = None
        try:                                    # ISO first: never day-first
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = dtparser.parse(raw, dayfirst=True, fuzzy=True)
            except (ValueError, OverflowError, TypeError):
                return None
        had_time = bool(re.search(r"\d\s*[:.]\s*\d\d", raw))
        if not had_time and default_time is not None:
            dt = dt.replace(hour=default_time.hour, minute=default_time.minute)
            had_time = True
    if dt.tzinfo is not None:
        return dt.astimezone(UK).isoformat()
    return _localise(dt).isoformat()


def shift(iso: str, **delta) -> str:
    """Add a timedelta to an ISO string in *local* terms, so DST is respected.

    ``shift("2026-03-28T12:00:00+00:00", days=1)`` is noon the next day in
    London (``+01:00``), not 11:00.

    >>> shift("2026-03-28T12:00:00+00:00", days=1)
    '2026-03-29T12:00:00+01:00'
    """
    dt = datetime.fromisoformat(iso).astimezone(UK)
    naive = dt.replace(tzinfo=None) + timedelta(**delta)
    return _localise(naive).isoformat()


_RANGE_SPLIT = re.compile(r"\s*(?:\u2013|\u2014|\u2212|--|\sto\s|\s-\s|(?<=\d)-(?=\d{1,2}\w))\s*")


def parse_date_range(text: str | None, *, default_time: time | None = None
                     ) -> tuple[str | None, str | None]:
    """Split a written date range into ``(start, end)`` ISO strings.

    Venues write runs as one string, and the year (and often the month) appears
    only on the right-hand side: "10th September\u201331st October 2026" means
    September *2026*, not September of the current year.  Parsing only the
    left-hand date would put a live exhibition in the past and drop it.

    Returns ``(start, None)`` when the text holds a single date.

    >>> parse_date_range("10th September\u201331st October 2026")
    ('2026-09-10T00:00:00+01:00', '2026-10-31T00:00:00+00:00')
    >>> parse_date_range("17/01/2026 - 31/12/2026")
    ('2026-01-17T00:00:00+00:00', '2026-12-31T00:00:00+00:00')
    >>> parse_date_range("28th October 2026")
    ('2026-10-28T00:00:00+00:00', None)
    >>> parse_date_range("Exhibition 10th September 2026")
    ('2026-09-10T00:00:00+01:00', None)
    >>> parse_date_range(None)
    (None, None)
    """
    if not text or not str(text).strip():
        return (None, None)
    raw = re.sub(r"\s+", " ", str(text)).strip()

    # "On now until Saturday, 28 November 2026" is a run that has already
    # started; reading it as a single date would put a live exhibition in the
    # future and mis-sort it.
    m = re.match(r"^(?:on now\s+)?(?:until|till|through(?:out)?|ends?)\s+(.+)$",
                 raw, re.I)
    if m:
        end = parse_uk_datetime(m.group(1), default_time=default_time)
        if end:
            return (parse_uk_datetime(datetime.now(UK).date()), end)

    parts = [p.strip(" ,;") for p in _RANGE_SPLIT.split(raw) if p.strip(" ,;")]
    if len(parts) >= 2:
        left = parts[0]
        # The right-hand end is the last part that actually looks like a date:
        # "... to Wednesday 30 September 2026 - 7pm" splits a trailing time off,
        # and "7pm" is not the end of the run.
        right = next((p for p in reversed(parts)
                      if re.search(_MONTHS, p, re.I) or re.search(r"\d{4}", p)
                      or re.search(r"\d{1,2}[/.]\d{1,2}", p)), parts[-1])
        end = parse_uk_datetime(right, default_time=default_time)
        if end:
            # Borrow the year, and the month when absent, from the right side.
            year = end[:4]
            start = None
            if not re.search(r"\d{4}", left):
                has_month = re.search(_MONTHS, left, re.I) or re.search(r"[/.]", left)
                candidate = f"{left} {year}" if has_month else f"{left} {end[5:7]} {year}"
                start = parse_uk_datetime(candidate, default_time=default_time)
            if start is None:
                start = parse_uk_datetime(left, default_time=default_time)
            if start and start <= end:
                return (start, end)
            if start:
                return (start, None)
    return (parse_uk_datetime(raw, default_time=default_time), None)


# --------------------------------------------------------------------------
# Prices
# --------------------------------------------------------------------------

_FREE_RE = re.compile(r"(?<!not )\bfree\b", re.I)
_NOT_FREE_RE = re.compile(r"\b(?:not|non|no)[- ]free\b", re.I)
_PRICE_RE = re.compile(r"([£$€])\s?([\d,]+(?:\.\d{2})?)")
_CURRENCY = {"£": "GBP", "$": "USD", "€": "EUR"}


def price_info(text: str | None) -> tuple[bool | None, float | None, str | None]:
    """Return ``(is_free, cheapest, currency)`` inferred from a price string.

    ``is_free`` is only ``True`` when *nothing* costs money: a listing with a
    free child ticket and a paid adult ticket is not a free outing for a parent
    and child.

    >>> price_info("Free")
    (True, 0.0, None)
    >>> price_info("Not free")
    (None, None, None)
    >>> price_info("£1,200")
    (False, 1200.0, 'GBP')
    >>> price_info("£0 child; £25 adult")
    (False, 0.0, 'GBP')
    >>> price_info("£16/£10/£7 Ri Members")
    (False, 7.0, 'GBP')
    >>> price_info("")
    (None, None, None)
    """
    if not text or not text.strip():
        return (None, None, None)
    matches = _PRICE_RE.findall(text)
    prices, symbols = [], []
    for sym, num in matches:
        try:
            prices.append(float(num.replace(",", "")))
            symbols.append(sym)
        except ValueError:
            continue
    currency = _CURRENCY.get(symbols[0]) if symbols else None
    if prices:
        cheapest = min(prices)
        dearest = max(prices)
        return (dearest == 0, cheapest, currency)
    if _NOT_FREE_RE.search(text):
        return (None, None, None)
    if _FREE_RE.search(text):
        return (True, 0.0, None)
    return (None, None, None)


def make_id(source: str, key: str) -> str:
    """Stable short id so saved/dismissed state survives rebuilds."""
    digest = hashlib.sha1(f"{source}|{key}".encode()).hexdigest()
    return f"{source}-{digest[:12]}"


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------

@dataclass
class Event:
    """One event, normalised."""

    id: str
    title: str
    url: str
    source: str
    source_name: str
    start: str | None = None
    end: str | None = None
    all_day: bool = False
    time_text: str | None = None
    ongoing: bool = False          # long-running exhibition
    anytime: bool = False          # standing offer with no fixed date
    when_text: str | None = None   # verbatim schedule, e.g. "Fridays, 14:00"
    status: str = "scheduled"
    summary: str | None = None
    venue_name: str | None = None
    city: str | None = None
    lat: float | None = None
    lon: float | None = None
    online: bool = False
    price_text: str | None = None
    is_free: bool | None = None
    price_from: float | None = None
    currency: str | None = None
    booking_url: str | None = None
    booking: str | None = None     # None | "required" | "school_only"
    accompanied: bool | None = None
    audiences: list[str] = field(default_factory=list)
    age_min: int | None = None
    age_max: int | None = None
    age_text: str | None = None
    topics: list[str] = field(default_factory=list)
    careers: list[str] = field(default_factory=list)
    work_styles: list[str] = field(default_factory=list)
    provenance: str | None = None
    link_status: int | None = None
    verified_on: str | None = None
    last_seen: str | None = None

    def eligibility(self, age: int) -> str:
        """``"eligible"``, ``"excluded"`` or ``"unknown"`` for a given age.

        Three states rather than a boolean, because "no stated age limit" is
        not the same claim as "suitable for a 14-year-old".

        >>> Event("i", "t", "u", "s", "S", age_min=13).eligibility(14)
        'eligible'
        >>> Event("i", "t", "u", "s", "S", age_min=16).eligibility(14)
        'excluded'
        >>> Event("i", "t", "u", "s", "S", audiences=["children"]).eligibility(14)
        'excluded'
        >>> Event("i", "t", "u", "s", "S").eligibility(14)
        'unknown'
        """
        if self.age_min is not None and age < self.age_min:
            return "excluded"
        if self.age_max is not None and age > self.age_max:
            return "excluded"
        if self.audiences and set(self.audiences) == {"children"} and age > 12:
            return "excluded"
        if self.age_min is not None or self.age_max is not None:
            return "eligible"
        if "teens" in self.audiences and 13 <= age <= 17:
            return "eligible"
        if "families" in self.audiences:
            return "eligible"
        return "unknown"

    def suits_age(self, age: int) -> bool:
        """Not excluded for ``age`` (i.e. eligible *or* unknown)."""
        return self.eligibility(age) != "excluded"

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, [], "")}
