"""How an agent calls a tool.

Both implementations enforce the same invariant: a tool call is authorised by the
gateway and written to the audit log *before* the handler runs, and the audit record
is written whether the call succeeded, failed, or was rejected. Logging only
successes is how a tool-abuse incident becomes invisible.
"""

from __future__ import annotations

import json
import uuid
from abc import ABC, abstractmethod
from typing import Any

from payer_agents.domain import ToolCall
from payer_agents.gateway.apim_sim import ApiManagementSimulator, CallContext
from payer_agents.phi.audit import AuditLog
from payer_agents.tools.registry import ToolNotFound, ToolRegistry, ToolResult

MCP_PROTOCOL_VERSION = "2025-06-18"


class McpToolError(RuntimeError):
    """A tool call could not be completed."""

    def __init__(self, message: str, *, status_code: int = 500, policy: str = "error") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.policy = policy


class McpClient(ABC):
    """The two operations an agent needs."""

    @abstractmethod
    def list_tools(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> ToolCall: ...


class InProcessMcpClient(McpClient):
    """Calls the registry directly, still through the gateway policy chain.

    Mock mode uses this so the demo runs with no sockets. The governance path is
    identical to the HTTP client's; only the transport differs.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        gateway: ApiManagementSimulator | None = None,
        context: CallContext | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.registry = registry
        self.gateway = gateway or ApiManagementSimulator(audit=audit)
        self.context = context or CallContext()
        self.audit = audit

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            tool.to_mcp() for tool in self.registry.list() if self.gateway.policy.permits(tool.name)
        ]

    def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, *, request_id: str | None = None
    ) -> ToolCall:
        arguments = dict(arguments or {})
        context = self.context.model_copy(
            update={"correlation_id": self.context.correlation_id or uuid.uuid4().hex}
        )

        decision = self.gateway.authorize(name, context)
        if not decision.allowed:
            self.gateway.log(
                name,
                context,
                decision,
                outcome="rejected",
                request_id=request_id,
                argument_keys=sorted(arguments),
            )
            return ToolCall(
                tool=name,
                arguments=_safe_arguments(arguments),
                ok=False,
                error=f"{decision.policy}: {decision.reason}",
                gateway_decision=decision.summary,
                correlation_id=context.correlation_id or "",
            )

        try:
            result = self.registry.invoke(name, arguments)
        except ToolNotFound:
            result = ToolResult(tool=name, ok=False, error=f"unknown tool: {name}")

        self.gateway.log(
            name,
            context,
            decision,
            outcome="ok" if result.ok else "error",
            request_id=request_id,
            duration_ms=round(result.duration_ms, 2),
            record_count=result.record_count,
            argument_keys=sorted(arguments),
            error=result.error,
        )
        return ToolCall(
            tool=name,
            arguments=_safe_arguments(arguments),
            ok=result.ok,
            duration_ms=result.duration_ms,
            result_summary=_summarise(result),
            payload=result.content if result.ok else None,
            error=result.error,
            gateway_decision=decision.summary,
            correlation_id=context.correlation_id or "",
        )

    def invoke_raw(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """Gateway-checked call that returns the payload, for agents that need the data."""
        context = self.context.model_copy(
            update={"correlation_id": self.context.correlation_id or uuid.uuid4().hex}
        )
        decision = self.gateway.authorize(name, context)
        if not decision.allowed:
            self.gateway.log(name, context, decision, outcome="rejected")
            raise McpToolError(
                f"{decision.policy}: {decision.reason}",
                status_code=decision.status_code,
                policy=decision.policy,
            )
        result = self.registry.invoke(name, arguments)
        self.gateway.log(
            name,
            context,
            decision,
            outcome="ok" if result.ok else "error",
            duration_ms=round(result.duration_ms, 2),
            record_count=result.record_count,
            error=result.error,
        )
        return result


class HttpMcpClient(McpClient):
    """JSON-RPC 2.0 over HTTP against a remote MCP endpoint.

    In Azure the endpoint is API Management, which is why the credential header goes
    here and not into the FHIR client: the agent holds a gateway credential, never a
    clinical-system credential.
    """

    def __init__(
        self, endpoint: str, headers: dict[str, str] | None = None, client: Any | None = None
    ) -> None:
        self.endpoint = endpoint
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        if client is None:
            import httpx

            client = httpx.Client(timeout=30.0)
        self._client = client
        self._request_id = 0

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._request_id += 1
        body = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params or {},
        }
        response = self._client.post(self.endpoint, json=body, headers=self.headers)
        if response.status_code >= 400:
            raise McpToolError(
                f"MCP endpoint returned HTTP {response.status_code}",
                status_code=response.status_code,
                policy="transport",
            )
        payload = response.json()
        if "error" in payload:
            error = payload["error"]
            raise McpToolError(str(error.get("message", error)), policy="jsonrpc")
        return payload.get("result", {})

    def list_tools(self) -> list[dict[str, Any]]:
        return self._rpc("tools/list").get("tools", [])

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> ToolCall:
        correlation_id = uuid.uuid4().hex
        try:
            result = self._rpc("tools/call", {"name": name, "arguments": dict(arguments or {})})
        except McpToolError as exc:
            return ToolCall(
                tool=name,
                arguments=_safe_arguments(arguments or {}),
                ok=False,
                error=str(exc),
                gateway_decision=f"{exc.policy} ({exc.status_code})",
                correlation_id=correlation_id,
            )
        is_error = bool(result.get("isError"))
        text = "".join(
            block.get("text", "")
            for block in result.get("content", [])
            if block.get("type") == "text"
        )
        payload: Any = None
        if not is_error:
            try:
                payload = json.loads(text)
            except ValueError:
                payload = text
        return ToolCall(
            tool=name,
            arguments=_safe_arguments(arguments or {}),
            ok=not is_error,
            result_summary=text[:400],
            payload=payload,
            error=text[:400] if is_error else None,
            correlation_id=correlation_id,
        )


def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Keys always, values only where they are not member identifiers.

    The audit log needs to show *that* a lookup happened and on which scope, which the
    member token already carries. Repeating the raw identifier in every record widens
    the PHI surface for no investigative benefit.
    """
    redacted_keys = {"member_id", "clinical_summary"}
    return {k: ("<redacted>" if k in redacted_keys else v) for k, v in arguments.items()}


def _summarise(result: ToolResult) -> str:
    """Shape of the result, not the result.

    ``ToolCall`` is serialised into API responses and rendered in the demo console,
    so dumping the payload here would widen the PHI surface to every consumer of a
    turn. Agents read :attr:`ToolCall.payload`, which is excluded from serialisation;
    everyone else gets enough to see that a call happened and what came back.
    """
    if not result.ok:
        return result.error or "failed"
    content = result.content
    if isinstance(content, dict):
        return f"object with fields: {', '.join(sorted(content))}"
    if isinstance(content, (list, tuple)):
        return f"{len(content)} record(s)"
    return f"{type(content).__name__} result"
