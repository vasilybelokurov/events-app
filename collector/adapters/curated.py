"""Adapter for hand-written YAML entries.

Some of the most useful things for this use case are not "events" with a feed
at all: the Old Bailey public gallery, the Bank of England Museum, the Supreme
Court's Friday tours, a six-month exhibition.  They are recorded by hand, with
``provenance`` naming where the claim came from, and ``build.py`` HTTP-checks
every URL so an entry that cannot be verified is flagged in the UI instead of
being quietly trusted.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..careers import infer_careers, infer_work_styles
from ..models import (Event, make_id, map_audience, parse_age_range,
                      parse_time_text, parse_uk_datetime, price_info)

KIND = "curated"


def fetch_raw(cfg: dict, **_) -> str:
    return Path(cfg["path"]).read_text(encoding="utf-8")


def parse(raw: str, cfg: dict) -> list[Event]:
    doc = yaml.safe_load(raw) or {}
    out: list[Event] = []
    for item in doc.get("events", []):
        title = (item.get("title") or "").strip()
        if not title:
            continue
        time_text = item.get("time_text")
        t_start, t_end = parse_time_text(time_text)
        start = parse_uk_datetime(item.get("start"), default_time=t_start)
        end = parse_uk_datetime(item.get("end"), default_time=t_end)
        age_text = item.get("age_text")
        age_min, age_max = parse_age_range(age_text)
        if item.get("age_min") is not None:
            age_min = item["age_min"]
        if item.get("age_max") is not None:
            age_max = item["age_max"]
        price_text = item.get("price_text")
        is_free, price_from, currency = price_info(price_text)
        summary = item.get("summary")
        audiences = sorted({a for a in (map_audience(x)
                                        for x in item.get("audiences", [])) if a})
        careers = item.get("careers") or infer_careers(
            title, summary, " ".join(item.get("topics", [])))
        work_styles = item.get("work_styles") or infer_work_styles(
            title, summary, " ".join(item.get("topics", [])))
        out.append(Event(
            id=make_id(cfg["key"], item.get("id") or item.get("url") or title),
            title=title,
            url=item.get("url") or "",
            source=cfg["key"],
            source_name=cfg["name"],
            start=start,
            end=end,
            all_day=bool(item.get("all_day", not time_text)),
            time_text=time_text,
            ongoing=bool(item.get("ongoing")),
            anytime=bool(item.get("anytime")),
            when_text=item.get("when_text"),
            # The date a human last confirmed this claim against the venue's
            # own page.  Absent means "never verified", which the page shows.
            verified_on=(str(item["verified_on"])
                         if item.get("verified_on") else None),
            summary=summary,
            venue_name=item.get("venue_name"),
            city=item.get("city"),
            lat=item.get("lat"),
            lon=item.get("lon"),
            online=bool(item.get("online")),
            price_text=price_text,
            is_free=is_free if is_free is not None else item.get("is_free"),
            price_from=price_from,
            currency=currency,
            booking_url=item.get("booking_url"),
            audiences=audiences,
            age_min=age_min,
            age_max=age_max,
            age_text=age_text,
            topics=list(item.get("topics", [])),
            careers=careers,
            work_styles=work_styles,
            provenance=item.get("provenance"),
        ))
    return out
