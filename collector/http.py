"""Polite HTTP fetching: robots.txt, per-host throttling, and an on-disk cache.

The robots check and the delay live here rather than in the adapters, so a new
adapter cannot accidentally bypass them.  Respecting robots.txt is not the same
as having permission to republish: see README "Sources and permissions".
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import urlsplit

import requests

LOG = logging.getLogger(__name__)

CONTACT = os.environ.get("EVENTS_CONTACT", "https://github.com/vasilybelokurov/events-app/issues")
UA = f"events_app/0.1 (personal event aggregator; contact: {CONTACT})"

CACHE_DIR = Path(os.environ.get("EVENTS_CACHE", ".cache"))
CACHE_TTL = int(os.environ.get("EVENTS_CACHE_TTL", 3600))
TIMEOUT = 30
#: Minimum seconds between requests to the same host.
MIN_INTERVAL = float(os.environ.get("EVENTS_MIN_INTERVAL", 1.0))
#: Set EVENTS_IGNORE_ROBOTS=1 only for a source you have written permission for.
IGNORE_ROBOTS = os.environ.get("EVENTS_IGNORE_ROBOTS") == "1"

_last_request: dict[str, float] = {}
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}


class RobotsDisallowed(RuntimeError):
    """Raised when robots.txt forbids the fetch."""


def _host(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _robots_for(url: str):
    host = _host(url)
    if host not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(host + "/robots.txt")
        try:
            rp.read()
        except Exception as exc:                       # pragma: no cover - network
            LOG.warning("robots.txt unreadable for %s (%s); assuming allowed", host, exc)
            rp = None
        _robots[host] = rp
    return _robots[host]


def allowed(url: str) -> bool:
    """Whether robots.txt permits our user agent to fetch ``url``."""
    if IGNORE_ROBOTS:
        return True
    rp = _robots_for(url)
    if rp is None:
        return True
    return rp.can_fetch(UA, url) or rp.can_fetch("*", url)


def _throttle(url: str) -> None:
    host = urlsplit(url).netloc
    last = _last_request.get(host)
    if last is not None:
        wait = MIN_INTERVAL - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
    _last_request[host] = time.monotonic()


def _cache_path(url: str, accept: str | None) -> Path:
    key = f"{url}|{accept or ''}"
    return CACHE_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".bin")


def fetch(url: str, *, ttl: int | None = None, accept: str | None = None,
          retries: int = 2) -> str:
    """GET ``url`` as text, using the disk cache when it is fresh.

    The cache key includes ``Accept``, because the same URL can legitimately
    return HTML or JSON depending on the header.
    """
    ttl = CACHE_TTL if ttl is None else ttl
    path = _cache_path(url, accept)
    if ttl > 0 and path.exists() and time.time() - path.stat().st_mtime < ttl:
        return path.read_text(encoding="utf-8", errors="replace")
    if not allowed(url):
        raise RobotsDisallowed(f"robots.txt disallows {url}")

    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        _throttle(url)
        try:
            LOG.info("GET %s (attempt %d)", url, attempt + 1)
            resp = requests.get(url, headers=headers, timeout=TIMEOUT)
            resp.raise_for_status()
            text = resp.text
            if ttl > 0:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            return text
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(2 ** attempt)
    raise last_exc  # type: ignore[misc]


def check_link(url: str) -> dict:
    """Probe a curated entry's URL.

    Returns ``{"status", "final_url", "redirected_to_root"}``.  A 200 proves
    only that a page loads: a venue that has retired a page often redirects to
    its home page, which is why the final URL is reported and a redirect to the
    site root is flagged rather than counted as healthy.
    """
    result: dict = {"status": None, "final_url": None, "redirected_to_root": False}
    try:
        r = requests.head(url, headers={"User-Agent": UA}, timeout=TIMEOUT,
                          allow_redirects=True)
        if r.status_code in (403, 405, 501):
            r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT,
                             allow_redirects=True, stream=True)
        result["status"] = r.status_code
        result["final_url"] = r.url
        if urlsplit(r.url).path.strip("/") == "" and urlsplit(url).path.strip("/"):
            result["redirected_to_root"] = True
    except requests.RequestException as exc:           # pragma: no cover - network
        LOG.warning("link check failed for %s: %s", url, exc)
    return result
