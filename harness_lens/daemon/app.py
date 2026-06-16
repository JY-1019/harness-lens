"""FastAPI surface over :class:`DaemonRuntime`.

Endpoints (all loopback-only, token-authenticated):

* ``POST /hook/{source}``           — a harness hook posts its payload; the response is the
                                       harness-specific hook output (allow/deny/ask/…).
* ``POST /api/mode``                — switch observe/enforce at runtime.
* ``GET  /api/status``             — daemon mode, pending approval count, rev.
* ``GET  /api/approvals``          — outstanding escalations.
* ``POST /api/approvals/{id}``     — resolve one (approved/denied).
* ``GET  /api/flows`` …            — Flow list / tree / step detail (the GUI read model).
* ``POST /api/flows/{id}/abort``   — request a session abort.
* ``GET  /api/criteria``           — the base 3-Layer harness (invariants / criteria / thresholds).
* ``GET  /api/criteria/effective`` — the project/session-scoped harness after scope resolution.
* ``POST /api/criteria/{layer}``   — human owner edit of layer1 / layer2 / layer3, then hot-reload.

The token gate (``X-HL-Token`` or ``Authorization: Bearer``) plus loopback binding keep
other local users off the control plane. The GUI/WebSocket surface is Phase 2; the REST
read endpoints are provided now so ``harness-lens show`` and the future GUI share one model.
"""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from .approvals import APPROVED, DENIED
from .config import MODES
from .runtime import DaemonRuntime


def _extract_token(x_hl_token: Optional[str], authorization: Optional[str]) -> str:
    if x_hl_token:
        return x_hl_token
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def create_app(runtime: Optional[DaemonRuntime] = None, root: Optional[Path] = None) -> FastAPI:
    runtime = runtime or DaemonRuntime(root)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="harness-lens daemon", version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime

    async def auth(
        x_hl_token: Optional[str] = Header(None, alias="X-HL-Token"),
        authorization: Optional[str] = Header(None),
    ) -> None:
        token = _extract_token(x_hl_token, authorization)
        if not secrets.compare_digest(token, runtime.token):
            raise HTTPException(status_code=403, detail="forbidden")

    # -- hook ingress ---------------------------------------------------- #
    @app.post("/hook/{source}")
    async def hook(source: str, request: Request, _=Depends(auth)) -> JSONResponse:
        if source not in ("claude_code", "codex"):
            raise HTTPException(status_code=404, detail="unknown source")
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 — a malformed body must not 500 the control plane
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        response = await runtime.handle(source, payload)
        return JSONResponse(response or {})

    # -- control / read API ---------------------------------------------- #
    @app.get("/api/status")
    async def status(_=Depends(auth)) -> dict:
        return runtime.status_payload()

    @app.post("/api/mode")
    async def set_mode(body: dict, _=Depends(auth)) -> dict:
        mode = str(body.get("mode", ""))
        if mode not in MODES:
            raise HTTPException(status_code=400, detail=f"mode must be one of {MODES}")
        return {"mode": runtime.set_mode(mode)}

    @app.get("/api/scopes")
    async def get_scopes(_=Depends(auth)) -> dict:
        return {"scopes": runtime.scopes_payload()}

    @app.post("/api/scopes")
    async def set_scopes(body: dict, _=Depends(auth)) -> dict:
        scopes = body.get("scopes")
        if not isinstance(scopes, list):
            raise HTTPException(status_code=400, detail="scopes must be a list")
        return {"scopes": runtime.save_scopes(scopes)}

    @app.post("/api/projects/mode")
    async def set_project_mode(body: dict, _=Depends(auth)) -> dict:
        """Pin observe/enforce for one project folder (exact-cwd scope), or clear it ('global')."""
        cwd = str(body.get("cwd", "")).strip()
        if not cwd:
            raise HTTPException(status_code=400, detail="cwd required")
        try:
            return runtime.set_project_mode(cwd, body.get("mode"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/criteria")
    async def get_criteria(_=Depends(auth)) -> dict:
        return runtime.criteria_payload()

    @app.get("/api/criteria/effective")
    async def get_effective_criteria(
        flow_id: Optional[str] = None,
        cwd: Optional[str] = None,
        session_id: Optional[str] = None,
        _=Depends(auth),
    ) -> dict:
        return runtime.effective_criteria_payload(cwd=cwd, session_id=session_id, flow_id=flow_id)

    @app.post("/api/criteria/{layer}")
    async def edit_criteria(layer: str, body: dict, _=Depends(auth)) -> dict:
        from ..components import ComponentError

        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="expected a JSON object")
        try:
            return runtime.edit_criteria(layer, body)
        except (ComponentError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/approvals")
    async def approvals(_=Depends(auth)) -> list[dict]:
        from dataclasses import asdict

        return [asdict(a) for a in runtime.ledger.pending_approvals()]

    @app.post("/api/approvals/{approval_id}")
    async def resolve_approval(approval_id: str, body: dict, _=Depends(auth)) -> dict:
        resolution = str(body.get("resolution", ""))
        mapped = {"approved": APPROVED, "denied": DENIED}.get(resolution)
        if mapped is None:
            raise HTTPException(status_code=400, detail="resolution must be approved|denied")
        ok = runtime.resolve_approval(approval_id, mapped, body.get("reason"))
        if not ok:
            # No in-memory waiter: either already resolved or the blocked hook is gone. Record the
            # human intent on the ledger row anyway so the audit trail is complete.
            runtime.ledger.resolve_approval(approval_id, resolution, "gui", body.get("reason"))
        return {"resolved": ok, "approval_id": approval_id}

    @app.get("/api/flows")
    async def flows(limit: int = 50, status: Optional[str] = None,
                    source: Optional[str] = None, has_cwd: bool = False,
                    _=Depends(auth)) -> list[dict]:
        return [
            runtime.flow_payload(f)
            for f in runtime.ledger.list_flows(limit=limit, status=status,
                                               source=source, has_cwd=has_cwd)
        ]

    @app.delete("/api/flows/{flow_id}")
    async def delete_flow(flow_id: str, _=Depends(auth)) -> dict:
        if not runtime.delete_flow(flow_id):
            raise HTTPException(status_code=404, detail="flow not found")
        return {"deleted": flow_id}

    @app.get("/api/flows/{flow_id}/tree")
    async def flow_tree(flow_id: str, _=Depends(auth)) -> dict:
        tree = runtime.flow_tree_payload(flow_id)
        if tree is None:
            raise HTTPException(status_code=404, detail="flow not found")
        return tree

    @app.get("/api/steps/{step_id}")
    async def step_detail(step_id: str, _=Depends(auth)) -> dict:
        from dataclasses import asdict

        step = runtime.ledger.get_step(step_id)
        if step is None:
            raise HTTPException(status_code=404, detail="step not found")
        data = asdict(step)
        # Attribute against the FULL input/output (the tree only carries a truncated preview).
        data["harness_usage"] = runtime.usage_for_step(step.tool_name, step.tool_input, step.tool_output)
        return data

    @app.get("/api/flows/{flow_id}/service_harness")
    async def service_harness(flow_id: str, _=Depends(auth)) -> dict:
        payload = runtime.service_harness_payload(flow_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="flow not found")
        return payload

    @app.get("/api/harness/component")
    async def harness_component(kind: str, name: str, cwd: Optional[str] = None,
                                _=Depends(auth)) -> dict:
        return runtime.component_prompt(kind, name, cwd)

    @app.post("/api/flows/{flow_id}/abort")
    async def abort_flow(flow_id: str, _=Depends(auth)) -> dict:
        flow = runtime.ledger.get_flow(flow_id)
        if flow is None:
            raise HTTPException(status_code=404, detail="flow not found")
        flow.status = "aborted"
        runtime._publish_flow(flow)
        # Best-effort: also release any escalation parked for this flow so its hook unblocks.
        return {"flow_id": flow_id, "status": "aborted"}

    # -- live patch stream (WebSocket) ----------------------------------- #
    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        # A browser WebSocket cannot set custom headers, so the token rides the query string.
        token = websocket.query_params.get("token", "")
        if not secrets.compare_digest(token, runtime.token):
            await websocket.close(code=1008)  # policy violation
            return
        await websocket.accept()
        queue = runtime.bus.subscribe()
        try:
            # The client loads a REST snapshot, then applies only patches with rev > snapshot rev.
            await websocket.send_json({"op": "hello", "rev": runtime.bus.rev})
            while True:
                await websocket.send_json(await queue.get())
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001 — a broken socket must not take down the daemon
            pass
        finally:
            runtime.bus.unsubscribe(queue)

    # -- GUI (vanilla JS, served loopback-only) -------------------------- #
    @app.get("/ui", response_class=HTMLResponse)
    async def ui(request: Request) -> HTMLResponse:
        # Loopback Host gate (DNS-rebinding defense), mirroring the legacy GUI. The page embeds
        # the token for its own API/WS calls — same single-user local trust model.
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            raise HTTPException(status_code=403, detail="forbidden")
        from .ui import render_page

        return HTMLResponse(render_page(runtime.token))

    return app
