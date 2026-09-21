"""Declarative HTML adapter: a venue is added by writing CSS selectors, not code.

Aimed at listing pages that publish no feed and no structured data — for
example Drupal "views" listings such as ``museums.cam.ac.uk/whats-on``.  The
config in ``sources.yaml`` looks like::

    kind: html_css
    url: https://www.museums.cam.ac.uk/whats-on
    selectors:
      item: .views-row
      title: h3 a
      link: h3 a
      date: .views-field-field-event-date time
      date_attr: datetime
      time: .views-field-field-event-time
      end: .views-field-field-end-date time
      end_attr: datetime
      summary: .field-content p
      venue: .views-field-field-venue
      price: .views-field-field-free

Because the site owns the HTML, this adapter is the fragile one: a redesign
makes it return nothing.  ``build.py`` therefore records a per-source count and
the build fails loudly when a source that previously worked returns zero.
"""

from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..careers import infer_careers
from ..http import fetch
from ..models import (Event, make_id, parse_age_range, parse_time_text,
                      parse_uk_datetime, price_info)

KIND = "html_css"


def fetch_raw(cfg: dict, **_) -> str:
    return fetch(cfg["url"])


def _pick(node, selector: str | None, attr: str | None = None) -> str | None:
    if not selector:
        return None
    el = node.select_one(selector)
    if el is None:
        return None
    if attr:
        return (el.get(attr) or "").strip() or None
    return el.get_text(" ", strip=True) or None


def parse(raw: str, cfg: dict) -> list[Event]:
    sel = cfg["selectors"]
    soup = BeautifulSoup(raw, "lxml")
    base = cfg.get("site") or cfg["url"]
    out: list[Event] = []
    for node in soup.select(sel["item"]):
        title = _pick(node, sel.get("title"))
        if not title:
            continue
        date_text = _pick(node, sel.get("date"), sel.get("date_attr")) \
            or _pick(node, sel.get("date"))
        time_text = _pick(node, sel.get("time"))
        t_start, t_end = parse_time_text(time_text)
        start = parse_uk_datetime(date_text, default_time=t_start)
        if not start:
            continue
        end_text = _pick(node, sel.get("end"), sel.get("end_attr")) \
            or _pick(node, sel.get("end"))
        end = parse_uk_datetime(end_text) if end_text else None
        if end is None and t_end:
            end = parse_uk_datetime(f"{start[:10]} {t_end.isoformat()}")
        # A run of more than one day is an exhibition, not a timed event.
        ongoing = bool(end and end[:10] != start[:10])
        href = _pick(node, sel.get("link") or sel.get("title"), "href")
        url = urljoin(base, href) if href else base
        summary = _pick(node, sel.get("summary"))
        price_text = _pick(node, sel.get("price"))
        is_free, price_from, currency = price_info(price_text)
        age_text = _pick(node, sel.get("age"))
        age_min, age_max = parse_age_range(" ".join(filter(None, [age_text, title, summary])))
        out.append(Event(
            id=make_id(cfg["key"], url if href else f"{title}|{start}"),
            title=title,
            url=url,
            source=cfg["key"],
            source_name=cfg["name"],
            start=start,
            end=end,
            all_day=t_start is None,
            ongoing=ongoing,
            time_text=time_text,
            summary=summary,
            venue_name=_pick(node, sel.get("venue")) or cfg.get("venue_name"),
            city=cfg.get("city"),
            lat=cfg.get("lat"),
            lon=cfg.get("lon"),
            price_text=price_text,
            is_free=is_free,
            price_from=price_from,
            currency=currency,
            age_text=age_text,
            age_min=age_min,
            age_max=age_max,
            topics=list(cfg.get("topics", [])),
            careers=infer_careers(title, summary, " ".join(cfg.get("topics", []))),
        ))
    return out
