"""Idempotent OpenEMR **write** path for ingested clinical documents (PRP-05).

FHIR-native create is unavailable on this OpenEMR build (see
``PRPs/_spikes/PRP-00-result.md``), so persistence goes through the **Standard
REST API** (``/apis/default/api``). Two public coroutines:

* :func:`store_source` — stores the originating PDF as a patient document.
* :func:`persist_observations` — records the derived lab values as OpenEMR
  vitals bound to a single per-source *ingestion encounter*.

Both are **idempotent** (PRD FR-10): re-ingesting the same document does not
create duplicates.

Idempotency keys
----------------
* **Source document** — the file's SHA-256 is embedded in a deterministic
  filename (``copilot_<sha256>.<ext>``) inside a per-doc-type category folder.
  Before uploading we list the folder and skip if a document with that filename
  already exists (OpenEMR's own ``documents.hash`` column is SHA3-512, so the
  filename — which we control — is the reliable content key).
* **Ingestion encounter** — one encounter per source document, tagged in its
  ``reason`` with ``Clinical Co-Pilot ingest:<token>`` where ``token`` is
  derived from the source citation. Re-runs reuse the existing encounter.
* **Derived vital** — deduped by the exact ``note`` we write (``<test>: <value>
  <unit>``) under ``(pid, encounter)``; a re-run produces the same note and is
  skipped.

Contracts (base ``http://localhost:8300/apis/default/api``)
-----------------------------------------------------------
* Store source → ``POST /patient/:pid/document`` — multipart field **``document``**
  (NOT ``file``), with ``path`` as a **query-string** param. ``insertAtPath``
  returns only ``true``; the document id is recovered from
  ``GET /patient/:pid/document?path=<folder>`` (an empty folder answers 404).
* Persist derived → ``POST /patient/:puuid/encounter`` (reused per source), then
  ``POST /patient/:pid/encounter/:eid/vital`` for each derived value.

Failures surface as a typed :class:`OpenEmrWriteError` carrying an
:class:`~copilot.schemas.core.AgentError` (``retriable`` flagged) plus
``partial``/``missing``/``partial_results`` so nothing is silently dropped. A
correlation id is bound to every operation and no PHI is logged (only record
ids, resource types, status codes, and counts).

Local dev setup this module needs (one-time; performed for the LOCAL
``development-easy`` stack while building PRP-05)
-------------------------------------------------------------------------------
1. **Standard REST API enabled** — global ``rest_api=1`` (already set on the
   local stack; Admin → Config → Connectors → "Enable OpenEMR Standard REST
   API"). The ``admin`` password-grant user has the ``patients``/``docs`` and
   ``encounters`` write ACLs by default.
2. **Write-scoped OAuth client registered + enabled.** A confidential client was
   registered via ``POST /oauth2/default/registration`` with scopes::

       openid offline_access api:oemr
       user/patient.rs user/document.crs user/encounter.crus user/vital.crus

   OpenEMR registers clients **disabled**; it was enabled with
   ``UPDATE oauth_clients SET is_enabled=1 WHERE client_id=…`` against
   ``development-easy-mysql-1`` (``mariadb -uroot -proot openemr``). Note the
   write path uses the **Standard API** audience (``api:oemr``) — distinct from
   the Week-1 read client's FHIR audience (``api:fhir``); the two are separate
   tokens by design.
3. **Credentials saved to the gitignored ``agent/.env``** as
   ``OPENEMR_WRITE_CLIENT_ID`` / ``OPENEMR_WRITE_CLIENT_SECRET`` (never
   committed). ``OPENEMR_WRITE_SCOPES`` may override the default scope string.
"""

from __future__ import annotations

import hashlib
import mimetypes
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from copilot.config import Settings, get_settings
from copilot.documents.schemas import LabObservation, LabReport
from copilot.logging import (
    CORRELATION_ID_HEADER,
    current_correlation_id,
    get_logger,
    new_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)
from copilot.openemr.oauth import (
    ClientCredentials,
    OAuthError,
    TokenResponse,
    request_password_token,
    request_refresh_token,
)
from copilot.schemas.core import AgentError, SourceRef

__all__ = [
    "OpenEmrWriteError",
    "WriteTokenProvider",
    "OpenEmrRestClient",
    "OpenEmrWriter",
    "store_source",
    "persist_observations",
    "WRITE_SCOPES",
]

logger = get_logger(__name__)

# Standard-API audience + the create/read/update/search scopes the write path
# needs. `api:oemr` selects the Standard REST API (not FHIR). `user/patient.rs`
# is only for resolving the numeric pid <-> uuid, never for clinical reads.
WRITE_SCOPES = (
    "openid offline_access api:oemr "
    "user/patient.rs user/document.crs user/encounter.crus user/vital.crus"
)

_HTTP_TIMEOUT_SECONDS = 30.0
_RETRIABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# doc_type -> OpenEMR document category. Spaces are underscore-encoded because
# the route matches categories by name with spaces stripped (see
# DocumentService::getLastIdOfPath).
_CATEGORY_BY_DOC_TYPE: dict[str, str] = {
    "lab_report": "Lab_Report",
    "lab_pdf": "Lab_Report",
    "lab": "Lab_Report",
    "intake_form": "Patient_Information",
    "intake": "Patient_Information",
}
_DEFAULT_CATEGORY = "Medical_Record"

# Ingestion-encounter shape (office visit, ambulatory).
_ENCOUNTER_CATEGORY_ID = "5"  # openemr_postcalendar_categories office_visit
_ENCOUNTER_CLASS_CODE = "AMB"  # _ActEncounterCode: ambulatory
_INGEST_REASON_PREFIX = "Clinical Co-Pilot ingest:"

# Lab analytes that correspond to a real vitals column get written there as a
# numeric value; everything else is preserved verbatim in the vital `note`.
_VITAL_COLUMN_BY_TEST: dict[str, str] = {
    "weight": "weight",
    "body weight": "weight",
    "height": "height",
    "body height": "height",
    "bmi": "BMI",
    "body mass index": "BMI",
    "temperature": "temperature",
    "body temperature": "temperature",
    "pulse": "pulse",
    "heart rate": "pulse",
    "respiration": "respiration",
    "respiratory rate": "respiration",
    "systolic": "bps",
    "systolic blood pressure": "bps",
    "diastolic": "bpd",
    "diastolic blood pressure": "bpd",
    "oxygen saturation": "oxygen_saturation",
    "o2 saturation": "oxygen_saturation",
    "spo2": "oxygen_saturation",
    "waist circumference": "waist_circ",
    "head circumference": "head_circ",
}

_NOTE_MAX_LEN = 250  # OpenEMR validateVital caps `note` at 255 chars.


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OpenEmrWriteError(RuntimeError):
    """A Standard-API write to OpenEMR failed.

    Wraps a structured :class:`~copilot.schemas.core.AgentError` (the
    ``retriable`` flag distinguishes a transient failure from a permanent one)
    and, for partial persistence, records what *was* written
    (``partial_results``) and what could not be (``missing``) so the caller can
    build a ``ToolResult(partial=True)`` rather than lose data. Messages never
    contain PHI — only status codes and resource/operation names.
    """

    def __init__(
        self,
        error: AgentError,
        *,
        status_code: int | None = None,
        partial: bool = False,
        missing: list[str] | None = None,
        partial_results: list[SourceRef] | None = None,
    ) -> None:
        super().__init__(error.message)
        self.error = error
        self.status_code = status_code
        self.partial = partial
        self.missing = missing or []
        self.partial_results = partial_results or []

    @property
    def retriable(self) -> bool:
        return self.error.retriable


def _write_error(
    code: str,
    message: str,
    *,
    retriable: bool,
    status_code: int | None = None,
    partial: bool = False,
    missing: list[str] | None = None,
    partial_results: list[SourceRef] | None = None,
) -> OpenEmrWriteError:
    return OpenEmrWriteError(
        AgentError(code=code, message=message, retriable=retriable),
        status_code=status_code,
        partial=partial,
        missing=missing,
        partial_results=partial_results,
    )


# ---------------------------------------------------------------------------
# Write-client settings (read from the gitignored .env, config.py untouched)
# ---------------------------------------------------------------------------


class _WriteClientSettings(BaseSettings):
    """Write-scoped OAuth client creds, loaded from ``agent/.env``.

    Kept separate from the app-wide ``Settings`` so PRP-05 does not have to edit
    the shared ``config.py``; the write client is a distinct (Standard-API)
    credential from the Week-1 read client.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    openemr_write_client_id: str = ""
    openemr_write_client_secret: str = ""
    openemr_write_scopes: str = WRITE_SCOPES


# ---------------------------------------------------------------------------
# Token provider (Standard-API audience, write scopes)
# ---------------------------------------------------------------------------


class WriteTokenProvider:
    """Caches a user-bound, write-scoped token and refreshes it before expiry.

    Mirrors the Week-1 ``TokenProvider`` but pins the **write** scope string
    (``api:oemr`` + Standard-API resource scopes). Token acquisition is
    synchronous (a quick, cached round-trip); the REST client calls
    :meth:`get_access_token` from its header builder just like ``FhirClient``.
    """

    def __init__(
        self,
        username: str,
        password: str,
        *,
        credentials: ClientCredentials,
        scopes: str = WRITE_SCOPES,
        settings: Settings | None = None,
        refresh_skew_seconds: float = 60.0,
    ) -> None:
        self._username = username
        self._password = password
        self._credentials = credentials
        self._scopes = tuple(scopes.split())
        self._settings = settings or get_settings()
        self._skew = refresh_skew_seconds
        self._token: TokenResponse | None = None
        self._expires_at_monotonic: float = 0.0

    def get_access_token(self) -> str:
        if (
            self._token is not None
            and time.monotonic() < self._expires_at_monotonic
        ):
            return self._token.access_token

        if self._token is not None and self._token.refresh_token:
            try:
                self._store(
                    request_refresh_token(
                        self._token.refresh_token,
                        credentials=self._credentials,
                        settings=self._settings,
                    )
                )
                return self._token.access_token
            except OAuthError:
                logger.warning("openemr.write.refresh_failed_reauth")

        self._store(
            request_password_token(
                self._username,
                self._password,
                credentials=self._credentials,
                settings=self._settings,
                scopes=self._scopes,
            )
        )
        return self._token.access_token  # type: ignore[union-attr]

    def _store(self, token: TokenResponse) -> None:
        self._token = token
        self._expires_at_monotonic = time.monotonic() + token.expires_in - self._skew


# ---------------------------------------------------------------------------
# Async Standard-API REST client
# ---------------------------------------------------------------------------


def _tls_verify(url: str) -> bool:
    if not url.startswith("https://"):
        return True
    return not (
        url.startswith("https://localhost") or url.startswith("https://127.0.0.1")
    )


class OpenEmrRestClient:
    """Async client for the OpenEMR **Standard REST API** (``/apis/default/api``).

    Authenticates as the user (write-scoped bearer token), stamps
    ``X-Correlation-ID`` on every call, and retries only transient failures
    (network, 5xx, 429) with exponential backoff. Non-2xx responses raise a
    typed :class:`OpenEmrWriteError` (with the HTTP status) so callers can
    classify them; the caller owns the connection pool via the async context
    manager.
    """

    def __init__(
        self,
        token_provider: Any,
        *,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        max_attempts: int = 3,
    ) -> None:
        self._tokens = token_provider
        self._settings = settings or get_settings()
        self._base = f"{self._settings.openemr_base_url.rstrip('/')}/apis/default/api"
        self._client = client
        self._owns_client = client is None
        self._max_attempts = max_attempts

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT_SECONDS,
                verify=_tls_verify(self._base),
            )
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> OpenEmrRestClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _headers(self) -> dict[str, str]:
        try:
            token = self._tokens.get_access_token()
        except OAuthError as exc:
            raise _write_error(
                "openemr_auth_failed",
                "Could not obtain a write-scoped OpenEMR token",
                retriable=exc.retriable,
            ) from exc
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        cid = current_correlation_id()
        if cid:
            headers[CORRELATION_ID_HEADER] = cid
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        files: dict[str, Any] | None = None,
        expect_json: bool = True,
    ) -> Any:
        """Send a request (with transient retry) and return the parsed body.

        Raises :class:`OpenEmrWriteError` on a permanent failure or exhausted
        retries; ``error.status_code`` carries the HTTP status when present.
        """

        url = f"{self._base}/{path.lstrip('/')}"
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=0.2, max=2.0),
            retry=retry_if_exception(
                lambda exc: isinstance(exc, OpenEmrWriteError) and exc.retriable
            ),
            reraise=True,
        ):
            with attempt:
                return await self._send_once(
                    method,
                    url,
                    params=params,
                    json=json,
                    files=files,
                    expect_json=expect_json,
                )
        raise AssertionError("unreachable")  # pragma: no cover

    async def _send_once(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None,
        json: Any | None,
        files: dict[str, Any] | None,
        expect_json: bool,
    ) -> Any:
        headers = self._headers()
        try:
            resp = await self._http().request(
                method, url, params=params, json=json, files=files, headers=headers
            )
        except httpx.HTTPError as exc:
            logger.warning("openemr.write.transport_error", method=method)
            raise _write_error(
                "openemr_transport_error",
                "OpenEMR request failed to connect",
                retriable=True,
            ) from exc

        if resp.status_code >= 400:
            retriable = resp.status_code in _RETRIABLE_STATUS
            logger.warning(
                "openemr.write.http_error",
                method=method,
                status=resp.status_code,
                retriable=retriable,
            )
            raise _write_error(
                f"openemr_http_{resp.status_code}",
                f"OpenEMR {method} returned HTTP {resp.status_code}",
                retriable=retriable,
                status_code=resp.status_code,
            )

        logger.info("openemr.write.ok", method=method, status=resp.status_code)
        if not expect_json:
            return resp.text
        try:
            return resp.json()
        except ValueError as exc:
            raise _write_error(
                "openemr_bad_json",
                f"OpenEMR {method} response was not valid JSON",
                retriable=False,
                status_code=resp.status_code,
            ) from exc


def _unwrap(body: Any) -> Any:
    """Return the ``data`` payload from an enveloped response, else ``body``."""

    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def _extract(body: Any, key: str) -> Any:
    """Read ``key`` from a response that may or may not be ``data``-enveloped."""

    if isinstance(body, dict):
        if key in body:
            return body[key]
        data = body.get("data")
        if isinstance(data, dict):
            return data.get(key)
    return None


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def _category_for(doc_type: str) -> str:
    return _CATEGORY_BY_DOC_TYPE.get((doc_type or "").strip().lower(), _DEFAULT_CATEGORY)


def _numeric(value: str | None) -> str | None:
    """Return ``value`` if it parses as a plain number, else ``None``."""

    if value is None:
        return None
    candidate = value.strip().replace(",", "")
    try:
        float(candidate)
    except ValueError:
        return None
    return candidate


def _vital_note(obs: LabObservation) -> str:
    unit = f" {obs.unit}" if obs.unit else ""
    return f"{obs.test_name}: {obs.value}{unit}"[:_NOTE_MAX_LEN]


def _ingest_token(source_id: str) -> str:
    return hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:16]


class OpenEmrWriter:
    """Stateful writer over an :class:`OpenEmrRestClient` (one per operation)."""

    def __init__(self, client: OpenEmrRestClient) -> None:
        self._client = client

    async def __aenter__(self) -> OpenEmrWriter:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    # -- patient identity --------------------------------------------------

    async def _resolve_patient(self, patient_id: str) -> tuple[str, str]:
        """Return ``(numeric_pid, patient_uuid)`` for a patient uuid.

        Documents/vitals are keyed by the numeric pid; encounters by the uuid.
        """

        try:
            body = await self._client.request("GET", f"patient/{patient_id}")
        except OpenEmrWriteError as exc:
            if exc.status_code == 404:
                raise _write_error(
                    "patient_not_found",
                    "Patient not found for the supplied identifier",
                    retriable=False,
                    status_code=404,
                ) from exc
            raise
        data = _unwrap(body)
        if not isinstance(data, dict) or not data.get("id") or not data.get("uuid"):
            raise _write_error(
                "patient_unresolved",
                "OpenEMR patient response missing id/uuid",
                retriable=False,
            )
        return str(data["id"]), str(data["uuid"])

    # -- source document ---------------------------------------------------

    async def store_source(
        self, patient_id: str, file_path: str, doc_type: str
    ) -> SourceRef:
        path = Path(file_path)
        if not path.is_file():
            raise _write_error(
                "source_file_missing",
                "Source file does not exist",
                retriable=False,
            )
        content = path.read_bytes()
        sha256 = hashlib.sha256(content).hexdigest()
        suffix = path.suffix or ".bin"
        filename = f"copilot_{sha256}{suffix}"
        folder = _category_for(doc_type)

        pid, _ = await self._resolve_patient(patient_id)

        # Dedup: already stored (same SHA-256 in the deterministic filename)?
        existing = await self._list_documents(pid, folder)
        found = _find_document(existing, sha256)
        if found is not None:
            logger.info(
                "openemr.write.document_dedup", document_id=found, patient_pid=pid
            )
            return SourceRef(resource_type="Document", id=str(found))

        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        await self._client.request(
            "POST",
            f"patient/{pid}/document",
            params={"path": folder},
            files={"document": (filename, content, content_type)},
            expect_json=False,
        )

        # insertAtPath returns only `true`; recover the id from the listing.
        listing = await self._list_documents(pid, folder)
        new_id = _find_document(listing, sha256)
        if new_id is None:
            raise _write_error(
                "document_not_recovered",
                "Document uploaded but its id could not be recovered from listing",
                retriable=True,
            )
        logger.info("openemr.write.document_stored", document_id=new_id, patient_pid=pid)
        return SourceRef(resource_type="Document", id=str(new_id))

    async def _list_documents(self, pid: str, folder: str) -> list[dict[str, Any]]:
        try:
            body = await self._client.request(
                "GET", f"patient/{pid}/document", params={"path": folder}
            )
        except OpenEmrWriteError as exc:
            if exc.status_code == 404:  # empty folder → no documents
                return []
            raise
        data = _unwrap(body)
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    # -- derived observations ---------------------------------------------

    async def persist_observations(
        self, patient_id: str, report: LabReport
    ) -> list[SourceRef]:
        pid, puuid = await self._resolve_patient(patient_id)
        reason = f"{_INGEST_REASON_PREFIX}{_ingest_token(report.source.source_id)}"
        encounter_date = (report.report_date or date.today()).isoformat()

        eid = await self._reuse_or_create_encounter(puuid, reason, encounter_date)
        refs: list[SourceRef] = [SourceRef(resource_type="Encounter", id=str(eid))]

        existing = await self._list_vitals(pid, eid)
        existing_by_note = {
            v.get("note"): v.get("id") for v in existing if v.get("note")
        }

        missing: list[str] = []
        retriable_missing = False
        for obs in report.observations:
            if obs.value is None:  # explicit "not found" — nothing to persist
                continue
            note = _vital_note(obs)
            if note in existing_by_note:  # dedup: already persisted
                refs.append(
                    SourceRef(resource_type="Vitals", id=str(existing_by_note[note]))
                )
                continue
            try:
                vid = await self._post_vital(pid, eid, obs, note, encounter_date)
            except OpenEmrWriteError as exc:
                missing.append(obs.test_name)
                retriable_missing = retriable_missing or exc.retriable
                logger.warning(
                    "openemr.write.vital_failed",
                    encounter_id=eid,
                    status=exc.status_code,
                )
                continue
            refs.append(SourceRef(resource_type="Vitals", id=str(vid)))

        if missing:
            raise _write_error(
                "observations_partial",
                "Some derived observations could not be persisted",
                retriable=retriable_missing,
                partial=True,
                missing=missing,
                partial_results=refs,
            )

        logger.info(
            "openemr.write.observations_persisted",
            encounter_id=eid,
            vital_count=len(refs) - 1,
        )
        return refs

    async def _reuse_or_create_encounter(
        self, puuid: str, reason: str, encounter_date: str
    ) -> int:
        body = await self._client.request("GET", f"patient/{puuid}/encounter")
        rows = _unwrap(body)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("reason") == reason:
                    return int(row["eid"])

        created = await self._client.request(
            "POST",
            f"patient/{puuid}/encounter",
            json={
                "pc_catid": _ENCOUNTER_CATEGORY_ID,
                "class_code": _ENCOUNTER_CLASS_CODE,
                "reason": reason,
                "date": encounter_date,
            },
        )
        _raise_on_validation(created, "encounter")
        eid = _extract(created, "eid") or _extract(created, "encounter")
        if eid is None:
            raise _write_error(
                "encounter_not_created",
                "Encounter POST did not return an encounter id",
                retriable=True,
            )
        return int(eid)

    async def _list_vitals(self, pid: str, eid: int) -> list[dict[str, Any]]:
        try:
            body = await self._client.request(
                "GET", f"patient/{pid}/encounter/{eid}/vital"
            )
        except OpenEmrWriteError as exc:
            if exc.status_code == 404:
                return []
            raise
        data = _unwrap(body)
        return [v for v in data if isinstance(v, dict)] if isinstance(data, list) else []

    async def _post_vital(
        self,
        pid: str,
        eid: int,
        obs: LabObservation,
        note: str,
        encounter_date: str,
    ) -> int:
        obs_date = obs.collection_date.isoformat() if obs.collection_date else encounter_date
        payload: dict[str, Any] = {"date": obs_date, "note": note}
        column = _VITAL_COLUMN_BY_TEST.get(obs.test_name.strip().lower())
        numeric = _numeric(obs.value)
        if column is not None and numeric is not None:
            payload[column] = numeric
        body = await self._client.request(
            "POST", f"patient/{pid}/encounter/{eid}/vital", json=payload
        )
        _raise_on_validation(body, "vital")
        vid = _extract(body, "vid")
        if vid is None:
            raise _write_error(
                "vital_not_created",
                "Vital POST did not return a vital id",
                retriable=True,
            )
        return int(vid)


def _find_document(listing: list[dict[str, Any]], sha256: str) -> int | None:
    for doc in listing:
        name = str(doc.get("filename", ""))
        if sha256 in name and doc.get("id") is not None:
            return int(doc["id"])
    return None


def _raise_on_validation(body: Any, resource: str) -> None:
    if not isinstance(body, dict):
        return
    if body.get("validationErrors") or body.get("internalErrors"):
        raise _write_error(
            f"{resource}_rejected",
            f"OpenEMR rejected the {resource} write (validation/internal error)",
            retriable=False,
        )


# ---------------------------------------------------------------------------
# Default wiring + module-level convenience coroutines
# ---------------------------------------------------------------------------


def _default_writer() -> OpenEmrWriter:
    settings = get_settings()
    creds = _write_credentials()
    provider = WriteTokenProvider(
        settings.openemr_dev_user,
        settings.openemr_dev_pass,
        credentials=creds,
        scopes=_WriteClientSettings().openemr_write_scopes,
        settings=settings,
    )
    return OpenEmrWriter(OpenEmrRestClient(provider, settings=settings))


def _write_credentials() -> ClientCredentials:
    ws = _WriteClientSettings()
    if not ws.openemr_write_client_id or not ws.openemr_write_client_secret:
        raise _write_error(
            "write_client_unconfigured",
            "Write-scoped OpenEMR client not configured "
            "(set OPENEMR_WRITE_CLIENT_ID / OPENEMR_WRITE_CLIENT_SECRET in agent/.env)",
            retriable=False,
        )
    return ClientCredentials(ws.openemr_write_client_id, ws.openemr_write_client_secret)


async def store_source(
    patient_id: str,
    file_path: str,
    doc_type: str,
    *,
    writer: OpenEmrWriter | None = None,
) -> SourceRef:
    """Store a source PDF as a patient document (idempotent by SHA-256).

    See the module docstring for the full contract. On failure raises a typed
    :class:`OpenEmrWriteError`.
    """

    token = None if current_correlation_id() else set_correlation_id(new_correlation_id())
    try:
        if writer is not None:
            return await writer.store_source(patient_id, file_path, doc_type)
        async with _default_writer() as w:
            return await w.store_source(patient_id, file_path, doc_type)
    finally:
        if token is not None:
            reset_correlation_id(token)


async def persist_observations(
    patient_id: str,
    report: LabReport,
    *,
    writer: OpenEmrWriter | None = None,
) -> list[SourceRef]:
    """Persist a lab report's derived values as vitals under one ingestion
    encounter (idempotent). See the module docstring for the full contract.
    """

    token = None if current_correlation_id() else set_correlation_id(new_correlation_id())
    try:
        if writer is not None:
            return await writer.persist_observations(patient_id, report)
        async with _default_writer() as w:
            return await w.persist_observations(patient_id, report)
    finally:
        if token is not None:
            reset_correlation_id(token)
