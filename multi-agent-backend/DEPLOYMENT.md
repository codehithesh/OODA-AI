# Deployment Guide

This guide covers production deployment of the OODA multi-agent backend.

## Prerequisites

- Docker + Docker Compose (v2+)
- PostgreSQL 16 (or use the Docker service)
- Redis 7 (or use the Docker service)
- LLM provider API key (OpenAI, Anthropic, etc.)
- (Optional) Sentry DSN for error tracking
- (Optional) Langfuse keys for LLM observability

## Pre-Deployment Checklist

### 1. Configuration

**Create a production `.env` file:**

```bash
cp .env.example .env
```

**Set these required values:**

```bash
# --- Core ---
ENVIRONMENT=production
DEBUG=false
BACKEND_API_KEYS=sk-prod-<random-token>   # generate a strong random key

# --- Database ---
POSTGRES_USER=<random-username>
POSTGRES_PASSWORD=<strong-password>        # generate a strong password
POSTGRES_DB=ooda_prod
DATABASE_URL=postgresql+asyncpg://<user>:<pass>@postgres:5432/ooda_prod

# --- LiteLLM routing ---
LITELLM_BASE_URL=http://litellm:4000
LITELLM_API_KEY=<random-litellm-key>
DEFAULT_MODEL=openai/gpt-4o-mini          # or your chosen model

# --- LLM provider keys (one or more) ---
OPENAI_API_KEY=sk-...                     # or set in litellm_config.yaml
ANTHROPIC_API_KEY=sk-ant-...

# --- Redis ---
REDIS_URL=redis://redis:6379/0

# --- Context (git-versioned agent code) ---
CONTEXT_DIR=/app/context

# --- DuckDB (analytics) ---
DUCKDB_PATH=/app/data/analytics.duckdb

# --- Observability ---
LOG_LEVEL=INFO
LOG_JSON=true                              # structured logging
SENTRY_DSN=https://...@sentry.io/...      # error tracking (optional)
LANGFUSE_PUBLIC_KEY=pk-...                # LLM tracing (optional)
LANGFUSE_SECRET_KEY=sk-...
LANGFUSE_HOST=https://cloud.langfuse.com

# --- n8n approval webhooks ---
N8N_WEBHOOK_URL=https://<your-domain>/webhook/ooda-approval

# --- Open WebUI ---
PUBLIC_BASE_URL=https://<your-domain>     # for redirect URIs, CORS, etc.
```

### 2. Secrets Management

**Use Docker secrets or environment variables from a secrets manager:**

```bash
# Option 1: Docker Secrets (Swarm/K8s)
docker secret create db_password <(echo -n "strong-password")
docker secret create litellm_key <(echo -n "sk-prod-...")

# Option 2: Environment file (less secure, use with caution)
echo "DB_PASSWORD=..." > /root/.secrets/db_password
chmod 600 /root/.secrets/db_password
```

Reference in `docker-compose.yml`:

```yaml
backend:
  environment:
    POSTGRES_PASSWORD_FILE: /run/secrets/db_password
    # or
    DATABASE_URL: postgresql+asyncpg://user:${DB_PASSWORD}@postgres:5432/ooda_prod
```

### 3. Database Setup

**Initialize PostgreSQL with backups:**

```bash
# Backup before upgrade
docker compose exec postgres pg_dump -U agent agent > backup-$(date +%s).sql

# Run migrations
docker compose exec backend alembic upgrade head

# Verify schemas
docker compose exec postgres psql -U agent -d agent -c "\dt"
```

### 4. Context Directory

**Ensure the git-versioned context is committed:**

```bash
cd backend/context
git init
git add .
git commit -m "Initial agent context"
```

**Mount as a read-only volume in production:**

```yaml
backend:
  volumes:
    - ./backend/context:/app/context:ro  # read-only
    - backend_data:/app/data
```

## Deployment Strategies

### Single-Machine Docker Compose

**Best for**: Development, small-scale testing, proof-of-concept

```bash
make dev  # starts all services with healthchecks

# Monitor
docker compose logs -f backend
docker compose ps  # view service status
```

### Multi-Node Kubernetes (Helm/Kustomize)

**Best for**: Production, multi-region, auto-scaling

See `k8s/` directory for Kustomize templates (if present) or create Helm charts:

```bash
# Deploy with Helm
helm install ooda-backend ./helm/ooda-backend \
  --namespace production \
  --values helm/values-prod.yaml
```

### AWS ECS / Fargate

**Best for**: AWS-hosted, serverless scaling

```bash
# Push backend image to ECR
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <account>.dkr.ecr.us-east-1.amazonaws.com

docker tag multi-agent-backend:latest <account>.dkr.ecr.us-east-1.amazonaws.com/ooda-backend:latest
docker push <account>.dkr.ecr.us-east-1.amazonaws.com/ooda-backend:latest

# Create ECS task definition, service, etc. (via CloudFormation or console)
```

## Production Hardening

### 1. Security

**Network:**
- Use TLS/HTTPS (nginx reverse proxy or AWS ALB)
- Restrict `/v1/*` access to known clients (whitelist IPs or use API gateway)
- Use VPN/bastion for database access

**Credentials:**
- Rotate API keys quarterly
- Use environment variables or secrets manager (not hardcoded)
- Audit token usage via logs

**Example nginx config:**

```nginx
upstream backend {
  server backend:8000;
}

server {
  listen 443 ssl;
  server_name api.example.com;

  ssl_certificate /etc/ssl/certs/cert.pem;
  ssl_certificate_key /etc/ssl/private/key.pem;

  location /v1/ {
    # Rate limit
    limit_req zone=api burst=20 nodelay;
    
    # Require Authorization header
    if ($http_authorization = "") {
      return 401;
    }

    proxy_pass http://backend;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
  }
}
```

### 2. Observability

**Logging:**
```bash
# Structured logs to file/syslog/cloud
docker compose logs backend | jq '.level, .event, .error'

# Or route to ELK/DataDog/CloudWatch
```

**Monitoring:**
```bash
# Metrics: container CPU, memory, request latency
# Use Prometheus or cloud provider (CloudWatch, Datadog)
# Alert on:
#   - High error rates (> 1%)
#   - Latency SLA breach (> 5s)
#   - Database connection pool exhaustion
#   - Signal queue backlog
```

**Tracing:**
- Enabled via Langfuse (LLM call traces) and structlog (request traces)
- Dashboard: https://cloud.langfuse.com (if configured)

### 3. Database

**Backups:**
```bash
# Daily automated backups
0 2 * * * docker compose exec -T postgres pg_dump -U agent agent | gzip > /backups/agent-$(date +\%Y\%m\%d).sql.gz

# Test restore monthly
pg_restore -U agent -d agent_test /backups/agent-20260801.sql.gz
```

**High availability (optional):**
- Use managed PostgreSQL (AWS RDS, Azure Database for PostgreSQL)
- Enable automated failover
- Set up read replicas for analytics queries

**Connection pooling:**
```yaml
backend:
  environment:
    DB_POOL_SIZE: 20
    DB_MAX_OVERFLOW: 10
    DB_POOL_TIMEOUT: 30
```

### 4. Caching

**Redis:**
- Use managed Redis (AWS ElastiCache, Azure Cache)
- Enable persistence (RDB snapshots)
- Set eviction policy: `allkeys-lru`

```yaml
redis:
  command: redis-server --maxmemory-policy allkeys-lru --appendonly yes
```

### 5. n8n Approval Workflow

**Secure the webhook:**
```yaml
n8n:
  environment:
    N8N_WEBHOOK_AUTH_HEADER_NAME: Authorization
    N8N_WEBHOOK_AUTH_HEADER_VALUE_PREFIX: Bearer
```

**Audit approvals:**
```sql
SELECT signal_id, approver, decision_time, approved
FROM approval_audit
WHERE decision_time > NOW() - INTERVAL 7 DAY
ORDER BY decision_time DESC;
```

## Post-Deployment

### 1. Smoke Tests

```bash
# List models
curl -s https://api.example.com/v1/models | jq '.data[].id'

# Test analytics
curl -X POST https://api.example.com/v1/chat/completions \
  -H "Authorization: Bearer sk-prod-..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "analytics",
    "messages": [{"role": "user", "content": "total revenue?"}]
  }'

# Test monitor
curl -X POST https://api.example.com/v1/chat/completions \
  -H "Authorization: Bearer sk-prod-..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "monitor",
    "messages": [{"role": "user", "content": "{\"metric\": \"error_rate\", \"value\": 0.05}"}]
  }'
```

### 2. Evaluation Tests

```bash
make eval  # run evaluation suites (production data, real LLM)

# Review results
docker compose exec backend python eval_harness.py --suite analytics_suite --verbose
```

### 3. Load Testing

```bash
# Use wrk or k6 to simulate traffic
k6 run load-test.js

# Expected: >100 req/s, <200ms p95 latency
```

## Scaling

### Horizontal Scaling (Multiple Backends)

```yaml
services:
  backend:
    deploy:
      replicas: 3  # Docker Swarm
  
  backend-lb:
    image: nginx:latest
    ports:
      - "8000:8000"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      - backend
```

### Vertical Scaling (Larger Instance)

```yaml
backend:
  environment:
    DB_POOL_SIZE: 50
    DB_MAX_OVERFLOW: 30
  deploy:
    resources:
      limits:
        cpus: "4"
        memory: 8G
      reservations:
        cpus: "2"
        memory: 4G
```

## Rollback & Recovery

**Backup current state before updates:**
```bash
docker compose stop
tar -czf backup-prod-$(date +%s).tar.gz backend/ multi-agent-backend/ .env
```

**Rollback if issues:**
```bash
docker compose down
tar -xzf backup-prod-<timestamp>.tar.gz
docker compose up -d
```

**Database recovery:**
```bash
# Restore from backup
pg_restore -d agent < backup-agent-20260801.sql
# Run migrations again
make migrate
```

## Monitoring Checklist

- [ ] Backend health endpoint responds (`GET /health`)
- [ ] PostgreSQL backups run daily
- [ ] Redis memory usage < 80%
- [ ] Error rate < 1% (alert at > 5%)
- [ ] p95 latency < 5s (alert at > 10s)
- [ ] Pending signals < 100 (alert at > 500)
- [ ] LiteLLM cost tracking (alert monthly budget)
- [ ] n8n approvals processed < 1 hour (alert at > 24h)

## Support & Troubleshooting

**Common issues:**

| Issue | Cause | Solution |
|-------|-------|----------|
| 502 Bad Gateway | Backend down or slow | Check logs, scale vertically, reduce pool size |
| SQL validation fails | Forbidden keywords | Review rules, adjust schema rules |
| LLM calls timeout | Rate limit or API down | Check provider status, increase timeout |
| Signal backlog grows | Approvals slow | Increase n8n workers, auto-dismiss low-severity |
| OOM (backend) | Checkpointer backlog | Enable checkpoint cleanup job, increase memory |

**Logs to check:**
```bash
docker compose logs backend --tail 1000 | grep -i error
docker compose logs postgres --tail 500
docker compose logs redis --tail 500
```

---

For questions, see [README.md](README.md) or [github.com/...](https://github.com/...).
