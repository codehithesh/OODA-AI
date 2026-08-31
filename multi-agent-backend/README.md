# Multi-Agent Backend

Production-ready multi-agent LLM backend: **FastAPI + SQLAlchemy 2.0 (async) + Alembic + Pydantic v2 + LangGraph**, fronted by an **OpenAI-compatible API** so [Open WebUI](https://github.com/open-webui/open-webui) (self-hosted) can drive it. Runs locally in Docker Compose.

```
Open WebUI ──OpenAI API──▶ backend (FastAPI :8000)
                              │
   ┌──────────────────────────┼───────────────────────────────────┐
   │  LangGraph state machines│  4 agent modes = 4 "models"       │
   │  analytics  monitor      │  research  simulate                │
   ├──────────────────────────┼───────────────────────────────────┤
   │ PostgreSQL 16 (app state + checkpoints + DecisionLog)        │
   │ DuckDB (embedded, in-process — analytics SQL only)           │
   │ Redis (cache + pub/sub)          n8n (human approval)        │
   │ LiteLLM proxy :4000 (unified LLM access)                     │
   └──────────────────────────────────────────────────────────────┘
```

## The four agent modes

Each mode is a separately compiled LangGraph state machine, exposed as a "model" in `GET /v1/models`:

| Mode | Topology | Flow |
|---|---|---|
| `analytics` | linear | `load_context → generate_sql → validate_sql → log_decision` |
| `monitor` | event-driven | `detect_signal → classify_signal → decide_action → [approve_or_auto_act] → log_decision` |
| `research` | cyclic | `parallel_peers → evaluate_evidence → synthesize → [next_gen_or_stop]` |
| `simulate` | fan-out | `spawn_personas → run_draft → collect_reactions → score_variants → pick_winner` |

Architecture invariants (enforced everywhere):

- **Nodes are pure** — typed Pydantic input/output models, no DB writes; every node's docstring documents input keys, output keys, and side-effect guarantees.
- **All Postgres writes** happen through the LangGraph checkpointer or the route/runner layer — never inside nodes.
- **All prompts** are Jinja2 `.md` files in `backend/context/prompts/`, rendered by `PromptLoader`.
- **Agent context is git-versioned**: `backend/context/` is its own git repo; every `DecisionLog` row stores the commit SHA and every new SHA gets a `ContextSnapshot` manifest.
- **DuckDB** is imported as a library (`import duckdb`), connects to a local file, and raw SQL is allowed *only* there. All Postgres access goes through the ORM.
- **No Celery** — concurrency is `asyncio.gather` inside nodes and FastAPI `BackgroundTasks` for ingestion/evals.
- **No dynamic imports, metaclasses, or monkey-patching.**

## Quickstart

Prerequisites: Docker + a provider API key (or a local OpenAI-compatible endpoint) for LiteLLM.

```bash
cp .env.example .env          # already provided with local defaults
# put your key in .env: OPENAI_API_KEY=sk-...
make dev                      # builds and starts everything
```

| Service | URL | Notes |
|---|---|---|
| Open WebUI | http://localhost:3000 | pick a model: `analytics` / `monitor` / `research` / `simulate` |
| Backend API | http://localhost:8000/docs | OpenAI-compatible + management API |
| LiteLLM proxy | http://localhost:4000 | swap providers in `litellm_config.yaml` |
| n8n | http://localhost:5678 | approval workflows only |
| PostgreSQL | localhost:5432 | `agent/agent` |
| Redis | localhost:6379 | cache + pub/sub |

Migrations run automatically on backend boot (`alembic upgrade head`); `make migrate` re-applies them idempotently.

### Try it

```bash
# list agent modes as models
curl -s localhost:8000/v1/models | jq '.data[].id'

# analytics: generate + validate + execute DuckDB SQL
curl -s localhost:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "analytics",
  "messages": [{"role": "user", "content": "What was total revenue by region?"}]
}' | jq -r '.choices[0].message.content'

# monitor: a critical event pauses the graph and asks for human approval
curl -s localhost:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "monitor",
  "messages": [{"role": "user", "content": "{\"metric\": \"error_rate\", \"value\": 0.31, \"source\": \"payments-api\"}"}]
}' | jq -r '.choices[0].message.content'

# stream it (SSE, OpenAI chunk format)
curl -N localhost:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "research", "stream": true,
  "messages": [{"role": "user", "content": "Is the team\\'s error budget burn rate sustainable?"}]
}'
```

Auth: when `BACKEND_API_KEYS` is non-empty (default `sk-local-dev`), `/v1/*` requires `Authorization: Bearer <key>`. Open WebUI sends it automatically from its `OPENAI_API_KEY` env.

## Human approval loop (n8n)

`require_approval` monitor actions pause the graph via LangGraph `interrupt()` — state is checkpointed in Postgres, nothing is lost:

1. Backend POSTs the approval request to `N8N_WEBHOOK_URL` (set it in `.env` to enable):
   ```json
   {"event": "approval.required", "type": "signal_approval", "signal_id": "...",
    "thread_id": "monitor-...", "summary": "...", "action_plan": {...},
    "callback_url": "http://localhost:8000/v1/signals/<id>/approve"}
   ```
2. Build an n8n workflow: **Webhook trigger** (URL = your `N8N_WEBHOOK_URL`) → your human step (email/Slack/wait) → **HTTP Request** node POSTing back to `callback_url` with header `Authorization: Bearer sk-local-dev` and body `{"approved": true, "approver": "on-call"}`.
3. The backend resumes the paused graph with `Command(resume=...)`, the Signal flips to `executed`/`dismissed`, the `DecisionLog` row updates, and n8n receives the `action.execute` webhook.

Without `N8N_WEBHOOK_URL` set you can still approve manually:

```bash
curl -X POST localhost:8000/v1/signals/<signal_id>/approve \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer sk-local-dev' \
  -d '{"approved": true, "approver": "me"}'
```

## Decision logging & audit

Every agent run (chat, ingestion, evaluation) lands in the `decision_logs` table: `mode`, `context_commit_sha`, `input`, `output`, `evaluation_score`, `latency_ms`, `cost_usd`, token counts, `created_at`. The git SHA ties each decision to the exact prompts/rules/personas it ran with (`context_snapshots` stores the full manifest).

```bash
curl -s 'localhost:8000/v1/decisions?mode=analytics&limit=10' | jq
curl -s localhost:8000/v1/decisions/stats | jq
```

Signals (monitor events) have their own lifecycle API: `POST /v1/signals` (ingest → background monitor run), `GET /v1/signals`, `POST /v1/signals/{id}/approve`.

## Evaluation harness

Suites are YAML files in `backend/context/evaluations/`; scorers: `exact_sql`, `execution_match` (both queries run on an in-memory DuckDB fixture), `exact_match`, `llm_judge`.

```bash
make eval                                   # runs all suites (needs the stack up)
cd backend && uv run python eval_harness.py --suite context/evaluations/analytics_suite.yaml --no-persist
# or via the API (background run + Redis status):
curl -X POST localhost:8000/v1/eval/runs -H 'Content-Type: application/json' -d '{"suite": "analytics_suite"}'
curl -s localhost:8000/v1/eval/runs/<run_id> | jq
```

## Project layout

```
docker-compose.yml          # postgres, redis, litellm, backend, open-webui, n8n (all healthchecked)
litellm_config.yaml         # provider routing (swap freely)
Makefile                    # dev / migrate / test / eval / lint (+ logs, ps, shell, fmt, reset)
backend/
  main.py                   # app factory + lifespan (migrations, checkpointer, DuckDB seed, probes)
  config.py                 # Pydantic Settings — the single source of truth
  database.py               # async engine, sessionmaker, get_db, migrations
  models.py                 # DecisionLog, Signal, ContextSnapshot (SQLAlchemy 2.0)
  schemas.py                # OpenAI-compatible + domain Pydantic v2 schemas
  observability.py          # structlog JSON, Sentry, Langfuse (all optional, degrade gracefully)
  eval_harness.py           # evaluation runner + CLI
  alembic/                  # async env + initial migration
  graphs/                   # base.py (state/registry/runner) + 4 graph modules
  nodes/                    # 12 pure typed node functions, one per file
  routes/                   # chat (OpenAI+SSE), models, decisions, signals, eval
  clients/                  # litellm, prompt_loader, git_context, duckdb, redis
  context/                  # git-versioned: prompts/ rules/ schemas/ personas/ evaluations/
  tests/                    # test_nodes/ test_graphs/ test_routes/ (73 tests, no services needed)
  Dockerfile                # python:3.12-slim + pinned uv, non-root
  pyproject.toml            # exact pins; uv.lock committed
```

## Configuration

Everything is a `Settings` field in `backend/config.py` (env var or `.env`; no scattered `os.getenv`). The important ones:

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://agent:agent@localhost:5432/agent` | Postgres (state + checkpoints) |
| `REDIS_URL` | `redis://localhost:6379/0` | cache + pub/sub |
| `LITELLM_BASE_URL` / `LITELLM_API_KEY` | `http://localhost:4000` | unified LLM proxy |
| `DEFAULT_MODEL` | `agent-default` | model alias the nodes request |
| `BACKEND_API_KEYS` | `sk-local-dev` | comma-separated bearer tokens; empty disables auth |
| `EXECUTE_ANALYTICS_SQL` | `true` | run validated SQL on DuckDB in chat responses |
| `N8N_WEBHOOK_URL` | *(empty)* | enables the approval trigger |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | used for approval callback URLs |
| `SENTRY_DSN`, `LANGFUSE_*` | *(empty)* | observability integrations |
| `RUN_MIGRATIONS_ON_START` | `true` | idempotent `alembic upgrade head` on boot |

## Development

```bash
make test-local    # pytest — runs with fakes, no Docker/DB needed (73 tests)
make lint          # ruff check + format check
make fmt           # ruff autofix + format
make shell         # shell inside the running backend container
make logs          # tail backend logs
```

Tests inject a scripted `FakeLLM` and a fake session through the same LangGraph config seam the app uses — including a full interrupt → resume approval test. Redis, Langfuse, and Sentry degrade gracefully when unconfigured.

## Notes

- **Streaming**: nodes complete before the route responds, so SSE streams the final agent answer as OpenAI-style chunks (Open WebUI renders it normally). Token-level LLM streaming would require streaming inside nodes; the runner seam (`graphs/base.py`) is where to add it.
- **Cost tracking** uses LiteLLM's `x-litellm-response-cost` header (`return_response_headers: true` is set in the proxy config); token counts always come from the proxy response.
- **Context versioning**: `backend/context/` is its own git repo. Change a prompt/rule → commit → every new decision log records the new SHA. Mount a different commit to compare behaviour (`make eval` before/after).
- **Production mode** (`ENVIRONMENT=production`) fails fast when Postgres or the checkpointer is unavailable; development degrades to MemorySaver with warnings.
