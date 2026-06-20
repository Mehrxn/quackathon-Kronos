"""Core data models shared across the agent pipeline."""
from __future__ import annotations

import enum
import time
import uuid
from typing import Any, Optional

from pydantic import BaseModel, Field


class Priority(str, enum.Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"high": 3, "medium": 2, "low": 1}[self.value]


class IncidentStatus(str, enum.Enum):
    PROCESSING = "processing"
    DIAGNOSING = "diagnosing"
    AWAITING_APPROVAL = "awaiting_approval"
    FIXING = "fixing"
    PR_OPENED = "pr_opened"
    ISSUE_OPENED = "issue_opened"
    RESOLVED = "resolved"
    FAILED = "failed"
    IGNORED = "ignored"


# --- Retrieval models -------------------------------------------------------

class ErrorPattern(BaseModel):
    """A parsed error from a log line (Phase 1 output)."""
    function: str
    error_type: str
    keywords: list[str] = Field(default_factory=list)
    raw_line: str = ""
    priority_hint: Optional[Priority] = None

    def fingerprint_part(self) -> tuple[str, str]:
        return (self.function, self.error_type)


class CodeChunk(BaseModel):
    """A unit of extracted source context (Phase 3 output)."""
    file: str
    start_line: int
    end_line: int
    content: str
    category: str  # definition | caller | keyword | git_change | error_log
    score: float = 0.0
    function: Optional[str] = None

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.file, self.start_line, self.end_line)

    def approx_tokens(self) -> int:
        # ~4 chars/token heuristic, good enough for budgeting
        return max(1, len(self.content) // 4)


class Diagnosis(BaseModel):
    """LLM diagnosis output (Phase: single CoT call)."""
    root_cause: str
    reasoning: str = ""
    confidence: float = 0.0
    priority: Priority = Priority.LOW
    proposed_test: str = ""
    fix_summary: str = ""
    from_cache: bool = False
    fuzzy_hint_used: bool = False


# --- Incident model ---------------------------------------------------------

class Incident(BaseModel):
    incident_id: str = Field(default_factory=lambda: f"inc_{uuid.uuid4().hex[:12]}")
    created_at: float = Field(default_factory=time.time)
    status: IncidentStatus = IncidentStatus.PROCESSING

    service: str = ""
    instance: str = ""
    declared_priority: Optional[Priority] = None  # priority sent in webhook
    resolved_priority: Optional[Priority] = None   # after reconciliation

    error_logs: list[str] = Field(default_factory=list)
    prometheus_logs: dict[str, Any] = Field(default_factory=dict)

    patterns: list[ErrorPattern] = Field(default_factory=list)
    chunks: list[CodeChunk] = Field(default_factory=list)
    diagnosis: Optional[Diagnosis] = None

    fingerprint: Optional[str] = None
    cache_result: Optional[str] = None  # exact | fuzzy | miss

    pr_url: Optional[str] = None
    issue_url: Optional[str] = None
    error: Optional[str] = None

    # mutable log of pipeline events for the dashboard reasoning trace
    trace: list[str] = Field(default_factory=list)

    def log(self, msg: str) -> None:
        self.trace.append(f"{time.strftime('%H:%M:%S')} {msg}")
