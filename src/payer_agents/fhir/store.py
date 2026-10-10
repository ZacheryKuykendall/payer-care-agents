"""In-memory FHIR R4 store over the synthetic bundles in ``data/fhir/bundles``.

Only the slice a payer member-services assistant actually needs is implemented:
Patient, Coverage, Condition, MedicationRequest, ServiceRequest, and the
Claim/ClaimResponse pair that carries a prior-authorization determination.

Prior authorization is modelled as ``Claim.use = "preauthorization"`` with a matching
``ClaimResponse``, which is the shape the Da Vinci prior-authorization work builds on.
``ClaimResponse.outcome`` is a narrow FHIR code set (``queued``, ``complete``,
``error``, ``partial``), so :class:`PriorAuthorization` also carries the operational
status a member recognises -- derived once, here, instead of in three agents.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from payer_agents.config import DATA_ROOT

SYNTHETIC_TAG = "synthetic"
MEMBER_ID_SYSTEM = "https://example.invalid/northwind/member-id"


class FhirStoreError(RuntimeError):
    """A synthetic bundle could not be loaded."""


class PriorAuthorization(BaseModel):
    """Flattened view of one Claim/ClaimResponse pair."""

    reference_number: str
    service: str
    service_code: str | None = None
    requested_on: str | None = None
    decided_on: str | None = None
    outcome: str = "queued"
    status: str = "received"
    disposition: str = ""
    valid_from: str | None = None
    valid_through: str | None = None
    notes: list[str] = Field(default_factory=list)
    requesting_provider: str | None = None

    @property
    def is_final(self) -> bool:
        return self.status in {"approved", "partially approved", "denied", "withdrawn"}


#: ``ClaimResponse.outcome`` plus a disposition keyword to the member-facing status in
#: UM-001. Mapping in one place keeps the agent from inventing its own vocabulary.
_OUTCOME_TO_STATUS = {
    "complete": "approved",
    "partial": "partially approved",
    "error": "denied",
    "queued": "in clinical review",
}

_DISPOSITION_KEYWORDS = (
    ("pending information", "pending information"),
    ("additional clinical", "pending information"),
    ("withdrawn", "withdrawn"),
    ("in clinical review", "in clinical review"),
)


def derive_status(outcome: str, disposition: str) -> str:
    """Operational status a member recognises, derived from the FHIR outcome code."""
    lowered = (disposition or "").lower()
    for keyword, status in _DISPOSITION_KEYWORDS:
        if keyword in lowered:
            return status
    return _OUTCOME_TO_STATUS.get(outcome, "received")


class FhirStore:
    """Loads synthetic bundles and answers the handful of queries the tools need."""

    def __init__(self, resources: list[dict[str, Any]] | None = None) -> None:
        self._by_type: dict[str, list[dict[str, Any]]] = {}
        self._by_reference: dict[str, dict[str, Any]] = {}
        for resource in resources or []:
            self._insert(resource)

    # -- loading ---------------------------------------------------------------

    @classmethod
    def from_directory(cls, directory: Path | None = None) -> FhirStore:
        base = Path(directory) if directory is not None else DATA_ROOT / "fhir" / "bundles"
        if not base.exists():
            raise FhirStoreError(f"FHIR bundle directory not found: {base}")
        store = cls()
        for path in sorted(base.glob("*.json")):
            store.load_bundle(json.loads(path.read_text(encoding="utf-8")), source=path.name)
        return store

    def load_bundle(self, bundle: dict[str, Any], source: str = "<inline>") -> None:
        """Load one FHIR Bundle, refusing anything not tagged synthetic.

        The guard is the point. A repository that promises "synthetic data only" should
        enforce it in code, not in a README sentence, so that dropping a real export
        into ``data/fhir`` fails loudly on the first run.
        """
        if bundle.get("resourceType") != "Bundle":
            raise FhirStoreError(f"{source}: expected a Bundle resource")
        tags = {t.get("code") for t in (bundle.get("meta") or {}).get("tag", [])}
        if SYNTHETIC_TAG not in tags:
            raise FhirStoreError(
                f"{source}: bundle is not tagged '{SYNTHETIC_TAG}'. This repository "
                "accepts synthetic data only."
            )
        for entry in bundle.get("entry", []):
            resource = entry.get("resource")
            if isinstance(resource, dict):
                self._insert(resource)

    def _insert(self, resource: dict[str, Any]) -> None:
        resource_type = resource.get("resourceType")
        resource_id = resource.get("id")
        if not resource_type or not resource_id:
            raise FhirStoreError("resource is missing resourceType or id")
        self._by_type.setdefault(resource_type, []).append(resource)
        self._by_reference[f"{resource_type}/{resource_id}"] = resource

    # -- queries ---------------------------------------------------------------

    def all_of(self, resource_type: str) -> list[dict[str, Any]]:
        return list(self._by_type.get(resource_type, []))

    def by_reference(self, reference: str) -> dict[str, Any] | None:
        return self._by_reference.get(reference)

    def patient_by_member_id(self, member_id: str) -> dict[str, Any] | None:
        wanted = (member_id or "").strip().upper()
        if not wanted:
            return None
        for patient in self.all_of("Patient"):
            for identifier in patient.get("identifier", []):
                if str(identifier.get("value", "")).upper() == wanted:
                    return patient
        return None

    def _for_subject(self, resource_type: str, patient_id: str, field: str) -> list[dict[str, Any]]:
        reference = f"Patient/{patient_id}"
        return [
            resource
            for resource in self.all_of(resource_type)
            if (resource.get(field) or {}).get("reference") == reference
        ]

    def coverage_for(self, patient_id: str) -> list[dict[str, Any]]:
        return self._for_subject("Coverage", patient_id, "beneficiary")

    def conditions_for(self, patient_id: str) -> list[dict[str, Any]]:
        return self._for_subject("Condition", patient_id, "subject")

    def medications_for(self, patient_id: str) -> list[dict[str, Any]]:
        return self._for_subject("MedicationRequest", patient_id, "subject")

    def service_requests_for(self, patient_id: str) -> list[dict[str, Any]]:
        return self._for_subject("ServiceRequest", patient_id, "subject")

    def claims_for(self, patient_id: str, use: str | None = "preauthorization") -> list[dict[str, Any]]:
        claims = self._for_subject("Claim", patient_id, "patient")
        if use is None:
            return claims
        return [claim for claim in claims if claim.get("use") == use]

    def claim_response_for(self, claim_id: str) -> dict[str, Any] | None:
        reference = f"Claim/{claim_id}"
        for response in self.all_of("ClaimResponse"):
            if (response.get("request") or {}).get("reference") == reference:
                return response
        return None

    # -- projections -----------------------------------------------------------

    def prior_authorizations(
        self, patient_id: str, reference_number: str | None = None
    ) -> list[PriorAuthorization]:
        """Flatten Claim + ClaimResponse into something an agent can read aloud."""
        wanted = (reference_number or "").strip().upper() or None
        results: list[PriorAuthorization] = []
        for claim in self.claims_for(patient_id):
            reference = _first_identifier(claim) or claim.get("id", "")
            if wanted and reference.upper() != wanted:
                continue
            response = self.claim_response_for(claim.get("id", ""))
            item = (claim.get("item") or [{}])[0]
            coding = ((item.get("productOrService") or {}).get("coding") or [{}])[0]
            outcome = (response or {}).get("outcome", "queued")
            disposition = (response or {}).get("disposition", "")
            period = (response or {}).get("preAuthPeriod") or {}
            results.append(
                PriorAuthorization(
                    reference_number=reference,
                    service=coding.get("display") or item.get("productOrService", {}).get("text", "requested service"),
                    service_code=coding.get("code"),
                    requested_on=claim.get("created"),
                    decided_on=(response or {}).get("created"),
                    outcome=outcome,
                    status=derive_status(outcome, disposition),
                    disposition=disposition,
                    valid_from=period.get("start"),
                    valid_through=period.get("end"),
                    notes=[
                        note.get("text", "")
                        for note in (response or {}).get("processNote", [])
                        if note.get("text")
                    ],
                    requesting_provider=(claim.get("provider") or {}).get("display"),
                )
            )
        results.sort(key=lambda pa: (pa.requested_on or "", pa.reference_number), reverse=True)
        return results

    def plan_id_for(self, patient_id: str) -> str | None:
        """Plan identifier used to filter benefits retrieval to the member's plan."""
        for coverage in self.coverage_for(patient_id):
            for klass in coverage.get("class", []):
                if (klass.get("type") or {}).get("text") == "plan":
                    return klass.get("value")
        return None


def _first_identifier(resource: dict[str, Any]) -> str | None:
    for identifier in resource.get("identifier", []):
        value = identifier.get("value")
        if value:
            return str(value)
    return None


def patient_display_name(patient: dict[str, Any]) -> str:
    name = (patient.get("name") or [{}])[0]
    given = " ".join(name.get("given", []))
    return f"{given} {name.get('family', '')}".strip() or patient.get("id", "unknown")
