"""Tool registry.

One registry feeds three consumers: the MCP server's ``tools/list``, the in-process
client agents use in mock mode, and the governance layer that decides which tools a
given agent identity may call.

Each :class:`ToolSpec` carries governance metadata alongside the JSON Schema --
risk tier, whether the tool writes, whether it touches PHI. Those fields are what the
gateway and the audit log key off, so a new tool cannot be added without someone
answering the governance questions about it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RiskTier(str, Enum):
    """How much damage a bad call could do. Drives review and gateway policy."""

    PUBLIC = "public"
    MEMBER_READ = "member_read"
    CLINICAL_READ = "clinical_read"
    CLINICAL_WRITE = "clinical_write"


class ToolSpec(BaseModel):
    """An MCP tool plus the governance facts about it."""

    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any] = Field(exclude=True)
    risk: RiskTier = RiskTier.MEMBER_READ
    writes: bool = False
    touches_phi: bool = True
    #: Human-readable justification, surfaced in the governance docs and the UI.
    purpose_of_use: str = "treatment, payment, or health care operations"

    model_config = {"arbitrary_types_allowed": True}

    def to_mcp(self) -> dict[str, Any]:
        """The shape an MCP client sees from ``tools/list``."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "readOnlyHint": not self.writes,
                "riskTier": self.risk.value,
                "touchesPhi": self.touches_phi,
            },
        }


class ToolResult(BaseModel):
    """Outcome of one invocation, with timing for the audit record."""

    tool: str
    ok: bool
    content: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    record_count: int = 0

    def to_mcp(self) -> dict[str, Any]:
        """MCP ``tools/call`` result. Errors are in-band via ``isError``."""
        import json

        text = json.dumps(self.content if self.ok else {"error": self.error}, indent=2,
                          default=str)
        return {"content": [{"type": "text", "text": text}], "isError": not self.ok}


class ToolNotFound(KeyError):
    """No tool with that name is registered."""


class ToolRegistry:
    """Name -> :class:`ToolSpec`, with validation and timing on invoke."""

    def __init__(self, tools: list[ToolSpec] | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: ToolSpec) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFound(name) from exc

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def list(self) -> list[ToolSpec]:
        return [self._tools[name] for name in sorted(self._tools)]

    def describe(self) -> list[dict[str, Any]]:
        return [tool.to_mcp() for tool in self.list()]

    def validate_arguments(self, tool: ToolSpec, arguments: dict[str, Any]) -> str | None:
        """Check required properties and types against the declared schema.

        A deliberately small subset of JSON Schema. The point is to reject a
        malformed call at the boundary with a clear message rather than to be a
        general validator -- API Management validates the full schema in Azure mode.
        """
        schema = tool.input_schema
        properties = schema.get("properties", {})
        for required in schema.get("required", []):
            value = arguments.get(required)
            if value is None or (isinstance(value, str) and not value.strip()):
                return f"missing required argument: {required}"
        if not schema.get("additionalProperties", False):
            unexpected = sorted(set(arguments) - set(properties))
            if unexpected:
                return f"unexpected arguments: {', '.join(unexpected)}"
        for key, value in arguments.items():
            expected = (properties.get(key) or {}).get("type")
            if expected and not _type_ok(value, expected):
                return f"argument {key} must be of type {expected}"
        return None

    def invoke(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """Validate, call, and time one tool. Handler exceptions become failed results."""
        arguments = dict(arguments or {})
        tool = self.get(name)
        started = time.perf_counter()

        problem = self.validate_arguments(tool, arguments)
        if problem:
            return ToolResult(
                tool=name,
                ok=False,
                error=problem,
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        try:
            content = tool.handler(**arguments)
        except Exception as exc:  # noqa: BLE001 - tool failures are data, not crashes
            return ToolResult(
                tool=name,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        return ToolResult(
            tool=name,
            ok=True,
            content=content,
            duration_ms=(time.perf_counter() - started) * 1000,
            record_count=len(content) if isinstance(content, (list, tuple)) else 1,
        )


_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def _type_ok(value: Any, expected: str) -> bool:
    types = _TYPE_MAP.get(expected)
    if types is None:
        return True
    if expected in {"integer", "number"} and isinstance(value, bool):
        return False
    return isinstance(value, types)
