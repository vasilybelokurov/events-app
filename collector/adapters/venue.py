"""Adapter for a venue rather than an event feed.

Some of the most useful things for this project are not events and never will
be: the Old Bailey public gallery, a museum open on weekdays, a weekly tour.
They have no listing to scrape, which is why they used to be hand-typed — and a
hand-typed claim rots silently.

This adapter replaces that. The venue is a fixed entry in ``sources.yaml``; the
collector **visits its page on every run** and builds the record out of what the
page says today:

* the name comes from ``h1``/``og:title``, the description from
  ``og:description``, so a venue that renames or repurposes a page shows up;
* every additional claim (an age rule, a booking requirement) is declared with
  the **phrase that must still appear on the page**.  Find the phrase, apply the
  claim; miss it, and the claim is dropped and the omission recorded.  A claim
  can therefore never outlive the sentence it came from.

That is a different and stronger guarantee than a link check: a 200 only proves
a page loads, whereas a confirmed phrase proves the specific fact is still
published.
"""

from __future__ import annotations

import re
from datetime import datetime

from bs4 import BeautifulSoup

from ..careers import infer_careers, infer_work_styles
from ..http import fetch
from ..models import (UK, Event, make_id, map_audience, parse_age_range,
                      parse_uk_datetime, price_info)

KIND = "venue"

#: Outcome of the most recent :func:`fetch_raw`, read by :mod:`collector.build`
#: so an unreachable venue is never recorded as a verified success.  Reset on
#: every fetch; collection is sequential, so the build reads it for the source
#: it has just collected.
last_fetch: dict = {}

#: Fields a `confirm` rule is allowed to set.  Anything else is a config error,
#: caught by the tests rather than silently ignored.
SETTABLE = frozenset({
    "age_min", "age_max", "age_text", "price_text", "booking", "booking_url",
    "accompanied", "when_text", "summary", "audiences", "topics",
})


class VenueConfigError(ValueError):
    """Raised for a `confirm` rule that sets a field it may not."""


def fetch_raw(cfg: dict, **_) -> str:
    """Fetch the venue page, or return an empty document if it refuses us.

    Several venues (the Science Museum among them) serve 403 to anything that
    is not a desktop browser.  With ``allow_blocked: true`` the venue stays in
    the list and the record says plainly that nothing was confirmed today,
    which is better than dropping the venue or pretending it was checked.
    """
    global last_fetch
    last_fetch = {"reachable": True, "reason": None}
    try:
        return fetch(cfg["homepage"])
    except Exception as exc:                            # noqa: BLE001
        if cfg.get("allow_blocked"):
            reason = f"{type(exc).__name__}: {exc}"
            last_fetch = {"reachable": False, "reason": reason}
            return f"<!-- unfetchable: {reason} -->"
        raise


def _meta(soup: BeautifulSoup, prop: str) -> str | None:
    tag = (soup.find("meta", property=prop)
           or soup.find("meta", attrs={"name": prop}))
    if not tag:
        return None
    return (tag.get("content") or "").strip() or None


def _select_text(soup: BeautifulSoup, selector: str | None) -> str | None:
    if not selector:
        return None
    if selector.startswith("og:") or selector.startswith("twitter:"):
        return _meta(soup, selector)
    el = soup.select_one(selector)
    if el is None:
        return None
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) or None


def page_text(soup: BeautifulSoup) -> str:
    """Visible text of the page, normalised for phrase matching."""
    for tag in soup(["script", "style", "noscript"]):
        tag.extract()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def parse(raw: str, cfg: dict) -> list[Event]:
    """Build the venue's single standing record from the page as fetched."""
    for rule in cfg.get("confirm", []):
        bad = set(rule.get("sets", {})) - SETTABLE
        if bad:
            raise VenueConfigError(
                f"{cfg['key']}: confirm rule may not set {sorted(bad)}")

    soup = BeautifulSoup(raw, "lxml")
    text = page_text(soup)
    unfetchable = re.search(r"unfetchable: (.+?) -->", raw)
    extract = cfg.get("extract") or {}

    # The registry names the venue, not the page.  An h1 of "Tours", "Museum"
    # or "What's on" identifies nothing to a reader, and two venues whose pages
    # were both titled "What's on" were being de-duplicated into one another.
    # The page's own title is still read, and reported when it disagrees.
    title = cfg.get("title") or cfg["name"]
    page_title = (_select_text(soup, extract.get("title"))
                  or _meta(soup, "og:title")
                  or (soup.title.get_text(strip=True) if soup.title else None))

    summary = (_select_text(soup, extract.get("summary"))
               or _meta(soup, "og:description")
               or cfg.get("summary"))

    record: dict = {
        "when_text": cfg.get("schedule"),
        "summary": summary,
        "topics": list(cfg.get("topics", [])),
        "audiences": [],
    }

    # `expect` phrases guard against a page that has been repurposed: if the
    # thing we came for is no longer named on it, nothing here is confirmed.
    expected_missing = [phrase for phrase in cfg.get("expect", [])
                        if phrase.lower() not in text.lower()]

    confirmed: list[str] = []
    unconfirmed: list[str] = []
    for rule in cfg.get("confirm", []):
        phrase = rule["phrase"]
        if not unfetchable and phrase.lower() in text.lower():
            confirmed.append(phrase)
            for field, value in rule["sets"].items():
                if field in ("topics", "audiences"):
                    record[field] = sorted(set(record.get(field) or []) | set(value))
                else:
                    record[field] = value
        else:
            unconfirmed.append(phrase)

    age_min = record.get("age_min")
    age_max = record.get("age_max")
    if age_min is None and age_max is None and record.get("age_text"):
        age_min, age_max = parse_age_range(record["age_text"])
    audiences = sorted({a for a in (map_audience(x)
                                    for x in record.get("audiences", [])) if a})

    is_free, price_from, currency = price_info(record.get("price_text"))

    today = datetime.now(UK).date().isoformat()
    provenance_bits = [
        f"Built from {cfg['homepage']} as fetched on {today}."
    ]
    if page_title and page_title.lower() not in (title.lower(), "") :
        provenance_bits.append(f"The page calls itself \u201c{page_title}\u201d.")
    if expected_missing:
        provenance_bits.append(
            "The page no longer mentions "
            + "; ".join(f"\u201c{p}\u201d" for p in expected_missing)
            + ", so it may have been repurposed \u2014 check the link.")
    if confirmed:
        provenance_bits.append(
            "Confirmed on the page: " + "; ".join(f"“{p}”" for p in confirmed) + ".")
    if unconfirmed:
        provenance_bits.append(
            "NOT found on the page, so not claimed here: "
            + "; ".join(f"“{p}”" for p in unconfirmed) + ".")
    if unfetchable:
        provenance_bits.append(
            f"The venue refused automated access ({unfetchable.group(1)}), so "
            "nothing was confirmed today — open the link and check.")

    return [Event(
        id=make_id(cfg["key"], cfg["homepage"]),
        title=title,
        url=cfg["homepage"],
        source=cfg["key"],
        source_name=cfg["name"],
        # A standing offer has no date; `anytime` keeps it from expiring and
        # `when_text` carries the venue's own words about when to go.
        start=parse_uk_datetime(datetime.now(UK).date()),
        anytime=True,
        all_day=True,
        when_text=record.get("when_text"),
        summary=record.get("summary"),
        venue_name=cfg.get("venue_name") or cfg["name"],
        city=cfg.get("city"),
        lat=cfg.get("lat"),
        lon=cfg.get("lon"),
        price_text=record.get("price_text"),
        is_free=is_free,
        price_from=price_from,
        currency=currency,
        booking=record.get("booking"),
        booking_url=record.get("booking_url"),
        accompanied=record.get("accompanied"),
        audiences=audiences,
        age_min=age_min,
        age_max=age_max,
        age_text=record.get("age_text"),
        topics=sorted(set(record.get("topics", []))),
        careers=infer_careers(title, record.get("summary"),
                              " ".join(record.get("topics", [])),
                              record.get("age_text")),
        work_styles=infer_work_styles(title, record.get("summary")),
        # The page was read today, so the record is machine-verified as of
        # today -- except when we could not read it at all.
        verified_on=None if (unfetchable or expected_missing) else today,
        provenance=" ".join(provenance_bits),
    )]
