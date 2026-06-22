# Kronos — Autonomous Incident Response Agent

> **When production breaks at 3 AM, Kronos reads the error, finds the exact code, diagnoses the root cause, and either opens a PR or escalates to a GitHub issue — all before your pager stops buzzing.**

Kronos is an autonomous SRE agent built for **Quackathon 2026** (Track 01: Software — The Sentient Workspace). It plugs into your existing observability stack (Grafana, Prometheus, Loki), retrieves the right code chunks from error messages alone, runs a single chain-of-thought diagnosis, validates with a generated reproduction test, and then **auto-fixes and pushes a branch/PR** or **opens a GitHub issue** — depending on severity and your autonomy config.

**Core insight:** error messages are ground truth. Extract the function names, grep the repo, assemble minimal context, hand it to the LLM in one shot. No AST. No call graphs. No embedding search at retrieval time.

**Team:** WhyVenv — Nurysso · Mehran · Ibrahim · Emaad · omair · zain

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

## Tool integration

### Parcle — persistent memory layer

Parcle is the memory backbone of Kronos. Every resolved incident is fingerprinted and written back to Parcle as a reusable rule.

- **Fingerprint:** `alert_type + sorted functions + error types`
- **Exact match** → serve cached fix directly, skip diagnosis entirely
- **Fuzzy match** (Jaccard similarity above threshold) → pass cached context as a hint to the LLM, accelerating diagnosis
- **Miss** → full retrieval + diagnosis, then write the new rule back to Parcle

This means Kronos gets faster and more accurate with every incident. The two-run demo shows this clearly: the first run does full retrieval and diagnosis; the second run on the same fingerprint resolves in a fraction of the time.

Parcle is configured via `PARCLE_API_KEY` in `.env`. SQLite is used as a local fallback if Parcle is unreachable.

### Enter Pro — development environment

Enter Pro was used during development for building, iterating, and debugging the Kronos pipeline.

---

## How it works

> Works with any dashboard as long as Kronos receives the logs via `GET`

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

### traceGrep — retrieval pipeline

No AST parsing. No embeddings. Pure regex grep over source text.

1. **Error parsing** — regex library maps known phrasings to `error_type` + seed keywords; Go stack-frame and `identifier:` heuristics extract the erroring function; deduplicates on `(function, error_type)`; tags priority hints from config rules. (`kronos/retrieval/parser.py`)

2. **Parallel grep** — per `ErrorPattern`, concurrently greps definitions (regex match on function declaration), call sites (depth-2 caller expansion), and keyword fallbacks across a worker pool; surfaces the import block of every file that yielded a definition hit. (`kronos/retrieval/retriever.py`)

3. **Context extraction** — definitions read forward by brace depth (capped at `max_function_lines`); callers and keywords by fixed before/after windows; comments stripped, blank runs collapsed; per-file chunk cap enforced; content-hash dedup applied before returning.

4. **Ranking** — overlapping ranges merged (higher score wins, range widened); cross-pattern keyword aggregation (+0.2 cap); error-function boost (+0.15) and high-priority boost (+0.10); git recency normalised into score (+0.05 max). (`kronos/retrieval/assembler.py`)

5. **Budgeted assembly** — token budget split across error logs, definitions, callers, keywords, and recent git changes; erroring-function definitions pinned first; oversized chunks soft-truncated instead of silently dropped; section headers include file paths for LLM orientation.

### Fix vs. issue — you control the blast radius

The **decision matrix** (`kronos/agent/decision.py`) reconciles log-derived priority hints with the LLM's independent classification. A failed confirmation test or low confidence always routes to an issue.

| Priority   | `full_autonomous=true`      | `full_autonomous=false`                     |
| ---------- | --------------------------- | ------------------------------------------- |
| **high**   | Fix → test → build → **PR** | Fix → test → build → **PR** (bypasses flag) |
| **medium** | Fix → test → build → **PR** | Confirm → **issue** → tag maintainers       |
| **low**    | Confirm → **issue** → tag   | Confirm → **issue** → tag                   |

When routed to an issue, maintainers can reply `@agent: fix` or `@agent: ignore` — Kronos polls and acts accordingly.

---

## Quick start

### Docker (recommended)

```bash
git clone https://github.com/Nurysso/quackathon-Kronos.git
cd quackathon-Kronos
docker-compose up -d
```

### Manual setup

```bash
# 1. Clone
git clone https://github.com/Nurysso/quackathon-Kronos.git
cd quackathon-Kronos

# 2. Configure
cp .env.example .env          # set GITHUB_TOKEN, GEMINI_API_KEY (or CLAUDE_API_KEY), PARCLE_API_KEY
cp config.yaml.example config.yaml
# Edit config.yaml: set repository.local_path, github_url, test/build commands
set -a && source .env && set +a

# 3. Install uv and run
uv venv .venv --python 3.11
uv run main.py                # API + dashboard on :8000
```

> **Windows note:** May face config error run the error on enterPro or llm of choice to figure out error.

**Dashboards and docs:**

- `http://localhost:8000/dashboard` — live incident board
- `http://localhost:8000/api/docs` — Swagger UI
- `http://localhost:8000/api/redoc` — ReDoc

> If running via Docker, the port may differ — run `docker ps` to confirm and check `.env`.

### Running the demo

```bash
# Terminal 1
uv run main.py

# Terminal 2 — fires the same incident twice
uv run demo.py
```

**First run:** full retrieval → diagnosis → fix/issue routing (watch chunks, confidence, and routing in the dashboard).

**Second run:** same fingerprint hits Parcle cache — resolves in a fraction of the time.

### Docker + Grafana demo

Edit `config.yaml` and set your repo URL and local path, then:

- Open Grafana at `http://localhost:3001` (default credentials: `admin` / `admin`)
- Wire a new Grafana alerting contact point: **Webhook → `POST http://localhost:8000/api/v1/init/`**
- Set the notification policy to use the webhook instead of the default

The `dummyproj/` folder includes a complete Grafana + Prometheus + Loki + dummy backend for end-to-end alerting.

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
      auto_fix: 'follow_autonomy'

context_retrieval:
  max_context_tokens: 4000
  max_workers: 4

code_style:
  language: 'python' # also: go, javascript, typescript, java, rust
```

### Notifications (Slack / email)

```yaml
notifications:
  enabled: true
  min_priority: 'medium'
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
- **Observability:** Loki log pull, Grafana webhook ingestion, Prometheus

---

## Acknowledgements

Built at **Quackathon 2026** — thanks to **Produck** for organizing, and to **EnterPro** and **Parcle** for the free API access that made this possible.

---

## License

See [LICENSE](LICENSE).
