"""Authenticated, correlation-tagged FHIR HTTP client (PRP M0-7).

:class:`FhirClient` is the single doorway through which the agent reads from
OpenEMR's FHIR API. Every request it makes:

* carries the **user-bound** bearer token from an M0-4 ``TokenProvider`` so the
  read happens *as the logged-in clinician* (borrowed-identity access, FR-3),
  never as a standing privileged credential;
* stamps ``X-Correlation-ID`` (the active request's ID from ``copilot.logging``)
  so the call is traceable end-to-end and lands in OpenEMR's ``api_log``
  (FR-14);
* retries only **transient** failures (network errors, 5xx, 429) with
  exponential backoff via ``tenacity`` — permanent 4xx failures fail fast.

The client is ``async`` (httpx.AsyncClient) because the orchestrator fans out
several retrieval tools concurrently in M1. Token acquisition is delegated to
the (synchronous) ``TokenProvider``; its own caching means the common path is a
cheap in-memory read, not a network round-trip.

Secrets are never logged — only the request method, path, status, and
correlation ID.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from copilot.config import Settings, get_settings
from copilot.logging import (
    CORRELATION_ID_HEADER,
    current_correlation_id,
    get_logger,
)

__all__ = ["FhirError", "TokenSource", "FhirClient"]

logger = get_logger(__name__)

_HTTP_TIMEOUT_SECONDS = 30.0
_RETRIABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class TokenSource(Protocol):
    """Anything that can vend a fresh bearer access token.

    The M0-4 :class:`~copilot.openemr.oauth.TokenProvider` satisfies this; tests
    supply a trivial stub.
    """

    def get_access_token(self) -> str:  # pragma: no cover - structural type
        ...


class FhirError(RuntimeError):
    """A FHIR request to OpenEMR failed.

    ``retriable`` marks a transient failure (network blip, 5xx, 429) worth a
    retry versus a permanent one (404, 401, malformed request). Messages never
    contain response bodies (which could carry PHI) — only status + method/path.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retriable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retriable = retriable


def _tls_verify(url: str) -> bool:
    """Whether to verify TLS for ``url`` (self-signed localhost is exempted)."""

    if not url.startswith("https://"):
        return True
    return not (
        url.startswith("https://localhost") or url.startswith("https://127.0.0.1")
    )


class FhirClient:
    """Async FHIR client that authenticates as the user and traces every call.

    Use as an async context manager so the underlying connection pool is closed::

        async with FhirClient(token_provider) as fhir:
            bundle = await fhir.get("/Patient", params={"_count": 1})

    ``client`` may be injected (tests pass an httpx client wired to a mock
    transport); otherwise one is created lazily and owned by this instance.
    """

    def __init__(
        self,
        token_provider: TokenSource,
        *,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        max_attempts: int = 3,
    ) -> None:
        self._tokens = token_provider
        self._settings = settings or get_settings()
        self._base = self._settings.openemr_fhir_base.rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._max_attempts = max_attempts

    # -- lifecycle ---------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT_SECONDS,
                verify=_tls_verify(self._base),
            )
        return self._client

    async def aclose(self) -> None:
        """Close the owned connection pool (no-op for an injected client)."""

        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> FhirClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- requests ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """Bearer token + correlation ID + FHIR JSON negotiation."""

        headers = {
            "Authorization": f"Bearer {self._tokens.get_access_token()}",
            "Accept": "application/fhir+json",
        }
        cid = current_correlation_id()
        if cid:
            headers[CORRELATION_ID_HEADER] = cid
        return headers

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET ``{FHIR base}{path}`` and return the parsed JSON body.

        Retries transient failures with exponential backoff; raises
        :class:`FhirError` on a permanent failure or when retries are exhausted.
        """

        url = f"{self._base}/{path.lstrip('/')}"

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=0.2, max=2.0),
            retry=retry_if_exception(
                lambda exc: isinstance(exc, FhirError) and exc.retriable
            ),
            reraise=True,
        ):
            with attempt:
                return await self._get_once(url, params=params)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _get_once(
        self,
        url: str,
        *,
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """A single GET attempt: send, classify failures, parse JSON."""

        try:
            resp = await self._http().get(url, params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            logger.warning("openemr.fhir.transport_error", url=url)
            raise FhirError(
                "FHIR request failed to connect", retriable=True
            ) from exc

        if resp.status_code >= 400:
            retriable = resp.status_code in _RETRIABLE_STATUS
            logger.warning(
                "openemr.fhir.http_error",
                status=resp.status_code,
                url=url,
                retriable=retriable,
            )
            raise FhirError(
                f"FHIR request to {url} returned HTTP {resp.status_code}",
                status_code=resp.status_code,
                retriable=retriable,
            )

        try:
            body = resp.json()
        except ValueError as exc:
            raise FhirError(
                f"FHIR response from {url} was not valid JSON",
                status_code=resp.status_code,
            ) from exc

        if not isinstance(body, dict):
            raise FhirError(
                f"FHIR response from {url} was not a JSON object",
                status_code=resp.status_code,
            )
        logger.info("openemr.fhir.ok", status=resp.status_code, url=url)
        return body
