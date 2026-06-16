"""Payments domain — the L2 (business-logic) governance target.

The governance suite's Layer-2 criteria are *business rules* about this module, e.g.:
  - a charge amount must be positive and within the currency/limit checks,
  - a refund may not exceed the original charge,
  - inventory is decremented only after the charge is confirmed.
The Layer-1 criteria over this folder are *governance/compliance* rules (PCI: never log/export PAN).
"""


def authorize(card_token: str, amount: float, currency: str = "USD") -> dict:
    if amount <= 0:
        raise ValueError("amount must be positive")  # PAY-001 (business rule)
    return {"token": card_token, "amount": amount, "currency": currency, "authorized": True}


def refund(original_amount: float, refund_amount: float) -> dict:
    if refund_amount > original_amount:
        raise ValueError("refund exceeds original charge")  # PAY-002 (business rule)
    return {"refunded": refund_amount, "ok": True}
