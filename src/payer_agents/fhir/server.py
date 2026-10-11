"""A tiny FHIR R4 REST stub.

Enough of the search API to exercise :class:`~payer_agents.fhir.client.HttpFhirClient`
end to end locally: ``GET /fhir/{ResourceType}`` with the handful of search parameters
the tools use, returning a ``searchset`` Bundle.

It exists so the local demo can run the *networked* code path -- tool -> transport ->
HTTP -> FHIR -- rather than only the in-process shortcut. That is the path the egress
policy and the gateway sit on, so it is the one worth being able to demonstrate.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from payer_agents.fhir.store import FhirStore

#: Search parameter -> the resource field it filters on.
SEARCH_PARAMS: dict[str, dict[str, str]] = {
    "Patient": {"identifier": "__identifier__"},
    "Coverage": {"beneficiary": "beneficiary"},
    "Condition": {"subject": "subject", "patient": "subject"},
    "MedicationRequest": {"subject": "subject", "patient": "subject"},
    "ServiceRequest": {"subject": "subject", "patient": "subject"},
    "Claim": {"patient": "patient", "use": "__use__"},
    "ClaimResponse": {"request": "request", "patient": "patient"},
}


def as_bundle(resources: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "entry": [{"resource": resource} for resource in resources],
    }


def search(store: FhirStore, resource_type: str, params: dict[str, str]) -> dict[str, Any]:
    """Filter one resource type by the supported search parameters."""
    supported = SEARCH_PARAMS.get(resource_type)
    if supported is None:
        raise KeyError(resource_type)

    results = store.all_of(resource_type)
    for key, value in params.items():
        field = supported.get(key)
        if field is None or not value:
            continue
        if field == "__identifier__":
            results = [r for r in results if _has_identifier(r, value)]
        elif field == "__use__":
            results = [r for r in results if r.get("use") == value]
        else:
            results = [r for r in results if (r.get(field) or {}).get("reference") == value]
    return as_bundle(results)


def build_router(store: FhirStore) -> APIRouter:
    """FastAPI router mounted at ``/fhir`` by the demo app."""
    router = APIRouter(tags=["fhir"])

    @router.get("/metadata")
    def capability_statement() -> dict[str, Any]:
        return {
            "resourceType": "CapabilityStatement",
            "status": "active",
            "kind": "instance",
            "fhirVersion": "4.0.1",
            "format": ["application/fhir+json"],
            "software": {"name": "payer-agents synthetic FHIR stub"},
            "implementation": {"description": "Synthetic data only. Not a conformant FHIR server."},
            "rest": [
                {
                    "mode": "server",
                    "resource": [
                        {
                            "type": resource_type,
                            "interaction": [{"code": "search-type"}, {"code": "read"}],
                            "searchParam": [{"name": name, "type": "string"} for name in params],
                        }
                        for resource_type, params in SEARCH_PARAMS.items()
                    ],
                }
            ],
        }

    @router.get("/{resource_type}")
    def search_resources(resource_type: str, request: Request) -> dict[str, Any]:
        try:
            return search(store, resource_type, dict(request.query_params))
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"unsupported resource type: {resource_type}"
            ) from exc

    @router.get("/{resource_type}/{resource_id}")
    def read_resource(resource_type: str, resource_id: str) -> dict[str, Any]:
        resource = store.by_reference(f"{resource_type}/{resource_id}")
        if resource is None:
            raise HTTPException(status_code=404, detail="resource not found")
        return resource

    return router


def _has_identifier(resource: dict[str, Any], value: str) -> bool:
    wanted = value.split("|")[-1].upper()
    return any(
        str(identifier.get("value", "")).upper() == wanted
        for identifier in resource.get("identifier", [])
    )
