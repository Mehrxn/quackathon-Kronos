"""API request/response schemas."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from kronos.models import Priority


class InitRequest(BaseModel):
    """Primary Grafana webhook body."""

    prometheus_logs: dict[str, Any] = Field(default_factory=dict)
    loki_logs: list[str] = Field(default_factory=list)
    timestamp: Optional[float] = None
    priority: Optional[Priority] = None
    service: str = ""
    instance: str = ""


class QuickIncidentRequest(BaseModel):
    """Simplified manual trigger for demos."""

    error_logs: list[str]
    priority: Optional[Priority] = None
    service: str = "demo-service"


class IncidentCreatedResponse(BaseModel):
    incident_id: str
    status: str


class IncidentSummary(BaseModel):
    incident_id: str
    status: str
    service: str
    declared_priority: Optional[str] = None
    resolved_priority: Optional[str] = None
    cache_result: Optional[str] = None
    pr_url: Optional[str] = None
    issue_url: Optional[str] = None
    created_at: float


class IncidentListResponse(BaseModel):
    incidents: list[IncidentSummary]
    total: int
    offset: int
    limit: int


class DiagnosisResponse(BaseModel):
    incident_id: str
    root_cause: Optional[str] = None
    confidence: Optional[float] = None
    reasoning: Optional[str] = None
    proposed_test: Optional[str] = None
    resolved_priority: Optional[str] = None
    from_cache: bool = False
    code_context: list[dict] = Field(default_factory=list)
    trace: list[str] = Field(default_factory=list)
