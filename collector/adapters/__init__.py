"""Source adapters.

Each adapter exposes:

``fetch_raw(cfg)``
    Pull the source document(s) over HTTP.  Network-only; not used in tests.
``parse(raw, cfg)``
    Pure function turning a raw document into ``list[Event]``.  Every adapter
    is tested through this entry point against a saved fixture.
"""

from __future__ import annotations

from . import curated, drupal_jsonapi, html_css, ics, jsonld, venue

REGISTRY = {
    "drupal_jsonapi": drupal_jsonapi,
    "jsonld": jsonld,
    "ics": ics,
    "html_css": html_css,
    "curated": curated,
    "venue": venue,
}


def get(kind: str):
    """Look up an adapter module by its registry key."""
    try:
        return REGISTRY[kind]
    except KeyError:
        raise KeyError(f"unknown adapter kind {kind!r}; have {sorted(REGISTRY)}") from None
