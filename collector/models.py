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
#: "Tuesday 20 October 10.30" holds one clock time, not two: the 20 is a day
#: of the month.  Left in, it becomes the start token, and because a bare
#: number with no meridiem is rejected as ambiguous the real time is demoted
#: to the *end* time and the event loses its start.
_DAY_BEFORE_MONTH = re.compile(rf"^\s*(?:st|nd|rd|th)?\s*(?:{_MONTHS})", re.I)
_MONTH_BEFORE_DAY = re.compile(rf"(?:{_MONTHS})[a-z]*\s*$", re.I)


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
            if mm is None and (_DAY_BEFORE_MONTH.match(clause[m.end():])
                               or _MONTH_BEFORE_DAY.search(clause[:m.start()])):
                continue                      # a date, not a time
            tokens.append((hh, int(mm) if mm is not None else None,
                           ap.lower() if ap else None))
        # Prefer the clause that actually looks like the event's own time span.
        if len(tokens) > len(best) or (tokens and not best):
            best = tokens
        if len(best) >= 2:
            break

    if not best:
        return (None, None)
    tokens = best[:3]
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

    # An unusable leading token must not push a real time into the end slot.
    # "Monday 16 18.30 - Monday 23 November 18.30" opens with a bare day
    # number, and reading that as the start left the event with an end time
    # and no start at all.
    resolved = [t_ for t_ in (to_time(tok, False) for tok in tokens) if t_]
    start = resolved[0] if resolved else None
    end = resolved[1] if len(resolved) > 1 else None
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


_DOTTED_TIME = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{2})(?![\d.])")
#: A real date names a day *and* a month, or is written numerically.  Anything
#: less is not a date, however confidently a fuzzy parser reads one out of it.
_DAY_AND_MONTH = re.compile(
    rf"\d{{1,2}}\s*(?:st|nd|rd|th)?\s*(?:{_MONTHS})|(?:{_MONTHS})[a-z]*\s*\d{{1,2}}", re.I)
#: Dots are allowed only in the full "17.01.2026" form: "11.00" is a time.
_NUMERIC_DATE = re.compile(r"\d{1,2}\s*[/-]\s*\d{1,2}(?:\s*[/-]\s*\d{2,4})?"
                           r"|\d{1,2}\.\d{1,2}\.\d{2,4}")


def _looks_like_a_date(raw: str) -> bool:
    """Whether *raw* holds enough to be a date at all.

    ``dateutil``'s fuzzy mode will find a date in almost any string: it reads
    "Once a month, Wednesdays, 11.00" as the 11th of the current month and
    "Most Fridays at 11.30" as the 11th too.  Those are recurrence
    descriptions, and a venue that publishes one has not published a date.
    Inventing one puts a fabricated entry on the page, which is worse than
    omitting the event, so the burden of proof sits here.
    """
    return bool(_DAY_AND_MONTH.search(raw) or _NUMERIC_DATE.search(raw))


def _clock_dots(raw: str) -> str:
    """Rewrite a dotted clock time as ``HH:MM`` when the text names a month.

    A fuzzy date parser reads the "10.30" of "Tuesday 20 October 10.30" as a
    *year* and returns 20 October **2010**, which is silently dropped as a
    past event.  Once a month name is present the dots cannot be a ``d.m``
    date, so the rewrite is unambiguous; without one the string is left alone.
    """
    if not re.search(_MONTHS, raw, re.I):
        return raw

    def sub(m: re.Match) -> str:
        if int(m.group(1)) > 23 or int(m.group(2)) > 59:
            return m.group(0)
        return f"{m.group(1)}:{m.group(2)}"

    return _DOTTED_TIME.sub(sub, raw)


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
            probe = _clock_dots(raw)
            if not _looks_like_a_date(probe):
                return None
            try:
                dt = dtparser.parse(probe, dayfirst=True, fuzzy=True)
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


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]|$)")
_RANGE_SPLIT = re.compile(r"\s*(?:\u2013|\u2014|\u2212|--|\sto\s|\s-\s|(?<=\d)-(?=\d{1,2}\w))\s*")


#: How far into the past a *yearless* date may fall before a source that has
#: asked for it (``assume_future_dates``) has the date read as next year's.
#: A run that began a few weeks ago is still current and must not be rolled.
YEARLESS_GRACE_DAYS = 90


def _roll_year(iso: str | None) -> str | None:
    """Move an ISO timestamp forward one year, keeping its wall-clock time."""
    if not iso:
        return None
    naive = datetime.fromisoformat(iso).replace(tzinfo=None)
    try:
        naive = naive.replace(year=naive.year + 1)
    except ValueError:                          # 29 February
        naive = naive.replace(year=naive.year + 1, month=2, day=28)
    return _localise(naive).isoformat()


def _roll_yearless(raw: str, start: str | None, end: str | None,
                   assume_future: bool) -> tuple[str | None, str | None]:
    """Read a long-past yearless date as next year's, where that is warranted.

    ``dateutil`` fills a missing year with the current one, so a listing that
    says "Saturday 3 January" resolves to the January that has already gone.
    The event is then dropped as past and nobody sees it was ever there.

    Whether that reading is right is a fact about the *source*, not about the
    string, so it is declared per source and off by default.  A live what's-on
    listing only advertises what is coming, so its yearless January is next
    January.  An archived programme is the opposite case: the Cambridge
    Festival page still holds the 2025 programme, and rolling its "Monday 2
    March" forward would publish a fabricated 2027 event for something that
    happened two years ago.  Both ends move together, so a range cannot be
    turned inside out.
    """
    if not start or not assume_future or re.search(r"\d{4}", raw):
        return (start, end)
    cutoff = datetime.now(UK) - timedelta(days=YEARLESS_GRACE_DAYS)
    if datetime.fromisoformat(start) >= cutoff:
        return (start, end)
    return (_roll_year(start), _roll_year(end))


def parse_date_range(text: str | None, *, default_time: time | None = None,
                     assume_future: bool = False
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

    # An ISO-8601 timestamp is one instant, never a range.  The range splitter
    # breaks on a hyphen between digits, so "2027-10-01T09:00:00+01:00" was
    # torn into "2027-10" and "01T09:00:00+01:00" and parsed as neither.
    if _ISO_DATE.match(raw):
        iso = parse_uk_datetime(raw, default_time=default_time)
        if iso:
            return (iso, None)

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
                return _roll_yearless(raw, start, end, assume_future)
            if start:
                return _roll_yearless(raw, start, None, assume_future)
    return _roll_yearless(
        raw, parse_uk_datetime(raw, default_time=default_time), None,
        assume_future)


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
