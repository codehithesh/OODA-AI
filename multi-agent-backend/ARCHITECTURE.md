# Architecture & Design

This document explains the OODA backend architecture: data flow, design decisions, and key patterns.

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Open WebUI (Frontend)                     │
│                     OpenAI-compatible chat UI                    │
└────────────────────────────┬────────────────────────────────────┘
                             │ POST /v1/chat/completions
                             │ GET /v1/models
                             ↓
┌─────────────────────────────────────────────────────────────────┐
│                  FastAPI Backend (Port 8000)                     │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  OpenAI-compatible API Layer (routes/)                   │  │
│  │  - POST /v1/chat/completions (stream + JSON)             │  │
│  │  - GET /v1/models (list agent modes)                     │  │
│  │  - CRUD: /decisions, /signals, /eval                     │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  LangGraph State Machines (graphs/)                       │  │
│  │  - analytics (linear: SQL generation → validation)       │  │
│  │  - monitor (event-driven: detect → classify → decide)    │  │
│  │  - research (cyclic: peers → synthesize → converge)      │  │
│  │  - simulate (fan-out: personas × variants)               │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Pure Async Nodes (nodes/)                               │  │
│  │  13 typed, side-effect-free functions                   │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Clients (clients/)                                      │  │
│  │  - LiteLLM: unified LLM API                              │  │
│  │  - DuckDB: embedded analytics                            │  │
│  │  - Redis: caching + pub/sub                              │  │
│  │  - PromptLoader: Jinja2 templates                        │  │
│  │  - GitContext: versioned agent context                   │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Database Layer (SQLAlchemy 2.0 async ORM)               │  │
│  │  - DecisionLog: audit trail (all agent runs)             │  │
│  │  - Signal: monitor events + lifecycle                    │  │
│  │  - ContextSnapshot: git manifests by SHA                 │  │
│  │  - checkpoint_* tables: LangGraph checkpoints            │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
        ↓                        ↓                    ↓
   PostgreSQL 16          Redis 7            LiteLLM proxy
  (app state +         (cache +               (port 4000)
   checkpoints)        pub/sub)          unified LLM routing
   persistent          ephemeral               external
```

## Data Flow: Analytics Mode (Simplest Example)

```
User Query
    ↓
POST /v1/chat/completions
  { "model": "analytics", "messages": [...], "stream": true|false }
    ↓
[FastAPI route handler (routes/chat.py)]
    ↓
1. Parse request → ChatCompletionRequest
2. Load agent context (ContextBundle)
3. Build initial state:
   {
     "mode": "analytics",
     "query": "...",
     "input": {...},
     "context": {...},
     "usage": [],
   }
    ↓
[Run LangGraph: get_graph("analytics", checkpointer).ainvoke(state, config)]
    ↓
4. START → load_context node
   - Reads backend/context/ files (git-versioned)
   - Returns: context={rules, prompts, schemas, personas}
   - State update: {"context": ContextBundle}
    ↓
5. load_context → generate_sql node
   - Renders analytics/generate_sql.md prompt with Jinja2
   - LiteLLM call: "Convert this question to DuckDB SQL"
   - Returns: {"generated_sql": "SELECT ...", "usage": [...]}
    ↓
6. generate_sql → validate_sql node
   - Checks: no forbidden keywords, max length, valid syntax
   - DuckDB parse: verifies query parses
   - Returns: {"sql_valid": true|false, "sql_validation_errors": [...]}
   - If valid: executes query, captures result
   - Returns: {"sql_result": [...], "result_count": N}
    ↓
7. validate_sql → log_decision node
   - Constructs DecisionLog row:
     {
       "mode": "analytics",
       "status": "succeeded",
       "context_commit_sha": "abc...",
       "input": user_query,
       "output": {"sql": "...", "result": {...}},
       "evaluation_score": 1.0 if valid else 0.0,
       "latency_ms": time_elapsed,
       "cost_usd": llm_cost,
       "prompt_tokens": X,
       "completion_tokens": Y,
     }
   - Writes DecisionLog row to PostgreSQL (via checkpointer)
   - Returns: {"decision_record": {...}}
    ↓
8. END
    ↓
[LangGraph checkpointer persists graph state to PostgreSQL]
    ↓
[Route handler returns response]
    ↓
If stream=false:
  {
    "id": "chatcmpl-...",
    "object": "chat.completion",
    "created": 1234567890,
    "model": "analytics",
    "choices": [
      {
        "index": 0,
        "message": {
          "role": "assistant",
          "content": "Query result: [...]"
        },
        "finish_reason": "stop"
      }
    ],
    "usage": {"prompt_tokens": X, "completion_tokens": Y, "total_tokens": Z}
  }

If stream=true (SSE chunks):
  data: {"object": "chat.completion.chunk", "choices": [{"delta": {"role": "assistant"}}]}
  data: {"object": "chat.completion.chunk", "choices": [{"delta": {"content": "Query"}}]}
  ...
  data: [DONE]
    ↓
Client renders response
```

## Data Flow: Monitor Mode (With Approval Gate)

```
POST /v1/signals  (signal ingestion)
  { "source": "payments-api", "payload": {"metric": "error_rate", "value": 0.31} }
    ↓
[Routes signal to background task via FastAPI BackgroundTasks]
    ↓
[Background: run_monitor_for_signal(...)]
    ↓
1. Create Signal row (status: "new")
2. Run monitor graph (detect_signal → classify → decide → approve_or_auto_act → log)
    ↓
3. detect_signal node
   - Checks rules["monitor"].detection matrix
   - error_rate > 0.05 → signal_detected=true, kind="error_burst", severity="critical"
   - Returns: {"signal_detected": true, ...}
    ↓
4. classify_signal node
   - Calls LLM: "Classify this signal" (monitor/classify_signal.md)
   - Returns: {"classification": {kind, severity, confidence, evidence}}
    ↓
5. decide_action node
   - Checks rules["monitor"].action_matrix
   - Severity "critical" → action: "require_approval"
   - Returns: {"action": "require_approval", "action_plan": {...}}
    ↓
6. approve_or_auto_act node
   - If action != "require_approval": auto-execute, skip approval
   - If action == "require_approval":
     a. Call notify_n8n() → POST to N8N_WEBHOOK_URL
        Webhook body: {event, signal_id, callback_url, thread_id, ...}
     b. Call interrupt() → LangGraph pauses the graph
     c. Checkpoint state to PostgreSQL
     d. Return {"approval": {required: true, ...}}
    ↓
[n8n receives webhook, notifies approver]
    ↓
[Approver clicks "Approve" link in email/Slack]
    ↓
[n8n POSTs back to callback_url with {"approved": true, "approver": "..."}]
    ↓
[Route handler receives POST /v1/signals/{id}/approve]
    ↓
resume_agent_graph(signal_id, approval_decision)
    ↓
[LangGraph resumes from interrupted node]
    ↓
7. approve_or_auto_act (resumed)
   - Reads approval from Command(resume=...)
   - Executes action: page on-call, notify ops, etc.
   - Returns: {"approval": {required: true, approved: true, decided_by: "on-call@..."}
    ↓
8. log_decision node
   - Creates DecisionLog row with status: "pending_approval" → "executed" | "dismissed"
    ↓
[Checkpointer updates Signal.status to "executed" or "dismissed"]
```

## Key Architectural Decisions

### 1. Nodes are Pure Functions

**Rule**: Nodes read from state, return partial state updates. NO database writes inside nodes.

**Why**:
- State is reproducible: same input → same output (idempotent)
- Graph can be paused/resumed without losing data
- Testing is trivial: mock state, verify output
- Concurrent runs don't interfere

**Pattern**:
```python
async def my_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Input state keys: X, Y. Output state keys: Z.
    
    Side-effect guarantees: None — only reads state, returns partial updates.
    Database writes happen in the route layer (via checkpointer).
    """
    X = state.get("X")
    Y = state.get("Y")
    Z = compute(X, Y)
    return {"Z": Z}
```

### 2. All Prompts are Jinja2 Files

**Rule**: No prompt text in Python code. All prompts live in `backend/context/prompts/`.

**Why**:
- Prompts are git-versioned (audit trail)
- Easy to update without redeploying code
- PromptLoader renders with context (rules, schemas, personas)
- Non-programmers can edit prompts

**Pattern**:
```python
async def my_node(state, config):
    loader = get_prompt_loader(config)
    prompt_text = loader.render("mode/prompt_name.md", var1=..., var2=...)
    result = await llm.chat(prompt_text)
    return {"output": result}
```

### 3. Context is Git-Versioned, Commit SHA is Audit Trail

**Rule**: Every DecisionLog row records `context_commit_sha`. Every context SHA gets a ContextSnapshot.

**Why**:
- Reproducible decisions: if a decision looks wrong, check what context was used
- Rollback capability: if a context update breaks agents, revert the commit
- Audit trail: who changed the rules/prompts and when?

**Pattern**:
```python
# In load_context node:
commit_sha = git_context_client.get_current_commit_sha()
context = load_yaml_files(...)
# Ensure ContextSnapshot is created:
create_context_snapshot(commit_sha, context)

# In log_decision node:
decision = DecisionLog(
    context_commit_sha=state["context_commit_sha"],
    input=...,
    output=...,
)
```

### 4. No Celery — Use asyncio + BackgroundTasks

**Rule**: Concurrent work happens via `asyncio.gather()` inside nodes or `BackgroundTasks` in routes.

**Why**:
- Simpler deployment (no separate worker pool)
- Shared context (no message serialization overhead)
- LangGraph checkpointer handles state persistence

**Pattern**:
```python
# Inside a node: parallel LLM calls
results = await asyncio.gather(
    llm.chat(...),
    llm.chat(...),
    llm.chat(...),
    return_exceptions=True,
)

# Inside a route: background signal processing
@router.post("/signals")
async def ingest_signal(payload: SignalCreate, bg: BackgroundTasks):
    signal = Signal(**payload.dict())
    db.add(signal)
    db.commit()
    
    bg.add_task(run_monitor_for_signal, signal.id, ...)
    return {"status": "queued", "signal_id": str(signal.id)}
```

### 5. DuckDB is Embedded (No Service)

**Rule**: `import duckdb` and connect to a local file. Raw SQL is ONLY allowed here.

**Why**:
- No separate service to deploy
- Fast for analytical queries
- Safe: only SELECT queries are allowed (other keywords forbidden by validate_sql)
- Easy to seed/reset

**Pattern**:
```python
class DuckDBClient:
    def __init__(self, db_path: str):
        self.db = duckdb.connect(db_path)
    
    def query(self, sql: str) -> list[dict]:
        """Execute a SELECT query."""
        result = self.db.execute(sql).fetchall()
        return [dict(row) for row in result]
```

### 6. Checkpointer Handles State Persistence

**Rule**: LangGraph's `AsyncPostgresSaver` writes node outputs to PostgreSQL checkpoint tables.

**Why**:
- Automatic: no manual DB writes in nodes
- Atomic: entire graph state is persisted or rolled back together
- Resumable: `interrupt()` pauses and later `resume()` continues
- Queryable: checkpoint tables are accessible for debugging

**Pattern**:
```python
checkpointer = AsyncPostgresSaver(
    conn=engine,
    table_name="checkpoints",
)
graph = builder.compile(checkpointer=checkpointer)
await graph.ainvoke(state, config)  # checkpoint persists state
```

### 7. Evaluation Harness Measures Quality

**Rule**: Every agent mode has a YAML evaluation suite with test cases and scorers.

**Scorers**:
- `exact_sql`: normalized SQL string comparison
- `execution_match`: both queries produce identical results
- `exact_match`: output field equals expected
- `llm_judge`: LLM-as-judge scores 0.0–1.0 against rubric

**Why**:
- Continuous evaluation of agent quality
- Regression detection: does a context update break agents?
- Score is recorded in DecisionLog (visible in dashboards)

**Pattern**:
```yaml
suite: analytics_suite
mode: analytics
cases:
  - id: total_revenue
    input:
      query: "What is the total revenue?"
    expected:
      sql: "SELECT SUM(amount) FROM orders"
    scorer: exact_sql
```

## Threading & Concurrency

**LangGraph is single-threaded per graph instance, but:**
- Multiple requests are handled concurrently (FastAPI + asyncio)
- Each request gets its own graph invocation
- No race conditions: checkpointer handles concurrent writes safely

**Inside nodes:**
- `await asyncio.gather()` for parallel LLM calls
- Each coroutine is independent (no shared state)
- Errors bubble up (caught by graph executor)

**Example: research mode**:
```python
# 3 peers respond in parallel
peer_results = await asyncio.gather(
    parallel_peers[0](state),
    parallel_peers[1](state),
    parallel_peers[2](state),
)  # all 3 complete before moving to next node
```

## Error Handling

**Node errors**:
- Caught by graph executor
- Recorded in DecisionLog.error
- Status set to "failed"
- No recovery: returned to client as error

**LLM errors**:
- Timeout: caught, retried (configurable)
- Rate limit: backoff + retry (LiteLLM handles)
- API down: returned as error, client retries

**Database errors**:
- Connection pool exhausted: wait + retry
- Transaction rollback: logged, decision marked "failed"
- Checkpoint write failure: graph invocation fails

## Security

### Input Validation

- **ChatCompletionRequest**: Pydantic validates structure, max message length
- **Signal payload**: Schema validation, max payload size
- **SQL**: Keyword blacklist (no INSERT/UPDATE/DELETE), max length

### Authentication

- **Bearer tokens**: `Authorization: Bearer sk-...` header
- **Whitelist**: `BACKEND_API_KEYS` env var (comma-separated tokens)
- **Routes**: `/v1/*` requires auth; `/health` is public

### Data Privacy

- **No PII storage**: only decisions (high-level) logged
- **Audit trail**: all decisions recorded with timestamp + approver
- **Encryption**: use TLS for all network communication

## Testing Strategy

### Levels

1. **Unit tests** (nodes):
   - Fake LLM, fake DB
   - Test node logic in isolation

2. **Integration tests** (graphs):
   - MemorySaver checkpointer
   - Scripted LLM responses
   - Full graph flow

3. **Route tests**:
   - FastAPI TestClient
   - Mock database, mock LLM
   - End-to-end API

4. **Evaluation tests**:
   - Real LLM (production data)
   - Score against expected outputs
   - Catch regressions

### Coverage

- Target: >80% code coverage
- Focus: node logic, validation, error paths
- Skip: infrastructure (Docker, k8s)

## Performance Considerations

### Latency SLAs

- **Analytics**: <5s (SQL generation + validation + execution)
- **Monitor**: <2s (detection + classification + decision)
- **Research**: 30–120s (3 peers × 2 generations, each +1 LLM call)
- **Simulate**: 20–60s (K variants × persona count × scoring)

### Optimization

- **Caching**: Redis for prompt templates, context manifests
- **Connection pooling**: DB pool size 20, max overflow 10
- **Parallel LLM**: `asyncio.gather()` for independent calls
- **Checkpoint cleanup**: old checkpoints archived/deleted

### Bottlenecks

- LLM latency (external, can't optimize)
- Database checkpoint writes (tune pool size)
- Redis connection (use sentinel for HA)

---

For operational details, see [DEPLOYMENT.md](DEPLOYMENT.md). For testing, see `tests/`.
