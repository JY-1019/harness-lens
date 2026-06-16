# Payments change guide (demo)

Business rules enforced as Layer-2 criteria for `services/payments/`:
- charge amount must be positive and pass currency/limit validation,
- refunds must not exceed the original charge,
- decrement inventory only after the charge is confirmed,
- payment-logic changes must come with regression tests.

PAY-001 expects this guide to be consulted before changing `charge.py` (scored by the async Judge).
