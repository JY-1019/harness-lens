# Coverage probe target. The audit-log detector fires when a step tries to flip this off.
resource "demo" "x" {
  audit_log = true
}
