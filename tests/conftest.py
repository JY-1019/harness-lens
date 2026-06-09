"""Shared fixtures. Every test runs against an isolated ``HARNESS_LENS_HOME`` so it never
touches the developer's real ``~/.harness-lens`` ledger or a live daemon."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def tmp_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "hl-home"
    home.mkdir()
    monkeypatch.setenv("HARNESS_LENS_HOME", str(home))
    return home
