"""Shared fixtures and the network marker."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).parent / "fixtures"


def pytest_configure(config):
    config.addinivalue_line("markers",
                            "network: hits live venue sites; deselect with -m 'not network'")


@pytest.fixture(scope="session")
def sources() -> dict:
    doc = yaml.safe_load((ROOT / "collector/sources.yaml").read_text())
    return {s["key"]: s for s in doc["sources"]}


@pytest.fixture
def fixture_text():
    def _load(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")
    return _load
