"""harness-lens — observe agentic harnesses and evolve their external scaffolding.

See the package modules for the three observability pillars:

* :mod:`harness_lens.reconstructor` — hook events → Flow / Task / Step.
* :mod:`harness_lens.experience` + :mod:`harness_lens.agents.debugger` — Pillar 2.
* :mod:`harness_lens.decision` + :mod:`harness_lens.agents.evolver` — Pillar 3.
"""

from __future__ import annotations

__version__ = "0.1.0"

HARNESS_HOME_ENV = "HARNESS_LENS_HOME"


def home_dir():
    """Return the harness-lens runtime directory (``~/.harness-lens`` by default)."""
    import os
    from pathlib import Path

    override = os.environ.get(HARNESS_HOME_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".harness-lens"


__all__ = ["__version__", "home_dir", "HARNESS_HOME_ENV"]
