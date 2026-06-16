# Payments zone — agent guide (regulated)

This folder is governed under the **payments** scope (enforce, strict). Two lenses apply:

- **Layer 1 — governance/compliance:** never log or export PAN / personal data (PCI-DSS). Production
  data deletion is forbidden globally.
- **Layer 2 — business logic:** charge amounts must be positive and pass currency/limit checks;
  refunds may not exceed the original charge; changing payment logic requires regression tests.

Read a file before editing it (DC-001). Consult `../../docs/payments/guide.md` before changing logic.
