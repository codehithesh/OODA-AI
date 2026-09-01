<p align="center">
    <picture>
      <img src="https://github.com/codehithesh/OODA-Agent/blob/main/assets/ooda_ai_logo.jpeg" alt="OODA AI logo" />
    </picture>
</p>

<h1 align="center">OODA AI</h1>

<h3 align="center">
  Multi-agent backend for decision intelligence
</h3>

<br/>

## What is OODA AI?

OODA AI is a self-hosted multi-agent backend for analytics and decision intelligence. It exposes an **OpenAI-compatible API** so any chat UI (Open WebUI, etc.) can drive it. Agents are LangGraph state machines — each one a distinct analytical pipeline with full audit trails, checkpointed state, and an evaluation harness.

The system currently ships five agent modes, each accessible as a "model" from the chat UI:

| Mode | What it does |
|---|---|
| `eda` | Iterative exploratory data analysis — breaks a broad business question into hypotheses, runs multiple SQL queries, fuses external web context, generates Plotly charts, and produces evidence-backed recommendations |
| `analytics` | Single-query SQL generation and execution against DuckDB |
| `monitor` | Event-driven signal detection with a human approval gate via n8n |
| `research` | Cyclic peer-review loop — multiple LLM personas debate and synthesise a research brief |
| `simulate` | Persona fan-out — generates draft variants and scores them across simulated user reactions |

---

## Key Features

- 🔁 **Iterative EDA loop** — the `eda` agent forms hypotheses, queries the warehouse, evaluates evidence, loops until resolved, then produces findings and recommendations
- 📊 **Inline Plotly charts** — visualisations render directly in the chat interface (no separate dashboard required)
- 🧪 **Built-in evaluation harness** — YAML test suites with `exact_sql`, `execution_match`, `exact_match`, and `llm_judge` scorers; every run is scored and stored
- 🔍 **Failure taxonomy** — every failed benchmark run is classified into one of five failure modes (planning failure, plan error, data selection error, implementation error, runtime error)
- 📋 **Full audit trail** — every agent run writes a `DecisionLog` row with context commit SHA, latency, token counts, and cost
- 🔀 **n8n integration** — monitor approval requests pause the graph and notify n8n; any downstream workflow (email, Slack, Jira) can be triggered from the agent
- 🔒 **Self-hosted** — runs entirely in Docker Compose; bring your own LLM keys via LiteLLM

---

## Stack

```
multi-agent-backend/
├── docker-compose.yml          # postgres, redis, litellm, backend, open-webui, n8n
├── litellm_config.yaml         # LLM provider routing
├── Makefile                    # dev / test / eval / lint targets
└── backend/
    ├── main.py                 # FastAPI app factory + lifespan
    ├── config.py               # Pydantic Settings (single source of truth)
    ├── models.py               # DecisionLog, Signal, ContextSnapshot (SQLAlchemy 2.0)
    ├── schemas.py              # OpenAI-compatible + domain Pydantic v2 schemas
    ├── eval_harness.py         # Evaluation runner + CLI
    ├── benchmark.py            # Extended benchmark with failure taxonomy + dashboard
    ├── analysis_state.py       # Structured EDA state (hypotheses, queries, findings, metrics)
    ├── graphs/                 # LangGraph state machines (analytics, eda, monitor, research, simulate)
    ├── nodes/                  # Pure async node functions (one per file)
    ├── tools/                  # Tool registry (warehouse, web search, visualization, n8n)
    ├── routes/                 # chat, models, decisions, signals, eval, tools, benchmark
    ├── clients/                # litellm, duckdb, redis, prompt_loader, git_context
    └── context/                # Git-versioned: prompts/ rules/ schemas/ personas/ evaluations/
```

**Backend:** FastAPI · LangGraph · SQLAlchemy 2.0 (async) · Alembic · Pydantic v2 · DuckDB · Redis · LiteLLM

**Infrastructure:** PostgreSQL 16 · Redis 7 · n8n · Open WebUI · Docker Compose

---

## Quickstart

Prerequisites: Docker Desktop + at least one LLM provider API key.

```bash
# 1. Clone and enter the repo
git clone <repo>
cd OODA-Agent

# 2. Run the setup script (installs dependencies, starts the stack)
chmod +x start.sh && ./start.sh
```

The script handles Homebrew, uv, Docker Desktop, `.env` creation, and launches the full stack. On completion it opens the services in your browser.

Or start manually:

```bash
cd multi-agent-backend
cp .env.example .env          # add your LLM key
make dev                      # docker compose up --build -d
```

### Services

| Service | Address | Notes |
|---|---|---|
| Open WebUI | `http://localhost:3000` | Select a model: `analytics` / `eda` / `monitor` / `research` / `simulate` |
| Backend API | `http://localhost:8000/docs` | OpenAI-compatible + management endpoints |
| LiteLLM proxy | `http://localhost:4000` | Swap providers in `litellm_config.yaml` |
| n8n | `http://localhost:5678` | Approval workflows + downstream integrations |

---

## Agent modes

### `eda` — Iterative exploratory analysis

Use this for broad business questions. The agent:
1. Breaks the question into sub-questions and initial hypotheses
2. Generates and executes SQL queries iteratively
3. Evaluates evidence for each hypothesis (supported / rejected / refined)
4. Optionally searches the web for external context
5. Fuses internal and external evidence (flagging comparability issues)
6. Generates Plotly charts for the most informative results
7. Produces findings (distinguishing facts from inferences) and prioritised recommendations

```
POST /v1/chat/completions
{ "model": "eda", "messages": [{"role": "user", "content": "How can we increase revenue?"}] }
```

### `analytics` — Single-query SQL

For direct data lookups. Generates a DuckDB SQL query, validates it, executes it, and returns the result as a markdown table.

```
POST /v1/chat/completions
{ "model": "analytics", "messages": [{"role": "user", "content": "What is total revenue by region this month?"}] }
```

### `monitor` — Signal detection with approval gate

For operational events. Detects signals against a rule matrix, classifies them with an LLM, decides an action, and pauses for human approval on critical events via n8n.

```
POST /v1/signals
{ "source": "payments-api", "payload": {"metric": "error_rate", "value": 0.31} }
```

### `research` — Cyclic peer review

Multiple LLM personas (data analyst, domain expert, sceptical peer) respond to a brief across up to N generations until consensus is reached.

### `simulate` — Persona fan-out

Generates K draft variants and scores them across simulated persona reactions to find the best-performing response.

---

## Context — the git-versioned agent brain

Everything the agents know lives in `multi-agent-backend/backend/context/` — a separate git repo:

```
context/
├── prompts/         # Jinja2 .md prompt templates (one per node)
│   ├── analytics/
│   ├── eda/         # plan_analysis, generate_eda_sql, evaluate_evidence, fuse_context, generate_findings, select_visualizations
│   ├── monitor/
│   ├── research/
│   ├── simulate/
│   └── eval/
├── rules/           # YAML rule files (SQL guardrails, action matrices, research budgets)
├── schemas/         # Warehouse DDL injected into SQL generation prompts
├── personas/        # YAML persona definitions for research and simulate modes
└── evaluations/     # YAML benchmark suites (analytics, eda, monitor, research, simulate)
```

Every `DecisionLog` row records the context commit SHA. Every new SHA gets a `ContextSnapshot` manifest — so any decision is reproducible from the exact prompts and rules that produced it.

---

## Evaluation

### Running evaluations

```bash
# All suites via the CLI (inside the backend container or venv)
uv run python eval_harness.py --all

# Single suite
uv run python eval_harness.py --suite context/evaluations/analytics_suite.yaml

# Via the API (background run, poll for results)
curl -X POST localhost:8000/v1/eval/runs \
  -H 'Content-Type: application/json' \
  -d '{"suite": "eda_suite"}'

curl localhost:8000/v1/eval/runs/<run_id>
```

### Extended benchmark with failure taxonomy

```bash
# Run all suites with failure classification + dashboard
uv run python benchmark.py --all --report benchmark_report.json

# Compare two runs for regressions
uv run python benchmark.py --compare baseline.json current.json
```

Failure modes tracked per run:

| Failure mode | What it means |
|---|---|
| `planning_failure` | Agent did not produce a valid plan or tool call |
| `plan_error` | Plan is syntactically valid but analytically wrong |
| `data_selection_error` | Wrong table, column, or join key selected |
| `implementation_error` | Correct plan, incorrect SQL / regex / transformation |
| `runtime_error` | Execution failed (DB error, timeout, API unavailable) |

### Writing test cases

Add YAML files to `multi-agent-backend/backend/context/evaluations/`:

```yaml
suite: my_suite
mode: analytics          # or eda, monitor, research, simulate
scorer: execution_match  # exact_sql | execution_match | exact_match | llm_judge
threshold: 0.75
cases:
  - id: total_revenue
    input:
      query: "What is total revenue?"
    expected:
      sql: "SELECT SUM(amount) AS total_revenue FROM orders"
```

---

## Tool registry

The EDA agent uses a typed tool registry. Tools available out of the box:

| Tool | Category | What it does |
|---|---|---|
| `inspect_schema` | warehouse | Returns tables and column definitions from the warehouse |
| `execute_sql` | warehouse | Executes a read-only SQL query and returns rows |
| `profile_data` | profiling | Null rates, distinct counts, min/max per column |
| `web_search` | web | External search via DuckDuckGo (or a custom backend) |
| `generate_visualization` | visualization | Produces a Plotly JSON spec from query result rows |
| `invoke_n8n` | n8n | Fires a named n8n workflow with a structured payload |

```bash
# List all registered tools
curl localhost:8000/v1/tools

# Invoke a tool directly
curl -X POST localhost:8000/v1/tools/inspect_schema \
  -H 'Content-Type: application/json' -d '{}'

# Trigger an n8n workflow directly
curl -X POST localhost:8000/v1/n8n/invoke \
  -H 'Content-Type: application/json' \
  -d '{"workflow_name": "send_email", "payload": {"to": "team@example.com"}}'
```

---

## Configuration

All settings live in `multi-agent-backend/backend/config.py` (env var or `.env`):

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://agent:agent@localhost:5432/agent` | PostgreSQL |
| `REDIS_URL` | `redis://localhost:6379/0` | Cache + pub/sub |
| `LITELLM_BASE_URL` | `http://localhost:4000` | LLM proxy |
| `DEFAULT_MODEL` | `agent-default` | Model alias used by all nodes |
| `BACKEND_API_KEYS` | `sk-local-dev` | Comma-separated bearer tokens; empty disables auth |
| `EXECUTE_ANALYTICS_SQL` | `true` | Run validated SQL in chat responses |
| `N8N_WEBHOOK_URL` | *(empty)* | Enables monitor approval notifications |
| `N8N_WORKFLOWS` | *(empty)* | JSON map of workflow name → webhook URL |
| `SEARCH_BACKEND_URL` | *(empty)* | Custom search backend; defaults to DuckDuckGo |
| `SENTRY_DSN` | *(empty)* | Error tracking |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | *(empty)* | LLM call tracing |

---

## Development

```bash
# Run tests (no Docker or external services required)
make test-local        # or: cd backend && uv run pytest

# Lint + format
make lint
make fmt

# Tail backend logs
make logs

# Shell inside the running backend container
make shell

# Stop and wipe all volumes
make reset
```

Tests use a scripted `FakeLLM` and in-memory `MemorySaver` — no real LLM calls or database connections needed.

---

## n8n integration

The monitor agent can pause on `require_approval` events and notify n8n:

1. Backend POSTs to `N8N_WEBHOOK_URL` with the signal details and a `callback_url`
2. Your n8n workflow notifies a human (email, Slack, etc.) and waits
3. On approval, n8n POSTs back to `callback_url` — the graph resumes

For general actions (send reports, create tickets, etc.) the `eda` agent can call named workflows via the `invoke_n8n` tool. Map workflow names to webhook URLs in `N8N_WORKFLOWS`.

Manual approval without n8n:

```bash
curl -X POST localhost:8000/v1/signals/<signal_id>/approve \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer sk-local-dev' \
  -d '{"approved": true, "approver": "me"}'
```

---

## Project layout

```
OODA-Agent/
├── start.sh                          # One-shot macOS setup + launcher
└── multi-agent-backend/
    ├── docker-compose.yml
    ├── litellm_config.yaml
    ├── Makefile
    ├── .env.example
    ├── ARCHITECTURE.md
    ├── DEPLOYMENT.md
    └── backend/
        ├── main.py
        ├── config.py
        ├── database.py
        ├── models.py
        ├── schemas.py
        ├── observability.py
        ├── eval_harness.py
        ├── benchmark.py
        ├── analysis_state.py
        ├── alembic/
        ├── graphs/
        │   ├── base.py               # State schema, registry, runner
        │   ├── analytics_graph.py
        │   ├── eda_graph.py          # Iterative EDA pipeline
        │   ├── monitor_graph.py
        │   ├── research_graph.py
        │   └── simulate_graph.py
        ├── nodes/                    # Pure async node functions
        ├── tools/                    # Tool registry (warehouse, web, viz, n8n)
        ├── routes/                   # API routes
        ├── clients/                  # Service clients
        ├── context/                  # Git-versioned agent context
        └── tests/
```

---

## License

This project is licensed under the Apache 2.0 License — see the [LICENSE](/LICENSE.txt) file for details.

### Third-party notices

**Open WebUI** is used as the chat interface. It is licensed under the [BSD 3-Clause License](https://github.com/open-webui/open-webui/blob/main/LICENSE) with an additional branding protection clause (introduced in v0.6.6). The "Open WebUI" name and branding may not be removed or altered — see the upstream license for the full terms.
