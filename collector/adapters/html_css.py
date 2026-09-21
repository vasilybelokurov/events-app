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

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..careers import infer_careers, infer_work_styles
from ..http import fetch
from ..models import (Event, combine_audience_values, make_id, map_audience,
                      parse_age_range, parse_date_range, parse_time_text,
                      parse_uk_datetime, price_info)

KIND = "html_css"

# Pages are concatenated into one document and split again in parse(), so a
# per-page claim check still sees one page at a time.
PAGE_BREAK = "<!--collector:page-break-->"

last_fetch: dict = {}


def _with_page(url: str, param: str, number: int) -> str:
    """Return *url* with the pagination parameter set to *number*."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k != param]
    query.append((param, str(number)))
    return urlunsplit(parts._replace(query=urlencode(query)))


def _row_keys(html: str, cfg: dict) -> set[str]:
    """Identify the rows on one page, so a repeated page can be recognised."""
    sel = cfg["selectors"]
    soup = BeautifulSoup(html, "lxml")
    keys = set()
    for node in soup.select(sel["item"]):
        href = _pick(node, sel.get("link") or sel.get("title"), "href")
        keys.add(href or _pick(node, sel.get("title")) or "")
    keys.discard("")
    return keys


def fetch_raw(cfg: dict, **_) -> str:
    """Fetch the listing, following its pagination when one is configured.

    Listings paginate in two unhelpful ways: some repeat a pinned row on every
    page, and some (the British Library among them) emit next-page hrefs that
    accumulate junk parameters.  So pages are requested by setting the
    parameter directly, and the walk stops when a page offers no row that an
    earlier page did not -- which covers both the natural end of the list and a
    site that silently serves page 1 for every number.

    A walk cut short by ``max_pages`` reports ``pagination_complete: False``,
    which :mod:`collector.build` treats as a degraded source rather than
    publishing a list it knows to be truncated.
    """
    global last_fetch
    last_fetch = {}
    html = fetch(cfg["url"]).replace(PAGE_BREAK, "")
    pages = cfg.get("pages") or {}
    if not pages:
        return html

    param = pages.get("param", "page")
    max_pages = int(pages.get("max_pages", 20))
    chunks = [html]
    seen = _row_keys(html, cfg)
    complete = False
    blanks = 0
    for number in range(2, max_pages + 1):
        page = fetch(_with_page(cfg["url"], param, number)).replace(PAGE_BREAK, "")
        keys = _row_keys(page, cfg)
        if not keys:
            # A page with no rows at all is *not* evidence that the listing
            # has ended: an interstitial, a rate-limit notice or an error page
            # served as HTTP 200 looks exactly the same.  Look past one, then
            # stop -- and do not claim the walk finished, because a source
            # frozen at its previous records is a loud failure while a
            # silently truncated list is not.
            blanks += 1
            if blanks > 1:
                break
            continue
        blanks = 0
        if not keys - seen:
            # Rows, but none that an earlier page did not have: the real end
            # of the listing, or a site that clamps every number to its last
            # page.  Either way there is nothing further to fetch.
            complete = True
            break
        seen |= keys
        chunks.append(page)

    # Falling out of the loop means the budget ran out with rows still
    # arriving.  Report that rather than spending another request on a probe
    # whose own failure would take the whole source down with it.
    last_fetch = {"pagination_complete": complete, "pages_fetched": len(chunks)}
    return PAGE_BREAK.join(chunks)


def _label_text(node, selector: str | None) -> list[str]:
    """Text of a labelled Drupal field, with the label itself removed.

    A field renders as ``<div class="field ..."><div class="field__label">Who
    </div><div class="field__item">12+</div>...</div>``, so the label word has
    to go or every age string starts with "Who".
    """
    if not selector:
        return []
    el = node.select_one(selector)
    if el is None:
        return []
    items = el.select(".field__item, .field-content li, li")
    if items:
        # Drupal renders multi-value fields with comma text nodes between the
        # items, which otherwise arrive as values of their own.
        return [t for t in (i.get_text(" ", strip=True) for i in items)
                if re.search(r"\w", t)]
    for label in el.select(".field__label"):
        label.extract()
    text = el.get_text("|", strip=True)
    return [part.strip() for part in text.split("|") if re.search(r"\w", part)]


def enrich(events: list[Event], cfg: dict) -> dict:
    """Fill gaps from each event's own page.

    The listing page is a summary; for ``cam_museums`` in particular it carries
    no age information at all, which left the age filter blind over the largest
    source.  The detail page has a "Who" field, a price and a longer
    description, so each event is fetched once (cached) and used to fill only
    the fields the listing left empty.

    Failures are per-event and non-fatal: a venue that 404s one page must not
    take out the other ninety-nine.  The counts are returned for the build's
    health report.
    """
    detail = cfg.get("detail") or {}
    selectors = detail.get("selectors") or {}
    if not detail.get("enabled") or not selectors:
        return {"detail_enabled": False}

    limit = int(detail.get("max_pages", 200))
    stats = {"detail_enabled": True, "detail_attempted": 0,
             "detail_enriched": 0, "detail_failed": 0, "detail_ages_added": 0}

    for event in events[:limit]:
        if not event.url:
            continue
        stats["detail_attempted"] += 1
        try:
            page = BeautifulSoup(fetch(event.url), "lxml")
        except Exception:                               # noqa: BLE001
            stats["detail_failed"] += 1
            continue

        changed = False
        who = _label_text(page, selectors.get("age"))
        if who and event.age_min is None and event.age_max is None:
            age_min, age_max, audiences = combine_audience_values(who)
            event.age_text = ", ".join(who)
            event.age_min, event.age_max = age_min, age_max
            event.audiences = sorted(set(event.audiences) | set(audiences))
            stats["detail_ages_added"] += 1
            changed = True

        if not event.price_text:
            price = _label_text(page, selectors.get("price"))
            if price:
                event.price_text = ", ".join(price)
                event.is_free, event.price_from, event.currency = \
                    price_info(event.price_text)
                changed = True

        topics = _label_text(page, selectors.get("topics"))
        if topics:
            merged = sorted(set(event.topics) | set(topics))
            if merged != event.topics:
                event.topics = merged
                changed = True

        if not event.summary:
            body = _label_text(page, selectors.get("summary"))
            if body:
                event.summary = " ".join(body)[:420]
                changed = True

        if not event.time_text:
            times = _label_text(page, selectors.get("time"))
            if times:
                event.time_text = ", ".join(times)
                t_start, t_end = parse_time_text(event.time_text)
                if t_start and event.start and event.start[11:16] == "00:00":
                    event.start = parse_uk_datetime(
                        f"{event.start[:10]}T{t_start.isoformat()}")
                    event.all_day = False
                changed = True

        if changed:
            stats["detail_enriched"] += 1
    return stats


def _pick(node, selector, attr: str | None = None) -> str | None:
    """First non-empty value among *selector*, which may be a list.

    A list is tried in order, which is how a listing that marks some rows up
    properly and leaves the rest as prose is read: the British Library gives
    an exhibition run a ``<time itemprop="startDate" datetime="...">`` but
    writes a single-session event as "Tuesday 20 October 10.30".  Machine
    markup first, prose as the fallback.
    """
    if not selector:
        return None
    for one in ([selector] if isinstance(selector, str) else selector):
        el = node.select_one(one)
        if el is None:
            continue
        if attr:
            value = (el.get(attr) or "").strip()
            if value:
                return value
            continue
        text = el.get_text(" ", strip=True)
        if text:
            return text
    return None


#: Filled by :func:`parse` so the build can report enrichment health.
last_enrichment: dict = {}


def _page_claims(soup: BeautifulSoup, cfg: dict) -> tuple[dict, list[str]]:
    """Source-level claims that the listing page must still support.

    The V&A's young people programme states "For young creatives aged 13 - 26"
    on the listing itself, which is worth applying to every event on it -- but
    only while the page still says so.  Same discipline as the venue adapter:
    find the phrase, apply the claim; miss it, and drop it and record that.
    """
    rules = cfg.get("page_confirm") or []
    if not rules:
        return ({}, [])
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    claims, missing = {}, []
    for rule in rules:
        if rule["phrase"].lower() in text.lower():
            claims.update(rule["sets"])
        else:
            missing.append(rule["phrase"])
    return (claims, missing)


def _parse_page(raw: str, cfg: dict) -> list[Event]:
    sel = cfg["selectors"]
    soup = BeautifulSoup(raw, "lxml")
    page_claims, page_missing = _page_claims(soup, cfg)
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
        # Venues write a run as one string ("10th September-31st October
        # 2026") with the year only on the right, so a range is tried first:
        # parsing the left date alone would put a live exhibition in the past.
        start, end = parse_date_range(
            date_text, default_time=t_start,
            assume_future=bool(cfg.get("assume_future_dates")))
        if not start:
            continue
        explicit_end = _pick(node, sel.get("end"), sel.get("end_attr")) \
            or _pick(node, sel.get("end"))
        if explicit_end:
            end = parse_uk_datetime(explicit_end) or end
        if end is None and t_end:
            end = parse_uk_datetime(f"{start[:10]}T{t_end.isoformat()}")
        # A run of more than one day is an exhibition, not a timed event.
        ongoing = bool(end and end[:10] != start[:10])

        status = "scheduled"
        status_text = (_pick(node, sel.get("status")) or "").lower()
        if "cancel" in status_text:
            status = "cancelled"
        elif "sold out" in status_text:
            status = "sold_out"
        elif "postponed" in status_text:
            status = "postponed"

        href = _pick(node, sel.get("link") or sel.get("title"), "href")
        url = urljoin(base, href) if href else base
        summary = _pick(node, sel.get("summary"))
        price_text = _pick(node, sel.get("price"))
        is_free, price_from, currency = price_info(price_text)
        age_text = _pick(node, sel.get("age")) or page_claims.get("age_text")
        age_min, age_max = parse_age_range(
            " ".join(filter(None, [age_text, title, summary])))
        if page_claims.get("age_min") is not None:
            age_min = page_claims["age_min"]
        if page_claims.get("age_max") is not None:
            age_max = page_claims["age_max"]
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
            status=status,
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
            audiences=list(page_claims.get("audiences", [])),
            provenance=("the listing page no longer says "
                        + "; ".join(f"\u201c{p}\u201d" for p in page_missing)
                        + ", so that claim was dropped") if page_missing else None,
            topics=list(cfg.get("topics", [])),
            careers=infer_careers(title, summary, " ".join(cfg.get("topics", []))),
            work_styles=infer_work_styles(title, summary),
        ))

    return out


def parse(raw: str, cfg: dict) -> list[Event]:
    """Parse every fetched page, keeping the first record of each event id.

    A paginated listing repeats its pinned rows on every page, so the same
    event arrives several times; ids are derived from the event's own URL, so
    the repeats collapse here rather than being published as duplicates.
    """
    out: list[Event] = []
    seen: set[str] = set()
    for chunk in raw.split(PAGE_BREAK):
        for event in _parse_page(chunk, cfg):
            if event.id in seen:
                continue
            seen.add(event.id)
            out.append(event)

    global last_enrichment
    last_enrichment = enrich(out, cfg)
    for event in out:
        # Career tags are re-derived after enrichment: the detail page often
        # supplies the topic words the listing row omitted.
        event.careers = infer_careers(event.title, event.summary,
                                      " ".join(event.topics))
        # Deliberately not the source's topics: a publisher bucket such as
        # "Workshops and talks" would tag a lunchtime lecture as design and
        # making, which is an artefact of their grouping, not evidence about
        # what an attendee would be doing.
        event.work_styles = infer_work_styles(event.title, event.summary)
    return out
