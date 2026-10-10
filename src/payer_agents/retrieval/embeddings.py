"""Embeddings.

Mock mode uses a hashed bag-of-words projection. It is not semantic -- it will not
match "heart attack" to "myocardial infarction" -- but it is deterministic, needs no
model, and is enough to demonstrate that hybrid retrieval fuses two signals. The
synthetic corpus is written with that limitation in mind, and the README is explicit
that semantic recall is the first thing you lose in mock mode.
"""

from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod
from collections import Counter

from payer_agents.decisions.mock import tokenize

DEFAULT_DIMENSIONS = 256


class Embedder(ABC):
    """Turn text into a unit-length vector."""

    name: str = "embedder"
    dimensions: int = DEFAULT_DIMENSIONS

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Embed one string."""

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]


class HashingEmbedder(Embedder):
    """Deterministic hashed bag-of-words with sublinear term weighting."""

    name = "hashing"

    def __init__(self, dimensions: int = DEFAULT_DIMENSIONS) -> None:
        if dimensions < 8:
            raise ValueError("dimensions must be >= 8")
        self.dimensions = dimensions

    def _bucket(self, token: str) -> tuple[int, float]:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        # Second bit decides the sign, which keeps unrelated collisions from always
        # reinforcing each other.
        return value % self.dimensions, 1.0 if (value >> 1) & 1 else -1.0

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        counts = Counter(tokenize(text))
        for token, count in counts.items():
            index, sign = self._bucket(token)
            vector[index] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            return vector
        return [v / norm for v in vector]


class AzureOpenAIEmbedder(Embedder):
    """Calls an embedding deployment on a Foundry resource.

    Kept intentionally thin: the only reason this class exists is to prove the
    :class:`Embedder` seam is real. Production would batch, retry and cache.
    """

    name = "azure-openai"

    def __init__(
        self,
        endpoint: str,
        deployment: str,
        *,
        api_key: str | None = None,
        api_version: str = "2024-10-21",
        client: object | None = None,
        dimensions: int = 1536,
    ) -> None:
        import httpx  # local import keeps module import cheap

        self.endpoint = endpoint.rstrip("/")
        self.deployment = deployment
        self.api_key = api_key
        self.api_version = api_version
        self.dimensions = dimensions
        self._client = client or httpx.Client(timeout=30.0)

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        url = (
            f"{self.endpoint}/openai/deployments/{self.deployment}/embeddings"
            f"?api-version={self.api_version}"
        )
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["api-key"] = self.api_key
        response = self._client.post(url, json={"input": texts}, headers=headers)
        response.raise_for_status()
        payload = response.json()
        return [item["embedding"] for item in payload["data"]]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity for already-normalised vectors, guarded for zero norms."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
