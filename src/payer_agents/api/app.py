"""FastAPI application.

One process serves four things that are four separate deployments in Azure:

``/``       the demo console (static, no build step)
``/api``    the orchestrator
``/mcp``    the MCP tool server, which API Management fronts in Azure
``/fhir``   the synthetic FHIR stub, which is a real FHIR service in Azure

Co-locating them is a local convenience, not the architecture. ``docs/ARCHITECTURE.md``
shows where the boundaries actually fall, and the egress policy is written against
the deployed hostnames rather than against ``localhost``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from payer_agents.config import Settings
from payer_agents.demo_scenarios import SCENARIOS
from payer_agents.domain import Channel, MemberRequest
from payer_agents.fhir import server as fhir_server
from payer_agents.fhir.client import LocalFhirClient
from payer_agents.orchestrator import Orchestrator, build
from payer_agents.tools import mcp_server

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    member_id: str | None = None
    channel: Channel = Channel.MEMBER_PORTAL
    conversation_id: str | None = None


class EgressProbe(BaseModel):
    url: str


def create_app(
    orchestrator: Orchestrator | None = None, settings: Settings | None = None
) -> FastAPI:
    """Build the app. Accepts an orchestrator so tests can inject a configured one."""
    orchestrator = orchestrator or build(settings)
    app = FastAPI(
        title="Payer Care Agents",
        version="0.1.0",
        description=(
            "Reference architecture for a governed multi-agent healthcare payer "
            "assistant. Synthetic data only."
        ),
    )
    app.state.orchestrator = orchestrator

    # -- demo + system ------------------------------------------------------

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "mode": orchestrator.settings.mode.value}

    @app.get("/api/system", tags=["demo"])
    def system() -> dict[str, Any]:
        return orchestrator.describe()

    @app.get("/api/scenarios", tags=["demo"])
    def scenarios() -> dict[str, Any]:
        return {"scenarios": [s.model_dump(mode="json") for s in SCENARIOS]}

    @app.post("/api/chat", tags=["demo"])
    def chat(body: ChatRequest) -> dict[str, Any]:
        request = MemberRequest(text=body.text, member_id=body.member_id, channel=body.channel)
        if body.conversation_id:
            request.conversation_id = body.conversation_id
        result = orchestrator.handle(request)
        return {
            "result": result.model_dump(mode="json"),
            "automated": result.automated,
            "display_text": result.display_text,
        }

    @app.get("/api/audit", tags=["demo"])
    def audit(limit: int = 100) -> dict[str, Any]:
        events = orchestrator.audit.as_dicts()
        return {
            "total": len(events),
            "chain_valid": orchestrator.audit.verify(),
            "events": events[-max(1, limit) :],
        }

    @app.post("/api/egress/evaluate", tags=["demo"])
    def evaluate_egress(body: EgressProbe) -> dict[str, Any]:
        if orchestrator.egress_policy is None:
            raise HTTPException(status_code=503, detail="no egress policy loaded")
        decision = orchestrator.egress_policy.evaluate(body.url)
        return decision.model_dump(mode="json")

    @app.get("/api/evals", tags=["demo"])
    def evals() -> dict[str, Any]:
        from payer_agents.evals.runner import run_suite

        return run_suite(orchestrator).model_dump(mode="json")

    # -- mounted services ----------------------------------------------------

    fhir_client = LocalFhirClient()
    app.include_router(fhir_server.build_router(fhir_client.store), prefix="/fhir")
    if orchestrator.registry is not None:
        gateway = getattr(orchestrator.mcp, "gateway", None)
        if gateway is not None:
            app.include_router(
                mcp_server.build_router(orchestrator.registry, gateway), prefix="/mcp"
            )

    # -- static UI -----------------------------------------------------------

    if WEB_ROOT.exists():
        app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(WEB_ROOT / "index.html")

    return app
