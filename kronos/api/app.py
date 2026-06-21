"""FastAPI application — webhook ingestion, dashboard, and control endpoints."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi.responses import HTMLResponse

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query

from kronos.agent import IncidentStore, Orchestrator
from kronos.api.schemas import (
    DiagnosisResponse,
    IncidentCreatedResponse,
    IncidentListResponse,
    IncidentSummary,
    InitRequest,
    QuickIncidentRequest,
)
from kronos.config import Config, load_config
from kronos.integrations.github_client import GitHubClient
from kronos.logging_setup import setup_logging
from kronos.models import Incident, IncidentStatus, Priority

log = logging.getLogger("kronos.api")

# Module-level singletons populated in lifespan.
_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config(_state.get("config_path", "config.yaml"))
    setup_logging(config)
    store = IncidentStore()
    orchestrator = Orchestrator(config, store)
    _state.update(config=config, store=store, orchestrator=orchestrator)
    log.info("Kronos started")
    try:
        yield
    finally:
        await orchestrator.aclose()
        log.info("Kronos stopped")


app = FastAPI(title="Kronos", version="1.0", lifespan=lifespan)


@app.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard():
    """Serves the static HTML dashboard."""
    # This assumes dashboard.html is in the same directory as this app.py file.
    # Adjust the path if you place it in a templates/ or static/ folder.
    html_path = Path(__file__).parent / "dashboard.html" 
    
    try:
        with open(html_path, "r") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>Dashboard HTML file not found.</h1><p>Please create dashboard.html in the correct directory.</p>"

def _store() -> IncidentStore:
    return _state["store"]


def _orchestrator() -> Orchestrator:
    return _state["orchestrator"]


def _config() -> Config:
    return _state["config"]


def _summary(inc: Incident) -> IncidentSummary:
    return IncidentSummary(
        incident_id=inc.incident_id,
        status=inc.status.value,
        service=inc.service,
        declared_priority=(
            inc.declared_priority.value if inc.declared_priority else None
        ),
        resolved_priority=(
            inc.resolved_priority.value if inc.resolved_priority else None
        ),
        cache_result=inc.cache_result,
        pr_url=inc.pr_url,
        issue_url=inc.issue_url,
        created_at=inc.created_at,
    )


@app.post("/api/v1/init/", response_model=IncidentCreatedResponse)
async def init_incident(req: InitRequest, background: BackgroundTasks):
    """Primary Grafana webhook target. Returns immediately, runs in background."""
    inc = Incident(
        service=req.service,
        instance=req.instance,
        declared_priority=req.priority,
        error_logs=req.loki_logs,
        prometheus_logs=req.prometheus_logs,
    )
    _store().add(inc)
    background.add_task(_orchestrator().run, inc, fetch_loki=True)
    return IncidentCreatedResponse(incident_id=inc.incident_id, status="processing")


@app.post("/api/v1/quick-incident/", response_model=IncidentCreatedResponse)
async def quick_incident(req: QuickIncidentRequest, background: BackgroundTasks):
    """Simplified manual trigger; skips Loki and uses supplied logs."""
    inc = Incident(
        service=req.service,
        declared_priority=req.priority,
        error_logs=req.error_logs,
    )
    _store().add(inc)
    background.add_task(_orchestrator().run, inc, fetch_loki=False)
    return IncidentCreatedResponse(incident_id=inc.incident_id, status="processing")


@app.get("/api/v1/incidents/", response_model=IncidentListResponse)
async def list_incidents(
    status: str | None = Query(None),
    priority: str | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    items, total = _store().list(
        status=status, priority=priority, offset=offset, limit=limit
    )
    return IncidentListResponse(
        incidents=[_summary(i) for i in items],
        total=total,
        offset=offset,
        limit=limit,
    )


@app.get("/api/v1/incidents/{incident_id}/", response_model=IncidentSummary)
async def get_incident(incident_id: str):
    inc = _store().get(incident_id)
    if not inc:
        raise HTTPException(404, "incident not found")
    return _summary(inc)


@app.get("/api/v1/incidents/{incident_id}/diagnosis", response_model=DiagnosisResponse)
async def get_diagnosis(incident_id: str):
    inc = _store().get(incident_id)
    if not inc:
        raise HTTPException(404, "incident not found")
    d = inc.diagnosis
    return DiagnosisResponse(
        incident_id=incident_id,
        root_cause=d.root_cause if d else None,
        confidence=d.confidence if d else None,
        reasoning=d.reasoning if d else None,
        proposed_test=d.proposed_test if d else None,
        resolved_priority=(
            inc.resolved_priority.value if inc.resolved_priority else None
        ),
        from_cache=d.from_cache if d else False,
        code_context=[c.model_dump() for c in inc.chunks],
        trace=inc.trace,
    )


@app.post("/api/v1/incidents/{incident_id}/approve")
async def approve(incident_id: str, background: BackgroundTasks):
    inc = _store().get(incident_id)
    if not inc:
        raise HTTPException(404, "incident not found")
    if not inc.diagnosis:
        raise HTTPException(409, "incident has no diagnosis yet")
    inc.resolved_priority = inc.resolved_priority or Priority.MEDIUM
    background.add_task(_orchestrator()._auto_fix_path, inc, inc.diagnosis)
    return {"incident_id": incident_id, "status": "fixing"}


@app.post("/api/v1/incidents/{incident_id}/ignore")
async def ignore(incident_id: str):
    inc = _store().get(incident_id)
    if not inc:
        raise HTTPException(404, "incident not found")
    inc.status = IncidentStatus.IGNORED
    inc.log("Ignored by operator")
    _store().update(inc)
    return {"incident_id": incident_id, "status": "ignored"}


@app.get("/api/v1/health")
async def health():
    cfg = _config()
    gh = GitHubClient(cfg)
    checks = {
        "github": bool(gh.token),
        "loki": bool(cfg.loki.get("url")),
        "gemini": bool(cfg.claude.get("api_key")),
        "parcle": bool(cfg.parcle.get("api_key")),
    }
    ok = all(checks.values())
    return {"status": "ok" if ok else "degraded", "checks": checks, "time": time.time()}


@app.get("/api/v1/rules")
async def rules():
    """Expose current priority rule patterns for the dashboard."""
    return _config().get("rules.priority", {})
