"""Demo service for the harness-lens enforce playground.

The L2 (DC-001 "read before edit") scenario uses this file: editing it without a prior Read
in the same flow escalates to a human approval. Read it first and the edit is allowed.
"""

from .db import fetch_user


def charge(user_id: int, amount: float) -> dict:
    user = fetch_user(user_id)
    return {"user": user, "amount": amount, "ok": True}
