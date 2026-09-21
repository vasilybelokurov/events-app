"""Tests for the fetching layer, mostly about robots.txt.

The bug these exist for: `RobotFileParser.read()` fetches robots.txt with
urllib's own user agent, several venues' WAFs answer that with 403, and
robotparser treats a 403 on robots.txt as "disallow everything".  Three venues
whose robots.txt does not restrict this project at all were silently dropped
from the build.  Politeness machinery that over-blocks is a correctness bug,
not a safe default.
"""

from __future__ import annotations

import pytest
import requests

from collector import http as http_mod


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text
        self.url = "https://example.org/robots.txt"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


@pytest.fixture(autouse=True)
def clear_caches():
    http_mod._robots.clear()
    http_mod._last_request.clear()
    yield
    http_mod._robots.clear()
    http_mod._last_request.clear()


def serve(monkeypatch, status, text=""):
    monkeypatch.setattr(http_mod.requests, "get",
                        lambda url, **kw: FakeResponse(status, text))


REAL_WORLD = """# robots.txt
User-agent: AhrefsBot
Disallow: /

User-agent: GPTBot
Disallow: /

User-agent: *
Disallow: /search
Disallow: /cpresources/
"""


class TestRobots:
    def test_a_403_on_robots_txt_does_not_block_everything(self, monkeypatch):
        """RFC 9309 s2.3.1: a 4xx means no robots file applies."""
        serve(monkeypatch, 403)
        assert http_mod.allowed("https://example.org/whats-on") is True

    def test_a_404_on_robots_txt_allows_crawling(self, monkeypatch):
        serve(monkeypatch, 404)
        assert http_mod.allowed("https://example.org/whats-on") is True

    def test_a_500_on_robots_txt_is_treated_as_disallow_all(self, monkeypatch):
        """Unavailable, as opposed to absent, is the conservative case."""
        serve(monkeypatch, 500)
        assert http_mod.allowed("https://example.org/whats-on") is False

    def test_an_unreachable_robots_txt_allows_crawling(self, monkeypatch):
        def boom(url, **kw):
            raise requests.ConnectionError("dns")
        monkeypatch.setattr(http_mod.requests, "get", boom)
        assert http_mod.allowed("https://example.org/whats-on") is True

    def test_rules_for_other_bots_do_not_apply_to_us(self, monkeypatch):
        """Blanket bans on AhrefsBot and GPTBot say nothing about this project."""
        serve(monkeypatch, 200, REAL_WORLD)
        assert http_mod.allowed("https://example.org/about-us/court") is True

    def test_a_disallowed_path_is_still_refused(self, monkeypatch):
        """The check must still actually work."""
        serve(monkeypatch, 200, REAL_WORLD)
        assert http_mod.allowed("https://example.org/search") is False
        assert http_mod.allowed("https://example.org/cpresources/x.css") is False

    def test_a_blanket_disallow_is_respected(self, monkeypatch):
        serve(monkeypatch, 200, "User-agent: *\nDisallow: /\n")
        assert http_mod.allowed("https://example.org/anything") is False

    def test_robots_is_fetched_with_our_own_user_agent(self, monkeypatch):
        """The whole bug was fetching it as Python-urllib."""
        seen = {}
        monkeypatch.setattr(http_mod.requests, "get",
                            lambda url, **kw: seen.update(url=url, headers=kw.get("headers"))
                            or FakeResponse(200, REAL_WORLD))
        http_mod.allowed("https://example.org/whats-on")
        assert seen["url"] == "https://example.org/robots.txt"
        assert "events_app" in seen["headers"]["User-Agent"]

    def test_the_result_is_cached_per_host(self, monkeypatch):
        calls = []
        monkeypatch.setattr(http_mod.requests, "get",
                            lambda url, **kw: calls.append(url) or FakeResponse(200, REAL_WORLD))
        http_mod.allowed("https://example.org/a")
        http_mod.allowed("https://example.org/b")
        assert len(calls) == 1, "robots.txt should be fetched once per host"

    def test_the_override_is_available_but_off_by_default(self, monkeypatch):
        serve(monkeypatch, 200, "User-agent: *\nDisallow: /\n")
        assert http_mod.allowed("https://example.org/x") is False
        monkeypatch.setattr(http_mod, "IGNORE_ROBOTS", True)
        assert http_mod.allowed("https://example.org/x") is True


class TestUserAgent:
    def test_the_user_agent_carries_a_contact(self):
        assert "events_app" in http_mod.UA
        assert http_mod.CONTACT in http_mod.UA


class TestCacheKey:
    def test_accept_header_is_part_of_the_cache_key(self):
        """The same URL can return HTML or JSON depending on the header."""
        a = http_mod._cache_path("https://example.org/x", "application/json")
        b = http_mod._cache_path("https://example.org/x", "text/html")
        c = http_mod._cache_path("https://example.org/x", None)
        assert len({a, b, c}) == 3


class TestCheckLink:
    def test_a_redirect_to_the_site_root_is_flagged(self, monkeypatch):
        class R:
            status_code = 200
            url = "https://example.org/"
        monkeypatch.setattr(http_mod.requests, "head", lambda url, **kw: R())
        probe = http_mod.check_link("https://example.org/deep/page")
        assert probe["redirected_to_root"] is True

    def test_staying_on_the_same_path_is_not_flagged(self, monkeypatch):
        class R:
            status_code = 200
            url = "https://example.org/deep/page"
        monkeypatch.setattr(http_mod.requests, "head", lambda url, **kw: R())
        probe = http_mod.check_link("https://example.org/deep/page")
        assert probe["redirected_to_root"] is False
        assert probe["status"] == 200
