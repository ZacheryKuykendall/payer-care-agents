"""How tools reach FHIR data.

Two implementations behind one protocol:

``LocalFhirClient``  reads the in-process synthetic store. Mock mode, tests, CI.
``HttpFhirClient``   talks FHIR REST over HTTP to a real server (Azure Health Data
                     Services, say) through whatever transport it is given -- which in
                     the deployed topology is the egress-guarded one.

Neither of them is reachable from an agent directly. Agents call tools; tools call a
client. That indirection is what lets API Management sit in the middle in Azure mode
without the agent code knowing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from payer_agents.fhir.store import (
    FhirStore,
    PriorAuthorization,
    derive_status,
    patient_display_name,
    sort_newest_first,
)


class FhirClient(ABC):
    """The FHIR operations the tool layer needs."""

    name: str = "fhir-client"

    @abstractmethod
    def find_patient(self, member_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def coverage_summary(self, member_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def prior_authorizations(
        self, member_id: str, reference_number: str | None = None
    ) -> list[PriorAuthorization]: ...

    @abstractmethod
    def conditions(self, member_id: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def medications(self, member_id: str) -> list[dict[str, Any]]: ...


class LocalFhirClient(FhirClient):
    """Direct reads against the in-memory synthetic store."""

    name = "local-fhir"

    def __init__(self, store: FhirStore | None = None) -> None:
        self.store = store or FhirStore.from_directory()

    def _patient_id(self, member_id: str) -> str | None:
        patient = self.store.patient_by_member_id(member_id)
        return patient.get("id") if patient else None

    def find_patient(self, member_id: str) -> dict[str, Any] | None:
        return self.store.patient_by_member_id(member_id)

    def coverage_summary(self, member_id: str) -> dict[str, Any] | None:
        patient = self.store.patient_by_member_id(member_id)
        if patient is None:
            return None
        patient_id = patient["id"]
        coverages = self.store.coverage_for(patient_id)
        if not coverages:
            return None
        coverage = coverages[0]
        classes = {(c.get("type") or {}).get("text", "other"): c for c in coverage.get("class", [])}
        plan = classes.get("plan", {})
        return {
            "member_id": member_id.upper(),
            "member_name": patient_display_name(patient),
            "status": coverage.get("status"),
            "plan_id": plan.get("value"),
            "plan_name": plan.get("name"),
            "group": (classes.get("group") or {}).get("value"),
            "effective_from": (coverage.get("period") or {}).get("start"),
            "effective_through": (coverage.get("period") or {}).get("end"),
        }

    def prior_authorizations(
        self, member_id: str, reference_number: str | None = None
    ) -> list[PriorAuthorization]:
        patient_id = self._patient_id(member_id)
        if patient_id is None:
            return []
        return self.store.prior_authorizations(patient_id, reference_number)

    def conditions(self, member_id: str) -> list[dict[str, Any]]:
        patient_id = self._patient_id(member_id)
        if patient_id is None:
            return []
        return [
            {
                "code": ((c.get("code") or {}).get("coding") or [{}])[0].get("code"),
                "display": (c.get("code") or {}).get("text")
                or ((c.get("code") or {}).get("coding") or [{}])[0].get("display"),
                "clinical_status": ((c.get("clinicalStatus") or {}).get("coding") or [{}])[0].get(
                    "code"
                ),
                "onset": c.get("onsetDateTime"),
            }
            for c in self.store.conditions_for(patient_id)
        ]

    def medications(self, member_id: str) -> list[dict[str, Any]]:
        patient_id = self._patient_id(member_id)
        if patient_id is None:
            return []
        return [
            {
                "medication": (m.get("medicationCodeableConcept") or {}).get("text"),
                "status": m.get("status"),
                "authored_on": m.get("authoredOn"),
                "instructions": (m.get("dosageInstruction") or [{}])[0].get("text"),
            }
            for m in self.store.medications_for(patient_id)
        ]

    def plan_id(self, member_id: str) -> str | None:
        patient_id = self._patient_id(member_id)
        return self.store.plan_id_for(patient_id) if patient_id else None


class HttpFhirClient(FhirClient):
    """FHIR REST client.

    ``transport`` is any object with ``get(path, params) -> dict``. In Azure mode the
    orchestrator passes an egress-guarded transport, so a misconfigured base URL is
    refused by policy before a request leaves the process.
    """

    name = "http-fhir"

    def __init__(self, transport: Any) -> None:
        self.transport = transport

    def _patient_id(self, member_id: str) -> str | None:
        patient = self.find_patient(member_id)
        return patient.get("id") if patient else None

    @staticmethod
    def _entries(bundle: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not bundle:
            return []
        return [
            entry["resource"]
            for entry in bundle.get("entry", [])
            if isinstance(entry.get("resource"), dict)
        ]

    def find_patient(self, member_id: str) -> dict[str, Any] | None:
        bundle = self.transport.get("Patient", {"identifier": member_id})
        entries = self._entries(bundle)
        return entries[0] if entries else None

    def coverage_summary(self, member_id: str) -> dict[str, Any] | None:
        patient = self.find_patient(member_id)
        if patient is None:
            return None
        bundle = self.transport.get("Coverage", {"beneficiary": f"Patient/{patient['id']}"})
        entries = self._entries(bundle)
        if not entries:
            return None
        coverage = entries[0]
        classes = {(c.get("type") or {}).get("text", "other"): c for c in coverage.get("class", [])}
        plan = classes.get("plan", {})
        return {
            "member_id": member_id.upper(),
            "member_name": patient_display_name(patient),
            "status": coverage.get("status"),
            "plan_id": plan.get("value"),
            "plan_name": plan.get("name"),
            "group": (classes.get("group") or {}).get("value"),
            "effective_from": (coverage.get("period") or {}).get("start"),
            "effective_through": (coverage.get("period") or {}).get("end"),
        }

    def prior_authorizations(
        self, member_id: str, reference_number: str | None = None
    ) -> list[PriorAuthorization]:
        patient_id = self._patient_id(member_id)
        if patient_id is None:
            return []
        claims = self._entries(
            self.transport.get(
                "Claim", {"patient": f"Patient/{patient_id}", "use": "preauthorization"}
            )
        )
        results: list[PriorAuthorization] = []
        for claim in claims:
            reference = next(
                (i.get("value") for i in claim.get("identifier", []) if i.get("value")),
                claim.get("id", ""),
            )
            if reference_number and str(reference).upper() != reference_number.strip().upper():
                continue
            responses = self._entries(
                self.transport.get("ClaimResponse", {"request": f"Claim/{claim['id']}"})
            )
            response = responses[0] if responses else {}
            item = (claim.get("item") or [{}])[0]
            coding = ((item.get("productOrService") or {}).get("coding") or [{}])[0]
            outcome = response.get("outcome", "queued")
            disposition = response.get("disposition", "")
            period = response.get("preAuthPeriod") or {}
            results.append(
                PriorAuthorization(
                    reference_number=str(reference),
                    service=coding.get("display", "requested service"),
                    service_code=coding.get("code"),
                    requested_on=claim.get("created"),
                    decided_on=response.get("created"),
                    outcome=outcome,
                    status=derive_status(outcome, disposition),
                    disposition=disposition,
                    valid_from=period.get("start"),
                    valid_through=period.get("end"),
                    notes=[
                        note.get("text", "")
                        for note in response.get("processNote", [])
                        if note.get("text")
                    ],
                    requesting_provider=(claim.get("provider") or {}).get("display"),
                )
            )
        return sort_newest_first(results)

    def conditions(self, member_id: str) -> list[dict[str, Any]]:
        patient_id = self._patient_id(member_id)
        if patient_id is None:
            return []
        return [
            {
                "code": ((c.get("code") or {}).get("coding") or [{}])[0].get("code"),
                "display": (c.get("code") or {}).get("text"),
                "clinical_status": ((c.get("clinicalStatus") or {}).get("coding") or [{}])[0].get(
                    "code"
                ),
                "onset": c.get("onsetDateTime"),
            }
            for c in self._entries(
                self.transport.get("Condition", {"subject": f"Patient/{patient_id}"})
            )
        ]

    def medications(self, member_id: str) -> list[dict[str, Any]]:
        patient_id = self._patient_id(member_id)
        if patient_id is None:
            return []
        return [
            {
                "medication": (m.get("medicationCodeableConcept") or {}).get("text"),
                "status": m.get("status"),
                "authored_on": m.get("authoredOn"),
                "instructions": (m.get("dosageInstruction") or [{}])[0].get("text"),
            }
            for m in self._entries(
                self.transport.get("MedicationRequest", {"subject": f"Patient/{patient_id}"})
            )
        ]
