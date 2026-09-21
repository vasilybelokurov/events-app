"""Adapter for pages carrying schema.org Event data in ``application/ld+json``.

This is the closest thing to a standard for event publishing, so it is the
first thing to try on any new venue.  It handles ``@graph`` wrappers, lists of
objects and nested ``subEvent`` arrays.
"""

from __future__ import annotations

import json
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..careers import infer_careers, infer_work_styles
from ..http import fetch
from ..models import (Event, make_id, parse_age_range, parse_uk_datetime,
                      price_info)

KIND = "jsonld"

EVENT_TYPES = {
    "event", "exhibitionevent", "educationevent", "socialevent", "festival",
    "screeningevent", "theaterevent", "musicevent", "courseinstance",
    "businessevent", "childrensevent", "visualartsevent",
}


def fetch_raw(cfg: dict, **_) -> str:
    return fetch(cfg["url"])


def _types(obj: dict) -> set[str]:
    t = obj.get("@type") or obj.get("type") or []
    if isinstance(t, str):
        t = [t]
    return {str(x).lower() for x in t}


def iter_event_objects(blob):
    """Walk arbitrary JSON-LD and yield objects whose @type is an event."""
    if isinstance(blob, list):
        for item in blob:
            yield from iter_event_objects(item)
    elif isinstance(blob, dict):
        if "@graph" in blob:
            yield from iter_event_objects(blob["@graph"])
        if _types(blob) & EVENT_TYPES:
            yield blob
        for key in ("subEvent", "subEvents", "event", "events", "itemListElement"):
            if key in blob:
                yield from iter_event_objects(blob[key])
        if "item" in blob:
            yield from iter_event_objects(blob["item"])


def extract_blocks(html: str) -> list:
    soup = BeautifulSoup(html, "lxml")
    blocks = []
    for tag in soup.find_all("script", type="application/ld+json"):
        text = tag.string or tag.get_text()
        if not text:
            continue
        try:
            blocks.append(json.loads(text))
        except json.JSONDecodeError:
            continue
    return blocks


def _text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("@value"))
    if isinstance(value, list):
        parts = [_text(v) for v in value]
        return ", ".join(p for p in parts if p) or None
    return str(value).strip() or None


_SYMBOL = {"GBP": "\u00a3", "USD": "$", "EUR": "\u20ac"}


def _offers(obj) -> tuple[str | None, bool | None, float | None, str | None, str | None]:
    offers = obj.get("offers")
    if not offers:
        return (None, None, None, None, None)
    if isinstance(offers, dict):
        offers = [offers]
    prices, urls, currencies = [], [], []
    for o in offers:
        if not isinstance(o, dict):
            continue
        for key in ("price", "lowPrice"):
            v = o.get(key)
            if v is None:
                continue
            try:
                prices.append(float(str(v).replace(",", "").strip("\u00a3$\u20ac ")))
            except ValueError:
                continue
            cur = o.get("priceCurrency") or o.get("currency")
            if cur:
                currencies.append(str(cur).upper())
        if o.get("url"):
            urls.append(str(o["url"]))
    booking = urls[0] if urls else None
    if not prices:
        return (None, None, None, None, booking)
    currency = currencies[0] if currencies else None
    cheapest, dearest = min(prices), max(prices)
    sym = _SYMBOL.get(currency or "", "")
    if dearest == 0:
        text = "Free"
    elif sym:
        text = f"from {sym}{cheapest:g}"
    else:
        text = f"from {cheapest:g} {currency}" if currency else f"from {cheapest:g}"
    return (text, dearest == 0, cheapest, currency, booking)


def parse(raw: str, cfg: dict) -> list[Event]:
    out: list[Event] = []
    seen: set[str] = set()
    for block in extract_blocks(raw):
        for obj in iter_event_objects(block):
            title = _text(obj.get("name"))
            start = parse_uk_datetime(obj.get("startDate"))
            if not title or not start:
                continue
            url = _text(obj.get("url")) or cfg.get("url", "")
            if url:
                url = urljoin(cfg.get("site") or cfg.get("url", ""), url)
            key = f"{title}|{start}"
            if key in seen:
                continue
            seen.add(key)

            loc = obj.get("location") or {}
            if isinstance(loc, list):
                loc = loc[0] if loc else {}
            addr = loc.get("address") if isinstance(loc, dict) else None
            city = None
            if isinstance(addr, dict):
                city = _text(addr.get("addressLocality"))
            geo = loc.get("geo") if isinstance(loc, dict) else None
            lat = lon = None
            if isinstance(geo, dict):
                try:
                    lat = float(geo.get("latitude"))
                    lon = float(geo.get("longitude"))
                except (TypeError, ValueError):
                    lat = lon = None

            price_text, is_free, price_from, currency, booking = _offers(obj)
            age_text = _text(obj.get("typicalAgeRange"))
            age_min, age_max = parse_age_range(age_text)
            summary = _text(obj.get("description"))
            end = parse_uk_datetime(obj.get("endDate"))
            all_day = len(str(obj.get("startDate", ""))) <= 10
            out.append(Event(
                id=make_id(cfg["key"], obj.get("@id") or key),
                title=title,
                url=url,
                source=cfg["key"],
                source_name=cfg["name"],
                start=start,
                end=end,
                all_day=all_day,
                ongoing=bool(end and end[:10] != start[:10]),
                summary=summary,
                venue_name=_text(loc.get("name")) if isinstance(loc, dict) else cfg.get("venue_name"),
                city=city or cfg.get("city"),
                lat=lat if lat is not None else cfg.get("lat"),
                lon=lon if lon is not None else cfg.get("lon"),
                online="online" in str(obj.get("eventAttendanceMode", "")).lower(),
                price_text=price_text,
                is_free=is_free,
                price_from=price_from,
                currency=currency,
                booking_url=booking,
                booking="required" if booking else None,
                status={"eventcancelled": "cancelled",
                        "eventpostponed": "postponed",
                        "eventrescheduled": "postponed"}.get(
                            str(obj.get("eventStatus", "")).rsplit("/", 1)[-1].lower(),
                            "scheduled"),
                age_text=age_text,
                age_min=age_min,
                age_max=age_max,
                topics=list(cfg.get("topics", [])),
                careers=infer_careers(title, summary, " ".join(cfg.get("topics", []))),
                work_styles=infer_work_styles(title, summary, " ".join(cfg.get("topics", []))),
            ))
    return out
