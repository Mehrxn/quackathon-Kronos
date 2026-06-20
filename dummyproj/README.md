# AI Agent Monitoring Stack

Complete monitoring solution with Python backend, frontend, Prometheus, Loki, and Grafana.

## Quick Start

1. Start all services:
```bash
docker-compose up -d
```

2. Access services:
- Frontend: http://localhost:8080
- Grafana: http://localhost:3001 (admin/admin)
- Prometheus: http://localhost:9090
- Backend API: http://localhost:8000/docs

3. Test the AI agent:
- Open the frontend at http://localhost:8080
- Type queries like "Analyze this data" or "Give me insights"
- Watch metrics appear in Grafana

## Monitoring Dashboard

The Grafana dashboard includes:
- Request rates and response times
- AI agent thinking time
- Token usage tracking
- Active sessions
- Error rates
- Application logs from Loki

## API Endpoints

- GET /health - Health check
- GET /metrics - Prometheus metrics
- POST /api/agent/query - Main AI query endpoint
- GET /api/agent/sessions - List active sessions
- DELETE /api/agent/sessions/{id} - Close session
- GET /api/agent/stats - Agent statistics

## Customization

- Modify MOCK_RESPONSES in backend/app.py to change AI responses
- Adjust thinking time simulation
- Add your real AI model integration
- Customize Grafana dashboard panels
```

## How to Run

1. **Save all files** in the structure shown above

2. **Start the stack**:
```bash
docker-compose up -d
```

3. **Access everything**:
- Frontend: `http://localhost:8080`
- Grafana: `http://localhost:3001` (login: admin/admin)
- Backend API docs: `http://localhost:8000/docs`

4. **Test the monitoring**:
- Open the frontend and send several queries
- Watch the Grafana dashboard populate with metrics
- Check Loki logs for structured application logging

The dashboard will show real-time metrics, request rates, thinking times, token usage, and logs. Everything is pre-configured and ready to use!