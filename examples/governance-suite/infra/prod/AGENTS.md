# Production infra zone — agent guide (red zone, strictest)

Governed under the **prod** scope (enforce, strictest L3). Layer-1 governance: no direct
apply/destroy against production, never disable audit logs, no direct production data deletion.
Layer-2 business/process: infra changes require an impact plan and approval in a change window.
