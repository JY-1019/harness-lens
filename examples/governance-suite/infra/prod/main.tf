# Demo production infra (the "red zone"). The prod scope enforces change-control governance:
# no direct apply/destroy, no disabling audit logs. This file is just a target for the scenarios.

resource "demo_database" "ledger" {
  name         = "ledger-prod"
  audit_logs   = true   # governance: must stay enabled
  deletion_protection = true
}
