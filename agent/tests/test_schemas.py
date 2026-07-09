"""Contract tests for the base Pydantic schemas (PRP M0-5).

Each case exercises a boundary (missing / malformed fields), an invariant
(grounding sources, the partial/missing degradation contract), or a
round-trip, per NFR-3: the schemas are the source of truth and malformed
model output is rejected at the schema layer.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import BaseModel, ValidationError

from copilot.schemas.core import AgentError, SourceRef, TokenResponse, ToolResult
from copilot.schemas.patient import Patient


# --- SourceRef -------------------------------------------------------------


def test_sourceref_parses_valid_payload() -> None:
    ref = SourceRef(resource_type="Observation", id="obs-1")
    assert ref.resource_type == "Observation"
    assert ref.id == "obs-1"
    assert ref.timestamp is None


def test_sourceref_round_trips() -> None:
    ts = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
    original = SourceRef(resource_type="Condition", id="c-9", timestamp=ts)
    restored = SourceRef.model_validate(original.model_dump())
    assert restored == original
    # JSON round-trip too (timestamp survives serialization).
    assert SourceRef.model_validate_json(original.model_dump_json()) == original


def test_sourceref_is_frozen_and_hashable() -> None:
    ref = SourceRef(resource_type="Patient", id="p-1")
    assert ref in {ref}  # hashable
    with pytest.raises(ValidationError):
        ref.id = "p-2"  # type: ignore[misc]


@pytest.mark.parametrize(
    "payload",
    [
        {"resource_type": "", "id": "x"},  # empty resource_type
        {"resource_type": "Patient", "id": ""},  # empty id
        {"resource_type": "Patient"},  # missing id
        {"resource_type": "Patient", "id": "p", "extra": 1},  # unexpected key
    ],
)
def test_sourceref_rejects_invalid(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SourceRef.model_validate(payload)


# --- TokenResponse ---------------------------------------------------------


def test_tokenresponse_parses_valid_payload() -> None:
    tok = TokenResponse(
        access_token="abc",
        token_type="Bearer",
        expires_in=3600,
        scope="openid patient/*.read",
    )
    assert tok.refresh_token is None
    assert tok.expires_in == 3600


@pytest.mark.parametrize(
    "payload",
    [
        {"token_type": "Bearer", "expires_in": 3600, "scope": "s"},  # no access_token
        {"access_token": "a", "token_type": "Bearer", "expires_in": 0, "scope": "s"},  # expires<=0
        {"access_token": "a", "token_type": "Bearer", "expires_in": -1, "scope": "s"},
    ],
)
def test_tokenresponse_rejects_invalid(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TokenResponse.model_validate(payload)


# --- AgentError ------------------------------------------------------------


def test_agenterror_parses_and_is_frozen() -> None:
    err = AgentError(code="OPENEMR_TIMEOUT", message="upstream timed out", retriable=True)
    assert err.retriable is True
    with pytest.raises(ValidationError):
        err.code = "OTHER"  # type: ignore[misc]


def test_agenterror_requires_retriable() -> None:
    with pytest.raises(ValidationError):
        AgentError.model_validate({"code": "X", "message": "m"})


# --- ToolResult ------------------------------------------------------------


class _Payload(BaseModel):
    value: int


def test_toolresult_defaults_are_complete_answer() -> None:
    result: ToolResult[_Payload] = ToolResult(data=_Payload(value=1))
    assert result.partial is False
    assert result.missing == []
    assert result.sources == []
    assert isinstance(result.retrieved_at, datetime)


def test_toolresult_carries_partial_and_missing() -> None:
    """FR-11: a tool can return what it has and name what it couldn't get."""
    result: ToolResult[_Payload] = ToolResult(
        data=_Payload(value=2),
        sources=[SourceRef(resource_type="Observation", id="o-1")],
        partial=True,
        missing=["medications", "allergies"],
    )
    assert result.partial is True
    assert result.missing == ["medications", "allergies"]
    assert result.sources[0].id == "o-1"


def test_toolresult_validates_generic_payload() -> None:
    with pytest.raises(ValidationError):
        ToolResult[_Payload].model_validate({"data": {"value": "not-an-int"}})


# --- Patient ---------------------------------------------------------------


def _valid_patient_payload() -> dict[str, object]:
    return {
        "id": "p-1",
        "name": "Jane Doe",
        "dob": "1990-04-01",
        "sex": "female",
        "source": {"resource_type": "Patient", "id": "p-1"},
    }


def test_patient_parses_valid_payload() -> None:
    patient = Patient.model_validate(_valid_patient_payload())
    assert patient.dob == date(1990, 4, 1)
    assert patient.sex == "female"
    assert patient.source.resource_type == "Patient"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(sex="martian"),  # not in the value set
        lambda p: p.update(dob="not-a-date"),  # unparseable date
        lambda p: p.pop("source"),  # missing grounding
        lambda p: p.update(name=""),  # empty name
        lambda p: p.update(unexpected=1),  # extra key
    ],
)
def test_patient_rejects_invalid(mutate) -> None:
    payload = _valid_patient_payload()
    mutate(payload)
    with pytest.raises(ValidationError):
        Patient.model_validate(payload)
