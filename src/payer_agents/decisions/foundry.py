"""Microsoft Foundry decision-API client.

Targets ``POST {endpoint}/providers/microsoft/v1/systemone``. Authentication is
Microsoft Entra ID by preference (``DefaultAzureCredential`` against the
``https://cognitiveservices.azure.com/.default`` scope); an API key is accepted for
local experiments and discouraged everywhere else.

``azure-identity`` is imported lazily so that mock mode -- and therefore the test
suite and the demo -- never needs the Azure SDK installed.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

import httpx

from payer_agents.decisions.base import (
    DecisionClient,
    DecisionError,
    DecisionRequest,
    DecisionResponse,
    parse_decision_payload,
)

DECISION_PATH = "/providers/microsoft/v1/systemone"
TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"

#: HTTP statuses where another attempt is plausibly useful. 429 and 5xx only --
#: retrying a 400 just burns the member's patience.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class TokenProvider(Protocol):
    """Minimal slice of ``azure.identity`` we depend on, so it can be faked in tests."""

    def get_token(self, *scopes: str) -> Any: ...


class FoundryDecisionClient(DecisionClient):
    """Calls a deployed ``Microsoft-Decision-1`` (or compatible) deployment."""

    name = "foundry-decision"

    def __init__(
        self,
        endpoint: str,
        deployment: str,
        *,
        api_key: str | None = None,
        credential: TokenProvider | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
        sleep: Any = time.sleep,
    ) -> None:
        if not endpoint:
            raise ValueError("endpoint is required")
        self.endpoint = endpoint.rstrip("/")
        self.deployment = deployment
        self.api_key = api_key
        self._credential = credential
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)
        self.max_attempts = max(1, max_attempts)
        self.backoff_seconds = backoff_seconds
        self._sleep = sleep

    # -- auth ------------------------------------------------------------------

    def _credential_or_default(self) -> TokenProvider:
        if self._credential is None:
            try:
                from azure.identity import DefaultAzureCredential
            except ImportError as exc:  # pragma: no cover - depends on optional extra
                raise DecisionError(
                    "Entra ID auth needs the 'azure' extra: pip install '.[azure]', "
                    "or pass api_key for a key-based deployment."
                ) from exc
            self._credential = DefaultAzureCredential()
        return self._credential

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["api-key"] = self.api_key
            return headers
        token = self._credential_or_default().get_token(TOKEN_SCOPE)
        headers["Authorization"] = f"Bearer {getattr(token, 'token', token)}"
        return headers

    # -- call ------------------------------------------------------------------

    def decide(self, request: DecisionRequest) -> DecisionResponse:
        body = request.wire(self.deployment)
        url = f"{self.endpoint}{DECISION_PATH}"
        started = time.perf_counter()
        last_error: Exception | None = None

        for attempt in range(self.max_attempts):
            try:
                response = self._client.post(url, json=body, headers=self._headers())
            except httpx.HTTPError as exc:
                last_error = DecisionError(f"decision API unreachable: {exc}")
            else:
                if response.status_code < 400:
                    latency_ms = (time.perf_counter() - started) * 1000
                    return parse_decision_payload(
                        _json_or_error(response),
                        request,
                        provider="foundry",
                        latency_ms=latency_ms,
                    )
                last_error = DecisionError(
                    f"decision API returned HTTP {response.status_code}: "
                    f"{response.text[:280]}"
                )
                if response.status_code not in RETRYABLE_STATUS:
                    break

            if attempt < self.max_attempts - 1:
                self._sleep(self.backoff_seconds * (2**attempt))

        raise last_error or DecisionError("decision API call failed")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def _json_or_error(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise DecisionError("decision API returned invalid JSON") from exc
