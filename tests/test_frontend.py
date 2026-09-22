"""Front-end tests, run through Node against the shipped `app.js`.

The page has no build step and no framework, so there is nothing to import:
the harness in ``tests/js`` evaluates ``docs/assets/app.js`` itself and calls
its own functions.  That is the point -- a copy of the filter logic in a test
would pass happily while the shipped file was broken.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "docs/assets/app.js"
DATA = ROOT / "docs/data/events.json"
HARNESS = Path(__file__).parent / "js"

node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="node is not installed")


@node
def test_app_js_parses():
    """A syntax error ships a blank page: nothing else here would catch it."""
    subprocess.run(["node", "--check", str(APP)], check=True,
                   capture_output=True, text=True)


@node
def test_one_off_and_running_split_the_catalogue():
    """The Kind filter, driven through app.js's own matches()."""
    proc = subprocess.run(
        ["node", str(HARNESS / "shape_filter.mjs"), str(APP), str(DATA)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert proc.stdout.startswith("ok:")


@node
def test_the_kind_chips_are_built_and_are_exclusive():
    """Clicked through a stub DOM: the predicate can be right and the panel
    still be empty, and `node --check` would not notice."""
    proc = subprocess.run(
        ["node", str(HARNESS / "shape_chips.mjs"), str(APP)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert proc.stdout.startswith("ok:")


def test_the_filter_panel_offers_the_control():
    """The chips are built into #shape, so the container has to be there."""
    html = (ROOT / "docs/index.html").read_text(encoding="utf-8")
    assert 'id="shape"' in html


def test_the_data_carries_both_kinds():
    """A filter nobody can use is worse than no filter."""
    import json
    events = json.loads(DATA.read_text(encoding="utf-8"))["events"]
    runs = [e for e in events if e.get("ongoing") or e.get("anytime")]
    assert runs, "no multi-day or undated events; the Kind filter has nothing to do"
    assert len(runs) < len(events), "everything is a run; the split is meaningless"
