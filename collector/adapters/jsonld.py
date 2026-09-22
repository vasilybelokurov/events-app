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
from ..models import (Event, combine_audience_values, make_id, parse_age_range,
                      parse_uk_datetime, price_info)

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


def iter_event_objects(blob, _depth: int = 0):
    """Walk arbitrary JSON and yield every object whose ``@type`` is an event.

    The walk is general rather than following named keys such as ``@graph``.
    That matters for JSON islands: Wellcome Collection's 75 events sit at
    ``props.pageProps...`` inside ``__NEXT_DATA__``, which a key-directed walk
    never reaches.  Yielding only event-typed objects keeps a broad walk safe,
    and the depth limit keeps a pathological document from recursing forever.
    """
    if _depth > 30:
        return
    if isinstance(blob, list):
        for item in blob:
            yield from iter_event_objects(item, _depth + 1)
    elif isinstance(blob, dict):
        if _types(blob) & EVENT_TYPES:
            yield blob
        for value in blob.values():
            if isinstance(value, (dict, list)):
                yield from iter_event_objects(value, _depth + 1)


#: Script types worth searching.  `application/ld+json` is the standard place,
#: but a Next.js site puts the same schema.org objects inside its
#: `__NEXT_DATA__` island instead -- Wellcome Collection publishes dozens of
#: Event objects that way and none in an ld+json block.
BLOCK_TYPES = ("application/ld+json", "application/json")


def extract_blocks(html: str) -> list:
    """Every JSON document embedded in (or constituting) the response.

    A plain JSON API response is treated as a single block, so the same
    adapter reads a schema.org page, a CMS JSON island and a content API
    without three separate implementations.
    """
    text = html.lstrip()
    if text[:1] in "[{":
        try:
            return [json.loads(text)]
        except json.JSONDecodeError:
            pass
    soup = BeautifulSoup(html, "lxml")
    blocks = []
    for script_type in BLOCK_TYPES:
        for tag in soup.find_all("script", type=script_type):
            text = tag.string or tag.get_text()
            if not text or '"Event"' not in text and script_type != "application/ld+json":
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


#: Keys whose entries carry a human label worth keeping as a topic.  Standard
#: schema.org objects have none of these, so this is a no-op for them; a JSON
#: island from a CMS often does (Wellcome's "Gallery tour", "Workshop",
#: "Performance"), and those labels say more about the event than its title.
_LABEL_KEYS = ("format", "series", "interpretations", "eventFormat")


def _labels(value) -> list[str]:
    """Pull ``label``/``title``/``name`` strings out of a field of any shape."""
    items = value if isinstance(value, list) else [value]
    out = []
    for item in items:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            text = item.get("label") or item.get("title") or item.get("name")
            if isinstance(text, str):
                out.append(text)
    return [t.strip() for t in out if t and t.strip()]


def _times(obj) -> tuple[str | None, str | None]:
    """Start/end from a ``times`` array, used when there is no ``startDate``.

    A CMS island commonly models repeat sittings as ``times: [{startDateTime,
    endDateTime}, ...]``; the earliest is the one to show.
    """
    times = obj.get("times")
    if not isinstance(times, list) or not times:
        return (None, None)
    parsed = []
    for t in times:
        if not isinstance(t, dict):
            continue
        start = parse_uk_datetime(t.get("startDateTime") or t.get("start"))
        if start:
            parsed.append((start, parse_uk_datetime(t.get("endDateTime") or t.get("end"))))
    if not parsed:
        return (None, None)
    return min(parsed, key=lambda pair: pair[0])


def _templated_url(obj, cfg: dict) -> str | None:
    """Build a per-event URL from the object's own identifiers.

    Some islands carry no ``url`` at all, which would leave every card pointing
    at the listing page -- and the link is the authority, so that matters.
    """
    template = cfg.get("url_template")
    if not template:
        return None
    fields = {k: v for k, v in obj.items() if isinstance(v, (str, int))}
    try:
        return template.format(**fields)
    except (KeyError, IndexError):
        return None


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
            title = _text(obj.get("name")) or _text(obj.get("title"))
            start = parse_uk_datetime(obj.get("startDate"))
            time_end = None
            if not start:
                start, time_end = _times(obj)
            if not title or not start:
                continue
            url = _text(obj.get("url")) or _templated_url(obj, cfg)
            if not url and cfg.get("require_url"):
                # Better to drop a record than to give every card the same
                # link to the listing page.
                continue
            url = url or cfg.get("url", "")
            if url:
                url = urljoin(cfg.get("site") or cfg.get("url", ""), url)
            identity = _text(obj.get("@id")) or _text(obj.get("id")) \
                or f"{title}|{start}"
            if identity in seen:
                continue
            seen.add(identity)

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
            audience_labels = _labels(obj.get("audiences") or obj.get("audience"))
            age_text = _text(obj.get("typicalAgeRange")) or (
                ", ".join(audience_labels) or None)
            if audience_labels:
                age_min, age_max, audiences = combine_audience_values(audience_labels)
            else:
                age_min, age_max = parse_age_range(age_text)
                audiences = []
            # The source-wide list is shown on the page but must not reach the
            # classifier: it is what the venue programmes overall, and feeding
            # the union of it to every event tagged a woodworking class
            # "Computing & AI".  `career_topics` is the narrower, per-source
            # list of things true of *every* event.  Same rule as html_css.
            topics = list(cfg.get("topics", []))
            own_topics: list[str] = []
            for label_key in _LABEL_KEYS:      # not `key`: that is the id key
                own_topics.extend(_labels(obj.get(label_key)))
            topics.extend(own_topics)
            career_text = " ".join(own_topics + list(cfg.get("career_topics", [])))
            summary = _text(obj.get("description"))
            end = parse_uk_datetime(obj.get("endDate")) or time_end
            all_day = len(str(obj.get("startDate", ""))) <= 10
            out.append(Event(
                id=make_id(cfg["key"], identity),
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
                audiences=audiences,
                topics=sorted(set(topics)),
                careers=infer_careers(title, summary, career_text),
                work_styles=infer_work_styles(title, summary),
            ))
    return out
