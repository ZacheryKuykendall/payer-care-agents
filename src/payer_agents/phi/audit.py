"""Audit log.

Every decision, retrieval, tool call, egress evaluation and human handoff produces
one record. Three properties make the log worth having:

**Complete.** The log is written by the orchestrator, not by each agent, so a new
agent cannot forget to emit.

**Hash-chained.** Each record carries the hash of the one before it. Tampering with
record *n* invalidates every record after it, which turns "we have logs" into "we can
show the logs were not edited" -- the distinction an auditor cares about.

**PHI-free by construction.** Records carry counts, categories, identifiers and
decisions, never raw member text. :meth:`AuditLog.record` refuses unredacted-looking
payloads rather than trusting callers.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from payer_agents.domain import new_id
from payer_agents.phi.redaction import PATTERNS

GENESIS_HASH = "0" * 64


class AuditEvent(BaseModel):
    """One immutable line in the trail."""

    event_id: str = Field(default_factory=lambda: new_id("evt"))
    sequence: int = 0
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    )
    category: str
    action: str
    outcome: str = "ok"
    request_id: str | None = None
    conversation_id: str | None = None
    actor: str = "payer-agents"
    subject_token: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    previous_hash: str = GENESIS_HASH
    hash: str = ""

    def compute_hash(self) -> str:
        payload = self.model_dump(exclude={"hash"}, mode="json")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditSink(ABC):
    """Where records go once sealed."""

    @abstractmethod
    def write(self, event: AuditEvent) -> None: ...

    def close(self) -> None:  # noqa: B027 - optional hook, not a required override
        """Flush and release resources. Sinks that hold a handle override this."""


class MemoryAuditSink(AuditSink):
    """Keeps records in a list. Used by tests, the demo and the web UI."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def __iter__(self) -> Iterator[AuditEvent]:
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)


class JsonlAuditSink(AuditSink):
    """Append-only JSON Lines on disk.

    Stands in for the real destination, which is an immutable store: Event Hubs into
    a Log Analytics workspace with an immutability policy, or storage with a legal
    hold. A local file is not tamper-proof; the hash chain is what carries the
    integrity claim here.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")

    def write(self, event: AuditEvent) -> None:
        self._handle.write(json.dumps(event.model_dump(mode="json"), sort_keys=True) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


class PhiLeakError(RuntimeError):
    """A caller tried to write something that looks like raw PHI into the audit log."""


class AuditLog:
    """Hash-chained writer over one or more sinks."""

    def __init__(self, sinks: list[AuditSink] | None = None, *, strict: bool = True) -> None:
        self.sinks: list[AuditSink] = sinks if sinks is not None else [MemoryAuditSink()]
        self.strict = strict
        self._last_hash = GENESIS_HASH
        self._sequence = 0
        self.events: list[AuditEvent] = []

    @property
    def memory(self) -> MemoryAuditSink | None:
        for sink in self.sinks:
            if isinstance(sink, MemoryAuditSink):
                return sink
        return None

    def record(
        self,
        category: str,
        action: str,
        *,
        outcome: str = "ok",
        request_id: str | None = None,
        conversation_id: str | None = None,
        actor: str = "payer-agents",
        subject_token: str | None = None,
        **detail: Any,
    ) -> AuditEvent:
        """Seal one record and fan it out to every sink."""
        if self.strict:
            self._assert_clean(detail)
        self._sequence += 1
        event = AuditEvent(
            sequence=self._sequence,
            category=category,
            action=action,
            outcome=outcome,
            request_id=request_id,
            conversation_id=conversation_id,
            actor=actor,
            subject_token=subject_token,
            detail=detail,
            previous_hash=self._last_hash,
        )
        event.hash = event.compute_hash()
        self._last_hash = event.hash
        self.events.append(event)
        for sink in self.sinks:
            sink.write(event)
        return event

    def verify(self) -> bool:
        """Re-walk the chain. ``False`` means a record was altered or removed."""
        previous = GENESIS_HASH
        for event in self.events:
            if event.previous_hash != previous:
                return False
            if event.hash != event.compute_hash():
                return False
            previous = event.hash
        return True

    def as_dicts(self) -> list[dict[str, Any]]:
        return [event.model_dump(mode="json") for event in self.events]

    def close(self) -> None:
        for sink in self.sinks:
            sink.close()

    @staticmethod
    def _assert_clean(detail: dict[str, Any]) -> None:
        blob = json.dumps(detail, default=str)
        for category, pattern in PATTERNS:
            if pattern.search(blob):
                raise PhiLeakError(
                    f"audit detail contains an unredacted {category.value}; redact before recording"
                )
