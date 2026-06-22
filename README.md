# Kronos — Autonomous Incident Response Agent

> **When production breaks at 3 AM, Kronos reads the error, finds the exact code, diagnoses the root cause, and either opens a PR or escalates to a GitHub issue — all before your pager stops buzzing.**

Kronos is an autonomous SRE agent built for the hackathon. It plugs into your existing observability stack (Grafana, Prometheus, Loki), retrieves the right code chunks from error messages alone, runs a single chain-of-thought diagnosis, validates with a generated reproduction test, and then **auto-fixes and pushes a branch/PR** or **opens a GitHub issue** — depending on severity and your autonomy config.

**Core insight:** error messages are ground truth. Extract the function names, grep the repo, assemble minimal context, hand it to the LLM in one shot. No AST. No call graphs. No embedding search at retrieval time.

---

## Why Kronos wins

| Problem                                                   | Kronos answer                                                                          |
| --------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| On-call gets paged, spends 45 min grepping logs and Slack | Parses errors in milliseconds, retrieves relevant code in parallel                     |
| Generic AI agents hallucinate fixes on whole repos        | Budgeted chunk assembly — only definitions, callers, and keywords that match the error |
| Auto-fix bots merge broken code                           | Confirmation test must reproduce the bug; full test + build gate before any PR         |
| Same incident fires every deploy                          | Parcle memory layer — second occurrence skips diagnosis via pattern cache              |
| Teams don't trust full autonomy                           | Configurable decision matrix: high → PR, medium → PR or issue, low → issue only        |

---

## How it works

```
Grafana alert / webhook
        ↓
  Parse error logs → extract function names + error types
        ↓
  Pattern cache lookup (Parcle) — exact match? fuzzy hint? miss?
        ↓
  Parallel grep retrieval → rank chunks → assemble token-budgeted context
        ↓
  Single CoT diagnosis (Claude / Gemini / DeepSeek / local)
        ↓
  Confirmation test — does the bug reproduce?
        ↓
  Decision matrix (priority × confidence × autonomy)
        ↓
  ┌─────────────────────────┬──────────────────────────┐
  │  AUTO-FIX path          │  ISSUE path              │
  │  branch → patch → test  │  GitHub issue + tags     │
  │  → build → commit       │  poll for @agent: fix    │
  │  → push → open PR       │  or @agent: ignore       │
  └─────────────────────────┴──────────────────────────┘
        ↓
  Learn: write rule back to Parcle for next time
```

### Retrieval pipeline (accurate chunks, minimal noise)

1. **Error parsing** — regex library maps known phrasings to `error_type` + keywords; extracts the function before the colon; dedups on `(function, error_type)`; tags priority hints. (`kronos/retrieval/parser.py`)
2. **Parallel grep** — per pattern, concurrently grep definitions, call sites, and keyword fallbacks across a worker pool; call-depth expansion and import surfacing. (`kronos/retrieval/retriever.py`)
3. **Streaming extraction** — read definitions forward by brace depth (capped); callers and keywords by fixed windows; strip comments, collapse blanks.
4. **Dedup & ranking** — merge overlapping ranges; score def 1.0 / caller 0.8 / keyword 0.6; boost error-type terms and git recency. (`kronos/retrieval/assembler.py`)
5. **Budgeted assembly** — split token budget across error logs, definitions, callers, keywords, and recent git changes; soft-truncate oversized chunks instead of dropping them silently.

### Fix vs. issue — you control the blast radius

The **decision matrix** (`kronos/agent/decision.py`) reconciles log-derived priority hints with the LLM's independent classification. A failed confirmation test or low confidence always routes to an issue.

| Priority   | `full_autonomous=true`      | `full_autonomous=false`                     |
| ---------- | --------------------------- | ------------------------------------------- |
| **high**   | Fix → test → build → **PR** | Fix → test → build → **PR** (bypasses flag) |
| **medium** | Fix → test → build → **PR** | Confirm → **issue** → tag maintainers       |
| **low**    | Confirm → **issue** → tag   | Confirm → **issue** → tag                   |

When routed to an issue, maintainers can reply `@agent: fix` or `@agent: ignore` — Kronos polls and acts accordingly.

### Pattern cache — incidents get cheaper over time

Fingerprint = `alert_type + sorted functions + error types`. Exact match → use cached fix. Jaccard similarity above threshold → pass as hint to LLM. Every resolved incident becomes a reusable rule in Parcle.

---

## Quick start

### 1. Install

```bash
git clone https://github.com/Nurysso/quackathon-Kronos.git
cd quackathon-Kronos
uv pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env          # GITHUB_TOKEN, GEMINI_API_KEY (or CLAUDE), PARCLE_API_KEY
cp config.yaml.example config.yaml
# Edit config.yaml: set repository.local_path, github_url, test/build commands
set -a && source .env && set +a
```

### 3. Run

```bash
uv run main.py                # API + dashboard on :8000 or whatever you kept in .env
```

Open **http://localhost:8000/dashboard** for the live incident board.
Open **http://localhost:8000/api/docs** for api documentaion [Swagger UI]
Open **http://localhost:8000/api/redocs** for redoc styled documentaion.

### 4. Demo (two terminals)

```bash
# Terminal 1
uv run main.py

# Terminal 2 — fires the same incident twice
uv run demo.py
```

**First run:** full retrieval → diagnosis → fix/issue path (watch chunks, confidence, and routing in the dashboard).

**Second run:** same fingerprint hits Parcle cache — resolves in a fraction of the time.

---

## Observability stack (optional full demo)

The `dummyproj/` folder includes a complete Grafana + Prometheus + Loki + dummy backend for end-to-end alerting:

```bash
cd dummyproj && docker-compose up -d
```

- Grafana: http://localhost:3001 (admin/admin)
- Wire Grafana alerting webhook → `POST http://localhost:8000/api/v1/init/`

---

## API reference

| Method | Path                                         | Purpose                                                              |
| ------ | -------------------------------------------- | -------------------------------------------------------------------- |
| `POST` | `/api/v1/init/`                              | Primary Grafana webhook; returns `{incident_id, status}` immediately |
| `POST` | `/api/v1/quick-incident/`                    | Manual trigger `{error_logs, priority}` (skips Loki)                 |
| `GET`  | `/api/v1/incidents/`                         | Paginated list, filter by `status` / `priority`                      |
| `GET`  | `/api/v1/incidents/{id}/`                    | Single incident status + PR/issue URL                                |
| `GET`  | `/api/v1/incidents/{id}/diagnosis`           | Root cause, confidence, retrieved chunks, trace                      |
| `POST` | `/api/v1/incidents/{id}/approve` · `/ignore` | Maintainer control                                                   |
| `GET`  | `/api/v1/health`                             | GitHub / Loki / LLM / Parcle reachability                            |
| `GET`  | `/api/v1/rules`                              | Current priority rule patterns                                       |
| `GET`  | `/dashboard`                                 | Live incident dashboard                                              |

---

## Configuration highlights

Everything is driven by `config.yaml` (env vars via `${VAR}`):

```yaml
autonomy:
  full_autonomous: true # medium incidents → PR instead of issue

rules:
  priority:
    high:
      required_confidence: 0.75
    medium:
      required_confidence: 0.70
      auto_fix: 'follow_autonomy' # respects full_autonomous flag

context_retrieval:
  max_context_tokens: 4000
  max_workers: 4

code_style:
  language: 'python' # also: go, javascript, typescript, java, rust
```

### Dev notifications (Slack / email)

Keep the team in the loop without watching the dashboard:

```yaml
notifications:
  enabled: true
  min_priority: 'medium' # only notify for medium+ incidents
  events:
    - diagnosis_complete
    - pr_opened
    - issue_opened
    - failed
  slack:
    enabled: true
    webhook_url: '${SLACK_WEBHOOK_URL}'
    mention: '<!channel>'
  email:
    enabled: true
    smtp_host: 'smtp.gmail.com'
    smtp_port: 587
    smtp_user: '${SMTP_USER}'
    smtp_password: '${SMTP_PASSWORD}'
    from_address: 'kronos@yourcompany.com'
    to_addresses:
      - 'oncall@yourcompany.com'
```

Notifications fire at each lifecycle stage: incident started, diagnosis complete, fix in progress, PR/issue opened, resolved, failed, or ignored.

---

## Project structure

```
kronos/
├── agent/          orchestrator, decision matrix, retry loop, incident store
├── retrieval/      parser → retriever → assembler (the chunk pipeline)
├── integrations/   GitHub, Loki, LLM providers, Parcle cache
├── api/            FastAPI endpoints + dashboard
└── models/         Incident, Diagnosis, CodeChunk, Priority
dummyproj/          Full observability demo stack
demo.py             Two-incident demo driver
config.yaml.example All tunables
```

---

## Tech stack

- **Python 3.11+** · FastAPI · uvicorn
- **LLM providers:** Claude, Gemini, DeepSeek, local models
- **GitHub:** PyGithub + local git (branch, commit, push, PR, issue)
- **Memory:** Parcle (remote API + SQLite fallback)
- **Observability:** Loki log pull, Grafana webhook ingestion, or direct interaction via

---

## License

See [LICENSE](LICENSE).
