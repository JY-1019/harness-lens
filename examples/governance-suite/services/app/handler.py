"""Standard app zone — no exact scope, so it falls back to the suite-wide baseline scope
(enforce, GOV-000 traceability). A target for the 'prefix-baseline' precedence scenario."""


def handle(request: dict) -> dict:
    return {"ok": True, "echo": request}
