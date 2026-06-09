"""``python -m harness_lens.daemon`` — run the daemon in the foreground.

This is the entrypoint the background launcher (:func:`harness_lens.daemon.runner.start`)
spawns, and is also usable directly for debugging.
"""

from __future__ import annotations

from .runner import serve

if __name__ == "__main__":
    raise SystemExit(serve())
