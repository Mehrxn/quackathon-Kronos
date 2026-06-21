import time
import random
import uuid
import logging
import sys
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from prometheus_client import Counter, Histogram, Gauge, generate_latest, REGISTRY
import json

# Setup structured logging for Loki
class StructuredFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
        }
        if hasattr(record, "extra_fields"):
            log_record.update(record.extra_fields)
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_record)

# Configure logger
logger = logging.getLogger("ai_agent")
logger.setLevel(logging.INFO)

# Console handler with structured logging
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(StructuredFormatter())
logger.addHandler(console_handler)

# Prometheus metrics
REQUEST_COUNT = Counter(
    'ai_agent_requests_total',
    'Total AI agent requests',
    ['method', 'endpoint', 'status']
)

RESPONSE_TIME = Histogram(
    'ai_agent_response_time_seconds',
    'Response time in seconds',
    ['endpoint'],
    buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0]
)

ACTIVE_SESSIONS = Gauge(
    'ai_agent_active_sessions',
    'Number of active sessions'
)

AGENT_THINKING_TIME = Histogram(
    'ai_agent_thinking_time_seconds',
    'AI agent thinking time',
    buckets=[0.1, 0.5, 1.0, 2.0, 3.0, 5.0]
)

TOKEN_USAGE = Counter(
    'ai_agent_tokens_total',
    'Total tokens used by AI agent',
    ['type']  # input/output
)

ERROR_COUNTER = Counter(
    'ai_agent_errors_total',
    'Total errors',
    ['error_type']
)

SYSTEM_MEMORY_USAGE = Gauge(
    'ai_agent_memory_usage_bytes',
    'Simulated RAM usage in bytes'
)

SYSTEM_CPU_USAGE = Gauge(
    'ai_agent_cpu_usage_ratio',
    'Simulated CPU usage ratio (0.0 to 1.0)'
)

# In-memory session store
active_sessions = {}

# Pydantic models
class QueryRequest(BaseModel):
    query: str
    session_id: str | None = None

class QueryResponse(BaseModel):
    session_id: str
    query: str
    response: str
    thinking_time: float
    tokens_used: int
    confidence: float
    timestamp: str

# Simulated AI responses
MOCK_RESPONSES = [
    "Based on my analysis, the data shows a positive trend in user engagement.",
    "I've processed your query and found 3 relevant patterns in the dataset.",
    "The AI model predicts a 15% increase in efficiency with the proposed changes.",
    "After analyzing the input, I recommend implementing solution B for optimal results.",
    "My neural network has identified key correlations that suggest a strong relationship.",
    "Processing complete. The sentiment analysis shows 78% positive feedback.",
    "I've generated insights from the data. Here are the top findings...",
    "The recommendation engine suggests prioritizing items with high confidence scores.",
]

def simulate_ai_thinking():
    """Simulate AI processing time"""
    time.sleep(random.uniform(0.5, 2.0))

def log_event(event_type: str, extra_fields: dict, level: str = "info"):
    """Log structured event for Loki"""
    if level == "warning":
        logger.warning(event_type, extra={"extra_fields": extra_fields})
    elif level == "error":
        logger.error(event_type, extra={"extra_fields": extra_fields})
    else:
        logger.info(event_type, extra={"extra_fields": extra_fields})

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("AI Agent backend starting up", extra={"extra_fields": {"event": "startup"}})
    yield
    logger.info("AI Agent backend shutting down", extra={"extra_fields": {"event": "shutdown"}})

app = FastAPI(
    title="AI Agent Backend",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request tracking middleware
# Request tracking middleware
@app.middleware("http")
async def track_requests(request: Request, call_next):
    start_time = time.time()
    
    # Define paths we don't want to spam in our logs
    skip_logging = request.url.path in ["/metrics", "/health"]
    
    # Log incoming request (only if not skipped)
    if not skip_logging:
        log_event("request_started", {
            "method": request.method,
            "path": request.url.path,
            "client_ip": request.client.host
        })
    
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception as e:
        status = 500
        response = JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )
    
    duration = time.time() - start_time
    
    # We still want Prometheus to TRACK the metrics for these endpoints
    REQUEST_COUNT.labels(
        method=request.method,
        endpoint=request.url.path,
        status=status
    ).inc()
    
    RESPONSE_TIME.labels(endpoint=request.url.path).observe(duration)
    
    # Log response (only if not skipped)
    if not skip_logging:
        log_event("request_completed", {
            "method": request.method,
            "path": request.url.path,
            "status": status,
            "duration_ms": duration * 1000
        })
    
    return response
@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint"""
    return Response(generate_latest(REGISTRY), media_type="text/plain")

@app.post("/api/agent/query", response_model=QueryResponse)
async def agent_query(request: QueryRequest):
    """Main AI agent endpoint"""
    start_time = time.time()
    session_id = request.session_id or str(uuid.uuid4())
    
    # Log query received
    log_event("query_received", {
        "session_id": session_id,
        "query_length": len(request.query),
        "query_preview": request.query[:100]
    })
    
    # Validate input
    if not request.query.strip():
        ERROR_COUNTER.labels(error_type="empty_query").inc()
        log_event("error", {
            "session_id": session_id,
            "error": "Empty query received"
        })
        raise HTTPException(status_code=400, detail="Query cannot be empty")
    
    try:
        # Simulate AI processing
        thinking_start = time.time()
        simulate_ai_thinking()
        thinking_time = time.time() - thinking_start
        
        AGENT_THINKING_TIME.observe(thinking_time)
        
        # Generate mock response
        response_text = random.choice(MOCK_RESPONSES)
        input_tokens = len(request.query.split())
        output_tokens = len(response_text.split())
        
        # Track token usage
        TOKEN_USAGE.labels(type="input").inc(input_tokens)
        TOKEN_USAGE.labels(type="output").inc(output_tokens)
        
        # Calculate mock confidence
        confidence = random.uniform(0.75, 0.99)
        
        # Update session
        if session_id not in active_sessions:
            active_sessions[session_id] = {
                "created_at": datetime.utcnow(),
                "query_count": 0
            }
            ACTIVE_SESSIONS.inc()
        
        active_sessions[session_id]["query_count"] += 1
        active_sessions[session_id]["last_query"] = datetime.utcnow()
        
        total_time = time.time() - start_time
        
        # Log successful response
        log_event("query_completed", {
            "session_id": session_id,
            "thinking_time_ms": thinking_time * 1000,
            "total_time_ms": total_time * 1000,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "confidence": confidence
        })
        
        return QueryResponse(
            session_id=session_id,
            query=request.query,
            response=response_text,
            thinking_time=thinking_time,
            tokens_used=input_tokens + output_tokens,
            confidence=confidence,
            timestamp=datetime.utcnow().isoformat()
        )
        
    except Exception as e:
        ERROR_COUNTER.labels(error_type="processing_error").inc()
        log_event("error", {
            "session_id": session_id,
            "error": str(e),
            "error_type": type(e).__name__
        })
        raise HTTPException(status_code=500, detail="AI agent processing error")

@app.post("/api/trigger/ram")
async def trigger_ram():
    bytes_used = int(7.8 * 1024 * 1024 * 1024)
    SYSTEM_MEMORY_USAGE.set(bytes_used)
    log_event("resource_warning", {
        "status": "critical",
        "resource": "ram",
        "used_gb": 7.8,
        "limit_gb": 8.0,
        "message": "HIGH RAM ALERT: System memory usage is critically high (97.5% capacity). Please allocate more resources or terminate active sessions."
    }, level="warning")
    return {"status": "triggered", "message": "Simulated RAM spike triggered."}

@app.post("/api/trigger/cpu")
async def trigger_cpu():
    SYSTEM_CPU_USAGE.set(0.95)
    log_event("resource_warning", {
        "status": "critical",
        "resource": "vcpu",
        "utilization": 0.95,
        "message": "HIGH CPU ALERT: vCPU utilization is critically high (95.0%). CPU throttling may occur."
    }, level="warning")
    return {"status": "triggered", "message": "Simulated vCPU spike triggered."}

@app.post("/api/trigger/segfault")
async def trigger_segfault():
    ERROR_COUNTER.labels(error_type="segfault").inc()
    log_event("fatal_error", {
        "status": "crash",
        "signal": "SIGSEGV",
        "error_type": "segfault",
        "code": 139,
        "message": "CRITICAL FATAL ERROR: Segmentation fault (SIGSEGV) at address 0x0000000000000030. Memory dereference failed. Process terminating..."
    }, level="error")
    return {"status": "triggered", "message": "Simulated Segfault crash triggered."}

@app.post("/api/trigger/outofindex")
async def trigger_outofindex():
    ERROR_COUNTER.labels(error_type="outofindex").inc()
    log_event("runtime_error", {
        "status": "error",
        "error_type": "outofindex",
        "exception": "IndexError",
        "message": "IndexError: list index out of range. Exception in thread 'main' java.lang.IndexOutOfBoundsException: Index 15 out of bounds for length 10. Attempted to read invalid offset."
    }, level="error")
    return {"status": "triggered", "message": "Simulated Out of Index exception triggered."}

@app.get("/api/agent/sessions")
async def get_sessions():
    """Get active sessions"""
    session_data = []
    for session_id, data in active_sessions.items():
        session_data.append({
            "session_id": session_id,
            "query_count": data["query_count"],
            "created_at": data["created_at"].isoformat(),
            "last_query": data.get("last_query", data["created_at"]).isoformat()
        })
    
    log_event("sessions_listed", {"active_sessions": len(session_data)})
    return {"active_sessions": len(session_data), "sessions": session_data}

@app.delete("/api/agent/sessions/{session_id}")
async def close_session(session_id: str):
    """Close a session"""
    if session_id in active_sessions:
        del active_sessions[session_id]
        ACTIVE_SESSIONS.dec()
        log_event("session_closed", {"session_id": session_id})
        return {"message": "Session closed", "session_id": session_id}
    
    ERROR_COUNTER.labels(error_type="session_not_found").inc()
    raise HTTPException(status_code=404, detail="Session not found")

@app.get("/api/agent/stats")
async def get_stats():
    """Get AI agent statistics"""
    total_sessions = len(active_sessions)
    avg_thinking_time = 0
    
    log_event("stats_requested", {"active_sessions": total_sessions})
    
    return {
        "active_sessions": total_sessions,
        "average_thinking_time": AGENT_THINKING_TIME._sum.get() / max(AGENT_THINKING_TIME._count.get(), 1),
        "total_requests": REQUEST_COUNT._value.get(),
        "total_errors": ERROR_COUNTER._value.get(),
        "total_tokens": TOKEN_USAGE._value.get(),
        "timestamp": datetime.utcnow().isoformat()
    }

# Simulate background activity
import asyncio

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(simulate_background_activity())

async def simulate_background_activity():
    """Simulate background agent activity"""
    while True:
        await asyncio.sleep(random.uniform(30, 60))
        # Simulate periodic health check
        log_event("background_health_check", {
            "sessions": len(active_sessions),
            "uptime_seconds": time.time()
        })

if __name__ == "__main__":
    import uvicorn
    import logging

    # Create a filter to ignore standard Uvicorn access logs for /metrics and /health
    class EndpointFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            return msg.find("GET /metrics") == -1 and msg.find("GET /health") == -1

    # Apply the filter to Uvicorn
    logging.getLogger("uvicorn.access").addFilter(EndpointFilter())

    uvicorn.run(app, host="0.0.0.0", port=8000)