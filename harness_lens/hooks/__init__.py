"""Hook entrypoints: receive harness events (record) and install configuration."""

from .record import EVENTS, handle_event

__all__ = ["EVENTS", "handle_event"]
