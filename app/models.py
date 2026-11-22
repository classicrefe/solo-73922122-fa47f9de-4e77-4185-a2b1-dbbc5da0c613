"""请求/响应模型（Pydantic v2）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ObservationIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    obs_id: str = Field(min_length=1, max_length=64, description="观测号")
    county: str = Field(min_length=1, max_length=64, description="县")
    species: str = Field(min_length=1, max_length=64, description="鸟种")
    team: str = Field(min_length=1, max_length=64, description="调查队")
    plot: str = Field(min_length=1, max_length=64, description="样地")


class BatchSubmission(BaseModel):
    batch_no: str = Field(min_length=1, max_length=64, description="批号")
    observations: list[ObservationIn] = Field(min_length=1, max_length=10000)


class RegistryChange(BaseModel):
    county: str
    species: str
    action: str
    evidence_no: str | None = None
    old_evidence_no: str | None = None


class BatchSubmitResponse(BaseModel):
    batch_no: str
    status: Literal["active", "withdrawn"]
    accepted: int
    replayed: bool
    registry_changes: list[dict[str, Any]]
    message: str | None = None


class BatchWithdrawResponse(BaseModel):
    batch_no: str
    status: Literal["withdrawn"]
    replayed: bool
    registry_changes: list[dict[str, Any]]
    message: str | None = None


class ObservationOut(BaseModel):
    obs_id: str
    county: str
    species: str
    team: str
    plot: str


class BatchDetail(BaseModel):
    batch_no: str
    status: Literal["active", "withdrawn"]
    received_at: str
    withdrawn_at: str | None
    observations: list[ObservationOut]


class BatchSummary(BaseModel):
    batch_no: str
    status: str
    received_at: str
    withdrawn_at: str | None
    observation_count: int


class RegistryEntry(BaseModel):
    county: str
    species: str
    evidence_no: str
    listed_at: str
    evidence_status: str


class EvidenceObservation(BaseModel):
    batch_no: str
    obs_id: str
    county: str
    species: str
    team: str
    plot: str
    batch_status: str


class EvidenceDetail(BaseModel):
    evidence_no: str
    county: str
    species: str
    status: Literal["valid", "invalid"]
    replaced_by: str | None
    created_at: str
    observations: list[EvidenceObservation]
    all_source_batches_active: bool
