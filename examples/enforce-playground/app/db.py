"""Demo DB layer for the harness-lens enforce playground.

This file exists so the read/edit scenarios point at a real path. Nothing here is run —
the playground drives *hook events*, not this code. Never issue destructive SQL against a
production database: the Layer-1 invariant blocks it (and that is exactly what the demo shows).
"""


def fetch_user(user_id: int) -> dict:
    return {"id": user_id, "name": "demo-user"}
