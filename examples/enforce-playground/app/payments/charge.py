"""Demo payments module — target of the conditional/branching L2 scenario (PG-002).

PG-002 expects ``docs/payments/guide.md`` to be consulted before editing files here. That is a
natural-language criterion scored by the async LLM Judge; the deterministic control path does not
block it (only the built-in DC-001 "read before edit" structural check gates edits in real time).
"""


def authorize(card_token: str, amount: float) -> dict:
    return {"token": card_token, "amount": amount, "authorized": True}
