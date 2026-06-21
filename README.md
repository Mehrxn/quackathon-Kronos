# Kronos — Autonomous Incident Response Agent

Kronos receives alerts (Prometheus metrics + Loki logs) via webhook, retrieves the
relevant code with a fast grep-based error-retrieval algorithm (no AST, no call
graphs), diagnoses the root cause in a single chain-of-thought Claude call, validates
with a generated reproduction test, and either auto-fixes (branch → PR) or escalates
(GitHub issue) based on a priority + autonomy decision matrix. Parcle is the long-term
memory layer: every resolved incident becomes a reusable rule that short-circuits
diagnosis on repeat occurrences.

**Core principle:** error messages are ground truth. Find the function names mentioned
in the error, grep for them, extract minimal context, hand to the LLM in one shot.

## Architecture

```
webhook → parse errors → pattern cache (Parcle) → grep retrieval → assemble context
        → single CoT diagnosis (Claude) → confirmation test → decision matrix
        → [auto-fix → test → build → PR]  OR  [issue → tag → poll]
        → learn (write rule back to Parcle)
```

### Retrieval pipeline (Phases 1–5)
1. **Error parsing** — regex library maps known phrasings to `error_type` + `keywords`,
   extracts the function before the colon, dedups on `(function, error_type)`, tags a
   priority hint. (`kronos/retrieval/parser.py`)
2. **Parallel grep** — per pattern, concurrently grep definition / call sites / keyword
   fallbacks across a worker pool. (`kronos/retrieval/retriever.py`)
3. **Streaming extraction** — read definitions forward by brace depth (capped), callers
   and keywords by fixed windows; strip comments / collapse blanks.
4. **Dedup & ranking** — merge overlapping ranges; score def 1.0 / caller 0.8 / keyword
   0.6, +0.1 high-priority; sort, tiebreak by git recency. (`kronos/retrieval/assembler.py`)
5. **Budgeted assembly** — split the token budget across categories, drop whole chunks
   that don't fit rather than truncating.

### Pattern cache (`kronos/integrations/cache.py`)
Single tier, no embeddings. Fingerprint = `alert_type + sorted functions + error types`.
Exact match → use cached fix, skip the LLM. Otherwise Jaccard over keyword sets; above
`similarity_threshold` it's passed to the LLM as a prior (cheap insurance), not a skip.

### Decision matrix (`kronos/agent/decision.py`)
Resolved priority reconciles the Phase-1 hint with the LLM's independent classification;
on disagreement the LLM wins if it clears that bucket's `required_confidence`. A failed
confirmation test always routes to an issue.

| Priority | `full_autonomous=true` | `full_autonomous=false` |
|---|---|---|
| high | Fix → test → build → PR | Fix → test → build → PR (bypasses flag) |
| medium | Fix → test → build → PR | Confirm → issue → tag |
| low | Confirm → issue → tag | Confirm → issue → tag |

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in GITHUB_TOKEN, CLAUDE_API_KEY, PARCLE_API_KEY
set -a && source .env && set +a
python main.py                # serves on :8000
```

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/init/` | Primary Grafana webhook; returns `{incident_id, status}` immediately |
| POST | `/api/v1/quick-incident/` | Manual trigger `{error_logs, priority}` (skips Loki) |
| GET | `/api/v1/incidents/` | Paginated list, filter by `status`/`priority` |
| GET | `/api/v1/incidents/{id}/` | Single incident status + PR/issue URL |
| GET | `/api/v1/incidents/{id}/diagnosis` | Root cause, confidence, context, trace |
| POST | `/api/v1/incidents/{id}/approve` · `/ignore` | Maintainer control |
| GET | `/api/v1/health` | GitHub / Loki / Claude / Parcle reachability |
| GET | `/api/v1/rules` | Current priority rule patterns |

## Demo

```bash
python main.py        # terminal 1
python demo.py        # terminal 2 — fires the same incident twice
```

The first occurrence runs the full retrieval + diagnosis path; the second shares a
fingerprint and resolves via the Parcle cache fast path.

## Tests

```bash
pytest tests/ -v      # 19 tests, fully offline (network stubbed)
```

## Configuration

Everything is driven by `config.yaml` (env vars via `${VAR}`). Key knobs:
`autonomy.full_autonomous`, `rules.priority.*.required_confidence`,
`pattern_cache.similarity_threshold`, `context_retrieval.*` (workers, budgets,
caps), `code_style.language` (retrieval is language-aware: go/python/js/ts/java/rust).

## Roadmap (pitch only — scoped out)

Multi-model reasoning pipeline; embedding-based similarity cache beyond Jaccard;
mutation testing for generated-test quality; git-blame hotspot expansion;
deployment-window / data-loss gating; auto-tuned confidence thresholds.
