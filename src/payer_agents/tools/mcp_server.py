"""MCP server over JSON-RPC 2.0, mounted at ``/mcp``.

Implements the handful of methods a tool-only server needs: ``initialize``,
``notifications/initialized``, ``ping``, ``tools/list`` and ``tools/call``. Resources
and prompts are deliberately absent -- API Management's MCP support covers tools
only, so a server that exposed resources could not be fronted by the gateway this
architecture depends on.

Transport-level governance (token validation, rate limiting, tool allow-lists,
audit) runs through the same :class:`~payer_agents.gateway.apim_sim.ApiManagementSimulator`
the in-process client uses, so local and remote callers are governed identically.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from payer_agents.gateway.apim_sim import ApiManagementSimulator, CallContext
from payer_agents.tools.mcp_client import MCP_PROTOCOL_VERSION
from payer_agents.tools.registry import ToolNotFound, ToolRegistry

SERVER_INFO = {"name": "payer-agents-fhir-tools", "version": "0.1.0"}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def context_from_headers(headers: Any, default_agent: str = "mcp-http-client") -> CallContext:
    """Build the call context from the headers API Management would have populated."""
    getter = headers.get
    return CallContext(
        agent_id=getter("x-agent-id") or default_agent,
        subscription_key=getter("ocp-apim-subscription-key"),
        bearer_token=(getter("authorization") or "").removeprefix("Bearer ").strip() or None,
        correlation_id=getter("x-correlation-id"),
        purpose_of_use=getter("x-purpose-of-use") or "TREAT",
    )


def handle_rpc(
    message: dict[str, Any],
    registry: ToolRegistry,
    gateway: ApiManagementSimulator,
    context: CallContext,
) -> dict[str, Any] | None:
    """Dispatch one JSON-RPC message. ``None`` means "notification, no response"."""
    if message.get("jsonrpc") != "2.0":
        return _error(message.get("id"), INVALID_REQUEST, "jsonrpc must be '2.0'")

    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": (
                    "Synthetic payer FHIR tools. Read-only member coverage, prior "
                    "authorization status, conditions and medications. All data is "
                    "synthetic."
                ),
            },
        )

    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        tools = [tool.to_mcp() for tool in registry.list() if gateway.policy.permits(tool.name)]
        return _result(request_id, {"tools": tools})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _error(
                request_id, INVALID_PARAMS, "name must be a string and arguments an object"
            )

        decision = gateway.authorize(name, context)
        if not decision.allowed:
            gateway.log(name, context, decision, outcome="rejected")
            return _error(
                request_id,
                INVALID_REQUEST,
                f"{decision.policy}: {decision.reason}",
                {"status": decision.status_code, "policy": decision.policy},
            )

        try:
            result = registry.invoke(name, arguments)
        except ToolNotFound:
            gateway.log(name, context, decision, outcome="not_found")
            return _error(request_id, METHOD_NOT_FOUND, f"unknown tool: {name}")

        gateway.log(
            name,
            context,
            decision,
            outcome="ok" if result.ok else "error",
            duration_ms=round(result.duration_ms, 2),
            record_count=result.record_count,
            error=result.error,
        )
        return _result(request_id, result.to_mcp())

    return _error(request_id, METHOD_NOT_FOUND, f"unknown method: {method}")


def build_router(registry: ToolRegistry, gateway: ApiManagementSimulator) -> APIRouter:
    """FastAPI router implementing MCP streamable HTTP (JSON responses only)."""
    router = APIRouter(tags=["mcp"])

    @router.post("")
    async def rpc_endpoint(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(_error(None, PARSE_ERROR, "invalid JSON"), status_code=400)

        context = context_from_headers(request.headers)

        if isinstance(payload, list):
            responses = [
                response
                for message in payload
                if isinstance(message, dict)
                and (response := handle_rpc(message, registry, gateway, context)) is not None
            ]
            if not responses:
                return JSONResponse(content=None, status_code=202)
            return JSONResponse(responses)

        if not isinstance(payload, dict):
            return JSONResponse(
                _error(None, INVALID_REQUEST, "expected an object or array"),
                status_code=400,
            )

        response = handle_rpc(payload, registry, gateway, context)
        if response is None:
            return JSONResponse(content=None, status_code=202)
        return JSONResponse(response)

    @router.get("/tools")
    def list_tools_http() -> dict[str, Any]:
        """Plain-HTTP tool listing. Handy for a browser and for the demo UI."""
        return {
            "tools": [
                {**tool.to_mcp(), "permitted": gateway.policy.permits(tool.name)}
                for tool in registry.list()
            ]
        }

    return router
