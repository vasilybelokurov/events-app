"""Adapter for Drupal sites that expose a public JSON:API.

Verified against the Royal Institution (www.rigb.org), whose ``node/event``
resource carries dates, times, prices, booking links and -- unusually useful
here -- a ``field_age`` taxonomy with values such as "Young people 13+".

Two things this adapter must get right:

* **Pagination.** JSON:API caps a page at 50 records; ``fetch_raw`` follows
  ``links.next`` so a busy season is not silently truncated.
* **Runs, not just start dates.** Filtering on ``field_dates.value >= today``
  alone would drop an exhibition that opened last month and runs until
  January, so the filter is an OR over the start and end dates.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..careers import infer_careers, infer_work_styles
from ..http import fetch
from ..models import (Event, combine_audience_values, make_id, parse_time_text,
                      parse_uk_datetime, price_info, shift)

KIND = "drupal_jsonapi"
MAX_PAGES = 20


def build_url(cfg: dict, *, since: str) -> str:
    """Compose the JSON:API query: anything still current, oldest first.

    The filter group is ``start >= since OR end >= since``.
    """
    base = cfg["endpoint"].rstrip("/")
    date_field = cfg.get("date_field", "field_dates")
    include = cfg.get("include", ["field_age", "field_topic", "field_event_type",
                                  "field_location"])
    params = [
        ("filter[current][group][conjunction]", "OR"),
        ("filter[starts][condition][path]", f"{date_field}.value"),
        ("filter[starts][condition][operator]", ">="),
        ("filter[starts][condition][value]", since),
        ("filter[starts][condition][memberOf]", "current"),
        ("filter[ends][condition][path]", f"{date_field}.end_value"),
        ("filter[ends][condition][operator]", ">="),
        ("filter[ends][condition][value]", since),
        ("filter[ends][condition][memberOf]", "current"),
        ("sort", f"{date_field}.value"),
        ("page[limit]", str(cfg.get("limit", 50))),
        ("include", ",".join(include)),
    ]
    query = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in params)
    return f"{base}?{query}"


def fetch_raw(cfg: dict, *, now: datetime | None = None) -> str:
    """Fetch every page of the collection and return one merged document.

    A truncated crawl is reported rather than hidden: the merged document
    carries ``meta.pagination_complete``, which :mod:`collector.build` records
    in the per-source health report.
    """
    now = now or datetime.now()
    url = build_url(cfg, since=now.date().isoformat())
    data: list[dict] = []
    included: dict[str, dict] = {}
    complete = True
    host = urlsplit(cfg["endpoint"])
    for page in range(MAX_PAGES):
        doc = json.loads(fetch(url, accept="application/vnd.api+json"))
        data.extend(doc.get("data", []))
        for item in doc.get("included", []):
            included[item["id"]] = item
        nxt = (doc.get("links") or {}).get("next")
        nxt = nxt.get("href") if isinstance(nxt, dict) else nxt
        if not nxt:
            break
        if urlsplit(nxt).netloc != host.netloc:      # never follow off-site
            complete = False
            break
        url = nxt
    else:
        complete = False
    return json.dumps({"data": data, "included": list(included.values()),
                       "meta": {"pagination_complete": complete}})


def _html_to_text(html: str | None, limit: int = 420) -> str | None:
    if not html:
        return None
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text or None


_CANCELLED = re.compile(r"\b(cancelled|canceled)\b", re.I)
_POSTPONED = re.compile(r"\b(postponed|rescheduled)\b", re.I)
_SOLD_OUT = re.compile(r"\bsold out\b", re.I)


def parse(raw: str | dict, cfg: dict) -> list[Event]:
    """Turn a JSON:API collection document into events."""
    doc = json.loads(raw) if isinstance(raw, str) else raw
    included = {i["id"]: i for i in doc.get("included", [])}
    date_field = cfg.get("date_field", "field_dates")
    site = cfg["site"]
    out: list[Event] = []

    for node in doc.get("data", []):
        attrs = node.get("attributes", {})
        rels = node.get("relationships", {})

        def terms(key: str) -> list[str]:
            data = rels.get(key, {}).get("data")
            if not data:
                return []
            items = data if isinstance(data, list) else [data]
            names = [included.get(i["id"], {}).get("attributes", {}).get("name")
                     for i in items]
            return [n for n in names if n]

        dates = attrs.get(date_field) or {}
        time_text = attrs.get("field_times")
        t_start, t_end = parse_time_text(time_text)
        # field_dates usually carries the real clock time; when it is midnight
        # the human-readable field_times is the only source of one.
        start = parse_uk_datetime(dates.get("value"))
        if start and t_start and start[11:16] == "00:00":
            start = parse_uk_datetime(f"{start[:10]}T{t_start.isoformat()}")
        if not start:
            continue
        end = parse_uk_datetime(dates.get("end_value"))
        if end is None and t_end:
            end = parse_uk_datetime(f"{start[:10]}T{t_end.isoformat()}")
            if end and end <= start:                 # runs past midnight
                end = shift(end, days=1)

        path = (attrs.get("path") or {}).get("alias") or ""
        url = urljoin(site, path) if path else site
        booking = (attrs.get("field_book_tickets") or {}).get("uri")
        price_text = attrs.get("field_prices")
        is_free, price_from, currency = price_info(price_text)

        # The age taxonomy lists the audiences an event suits, so they combine
        # as a union.  Concatenating them into one string and parsing that gave
        # "Children 12 and under, Families, Young people 13+" a minimum age of
        # 13, which is the opposite of what the venue means.
        ages = terms(cfg.get("age_field", "field_age"))
        age_text = ", ".join(ages) or None
        age_min, age_max, audiences = combine_audience_values(ages)

        topics = terms(cfg.get("topic_field", "field_topic"))
        etypes = terms(cfg.get("type_field", "field_event_type"))
        locations = terms(cfg.get("location_field", "field_location"))
        title = (attrs.get("title") or "").strip()
        summary = _html_to_text((attrs.get("body") or {}).get("value"))

        blob = " ".join(filter(None, [title, summary]))
        status = "scheduled"
        if _CANCELLED.search(blob):
            status = "cancelled"
        elif _POSTPONED.search(blob):
            # Materially different from cancelled: it is still going to happen.
            status = "postponed"
        elif _SOLD_OUT.search(blob):
            status = "sold_out"

        out.append(Event(
            id=make_id(cfg["key"], str(attrs.get("drupal_internal__nid") or url)),
            title=title,
            url=url,
            source=cfg["key"],
            source_name=cfg["name"],
            start=start,
            end=end,
            ongoing=bool(end and end[:10] != start[:10]),
            status=status,
            time_text=time_text,
            summary=summary,
            venue_name=cfg.get("venue_name"),
            city=cfg.get("city"),
            lat=cfg.get("lat"),
            lon=cfg.get("lon"),
            online=any("livestream" in l.lower() or "online" in l.lower()
                       for l in locations),
            price_text=price_text,
            is_free=is_free,
            price_from=price_from,
            currency=currency,
            booking_url=booking,
            booking="required" if booking else None,
            audiences=audiences,
            age_min=age_min,
            age_max=age_max,
            age_text=age_text,
            topics=sorted(set(topics + etypes)),
            careers=infer_careers(title, summary, " ".join(topics)),
            work_styles=infer_work_styles(title, summary),
        ))
    return out
