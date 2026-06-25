"""Shared fixtures. Every test runs against an isolated ``HARNESS_LENS_HOME`` so it never
touches the developer's real ``~/.harness-lens`` ledger / daemon config."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch) -> Path:
    """Point ``HARNESS_LENS_HOME`` at a per-test temp dir for EVERY test (autouse), whether or not it
    requests :func:`tmp_home`. This is the guarantee the module docstring makes — without it, a test
    that forgot the fixture would read/write the developer's real ``~/.harness-lens`` and could, e.g.,
    flip a live daemon's mode by saving a default config over it."""
    home = tmp_path / "hl-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HARNESS_LENS_HOME", str(home))
    return home


@pytest.fixture
def tmp_home(_isolate_home) -> Path:
    """The isolated home for tests that need the path explicitly (same dir as the autouse isolation)."""
    return _isolate_home
