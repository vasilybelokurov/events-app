"""Adapter for iCalendar (.ics) feeds.

A minimal RFC 5545 reader rather than a dependency: we only need VEVENT
components, and keeping the parser here means the build has no extra install
step.  Unsupported-by-design: RRULE expansion (see ``RRULE`` note below).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from ..careers import infer_careers, infer_work_styles
from ..http import fetch
from ..models import (UK, Event, make_id, parse_age_range, parse_uk_datetime,
                      price_info, shift)

KIND = "ics"


def fetch_raw(cfg: dict, **_) -> str:
    return fetch(cfg["url"], accept="text/calendar")


def unfold(text: str) -> list[str]:
    """Join RFC 5545 folded continuation lines."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(value: str) -> str:
    out = value.replace(chr(92)+"n", chr(10)).replace(chr(92)+"N", chr(10))
    for ch in (",", ";"):
        out = out.replace(chr(92)+ch, ch)
    return out.replace(chr(92)*2, chr(92))


def _parse_dt(value: str, params: dict[str, str]) -> tuple[str | None, bool]:
    """Return ``(iso_string, all_day)`` for a DTSTART/DTEND value."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        iso = parse_uk_datetime(datetime.strptime(value[:8], "%Y%m%d").date())
        return iso, True
    m = re.fullmatch(r"(\d{8})T(\d{6})(Z?)", value)
    if not m:
        return parse_uk_datetime(value), False
    dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    if m.group(3) == "Z":
        from datetime import timezone
        return dt.replace(tzinfo=timezone.utc).astimezone(UK).isoformat(), False
    tzid = params.get("TZID")
    if tzid:
        try:
            from zoneinfo import ZoneInfo
            return dt.replace(tzinfo=ZoneInfo(tzid)).astimezone(UK).isoformat(), False
        except Exception:
            pass
    return dt.replace(tzinfo=UK).isoformat(), False


def iter_vevents(text: str):
    """Yield ``{property: (value, params)}`` dicts, one per VEVENT."""
    current: dict | None = None
    for line in unfold(text):
        if line.strip() == "BEGIN:VEVENT":
            current = {}
            continue
        if line.strip() == "END:VEVENT":
            if current is not None:
                yield current
            current = None
            continue
        if current is None or ":" not in line:
            continue
        head, _, value = line.partition(":")
        name, *param_parts = head.split(";")
        params = {}
        for p in param_parts:
            k, _, v = p.partition("=")
            params[k.upper()] = v.strip('"')
        current[name.upper()] = (value, params)
    return


#: Placeholder descriptions some feeds emit; carrying them adds no information.
_EMPTY_DESCRIPTIONS = {"abstract not available", "no abstract", "tbc", "tba", "n/a"}


def _clean_description(text: str | None) -> str | None:
    """Strip markup from a DESCRIPTION and drop placeholder text.

    talks.cam sends HTML inside the iCalendar DESCRIPTION field (``<p>Abstract
    not available</p>``), which would otherwise be shown to the reader as-is.
    """
    if not text:
        return None
    plain = BeautifulSoup(text, "lxml").get_text(" ", strip=True)
    plain = re.sub(r"\s+", " ", plain).strip()
    if not plain or plain.lower().rstrip(".") in _EMPTY_DESCRIPTIONS:
        return None
    return plain


def parse(raw: str, cfg: dict) -> list[Event]:
    out: list[Event] = []
    for ve in iter_vevents(raw):
        def val(key: str) -> str | None:
            item = ve.get(key)
            return _unescape(item[0]).strip() if item else None

        title = val("SUMMARY")
        if not title:
            continue
        dtstart = ve.get("DTSTART")
        if not dtstart:
            continue
        start, all_day = _parse_dt(*dtstart)
        if not start:
            continue
        end = None
        if "DTEND" in ve:
            end, _ = _parse_dt(*ve["DTEND"])
        elif "DURATION" in ve:
            m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?",
                             val("DURATION") or "")
            if m:
                days, hours, mins = (int(g or 0) for g in m.groups())
                # shift() works in local terms, so a duration spanning a clock
                # change lands on the right wall-clock time.
                end = shift(start, days=days, hours=hours, minutes=mins)
        summary = _clean_description(val("DESCRIPTION"))
        status = (val("STATUS") or "CONFIRMED").upper()
        if status in ("CANCELLED", "CANCELED"):
            status = "cancelled"
        elif status == "TENTATIVE":
            status = "postponed"
        else:
            status = "scheduled"
        # RRULE: we intentionally surface only the first occurrence and mark the
        # event as recurring rather than fabricating dates we cannot verify.
        recurring = "RRULE" in ve
        price_text = None
        is_free, price_from, currency = price_info(price_text)
        age_min, age_max = parse_age_range(" ".join(filter(None, [title, summary])))
        out.append(Event(
            # Occurrence identity: a modified instance of a recurring series
            # shares its UID, so RECURRENCE-ID must be part of the key.
            id=make_id(cfg["key"], "|".join(filter(None, [
                val("UID") or title, val("RECURRENCE-ID") or start]))),
            title=title,
            url=val("URL") or cfg.get("site") or "",
            source=cfg["key"],
            source_name=cfg["name"],
            start=start,
            end=end,
            all_day=all_day,
            ongoing=recurring,
            status=status,
            summary=summary,
            venue_name=val("LOCATION") or cfg.get("venue_name"),
            city=cfg.get("city"),
            lat=cfg.get("lat"),
            lon=cfg.get("lon"),
            price_text=price_text,
            is_free=is_free,
            price_from=price_from,
            currency=currency,
            age_min=age_min,
            age_max=age_max,
            topics=list(cfg.get("topics", [])),
            careers=infer_careers(title, summary,
                                  " ".join(cfg.get("career_topics", []))),
            work_styles=infer_work_styles(title, summary),
            provenance="recurring series (RRULE); this is the series start, "
                       "not necessarily the next occurrence -- check the venue"
            if recurring else None,
        ))
    return out
