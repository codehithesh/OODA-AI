# n8n Approval Workflow for Monitor Agent

This guide shows how to build an n8n workflow that integrates with the OODA backend's
monitor agent for human approval gates.

## Architecture

```
OODA Backend (monitor graph paused)
    ↓ (POST to N8N_WEBHOOK_URL)
[n8n Webhook] → [Notify Approver] → [Wait for Response] → [HTTP Callback]
    ↓
OODA Backend (graph resumes with approval decision)
```

## Setup

### 1. Configure the backend to send webhooks

In your `.env`:

```bash
N8N_WEBHOOK_URL=http://localhost:5678/webhook/ooda-approval
```

The backend will POST an approval request to this URL when a monitor action
has `require_approval` status.

### 2. Create an n8n workflow

1. Log in to n8n (http://localhost:5678)
2. Click **+ New Workflow** and name it `OODA Signal Approval`
3. Add the nodes below (in order)

#### Node 1: Webhook Trigger

- **Type**: Webhook
- **HTTP Method**: POST
- **Authentication**: Set to "Basic Auth"
  - Username: `admin`
  - Password: (empty or your n8n password)
- **Path**: `ooda-approval`

This listens for POST requests from the backend.

#### Node 2: Extract Fields (Set node)

Extract and rename fields from the webhook payload for readability:

- **Action**: Set (Set Manually)
- **Fields to Set**:
  ```
  signal_id        = $json.signal_id
  thread_id        = $json.thread_id
  event_type       = $json.event
  signal_kind      = $json.kind
  severity         = $json.severity
  summary          = $json.summary
  action_plan      = $json.action_plan
  callback_url     = $json.callback_url
  classification   = $json.classification
  ```

#### Node 3: Notify Approver (Send Email or Slack)

Choose based on your preference:

**Option A: Email**

- **Type**: Send Email
- **To**: `on-call@company.com`
- **Subject**: `⚠️ OODA Signal Approval Required: {{ $json.summary }}`
- **Text**:
  ```
  Signal ID: {{ $json.signal_id }}
  Kind: {{ $json.signal_kind }}
  Severity: {{ $json.severity }}
  Source: {{ $json.event_type.source }}
  
  Summary: {{ $json.summary }}
  
  Classification:
  {{ JSON.stringify($json.classification, null, 2) }}
  
  Action Plan:
  {{ JSON.stringify($json.action_plan, null, 2) }}
  
  ---
  
  To approve, visit the admin dashboard:
  http://localhost:8000/admin/signals/{{ $json.signal_id }}
  ```

**Option B: Slack**

- **Type**: Slack
- **Channel**: `#incidents`
- **Text**:
  ```
  :warning: OODA Signal Approval Required

  *Signal*: {{ $json.summary }}
  *Kind*: {{ $json.signal_kind }}
  *Severity*: {{ $json.severity }}

  <{{ $json.callback_url }}?approved=true&approver=slack|Approve> | <{{ $json.callback_url }}?approved=false&approver=slack|Dismiss>
  ```

#### Node 4: Wait for Webhook Response (Continue node)

- **Type**: Continue
- This waits for the approver's response (backend will POST back to a specific endpoint)

If using Slack reactions, you can add a **Wait** node (e.g., 1 hour) before auto-dismissing.

#### Node 5: Send Approval Decision to Backend

- **Type**: HTTP Request
- **Method**: POST
- **URL**: `{{ $json.callback_url }}`
- **Headers**:
  ```
  Authorization: Bearer sk-local-dev
  Content-Type: application/json
  ```
- **Body**:
  ```json
  {
    "approved": true,
    "approver": "on-call@company.com",
    "decision_notes": "Approved — error rate returning to normal"
  }
  ```

#### Node 6: Log Result

- **Type**: Set
- **Action**: Log
- **Message**:
  ```
  Approval sent for signal {{ $json.signal_id }}: {{ $json.approved ? 'APPROVED' : 'DISMISSED' }}
  ```

### 3. Activate the workflow

1. Click **Activate** (toggle on)
2. Copy the webhook URL (shown in the Webhook node)
3. Update your `.env` if the path differs:
   ```bash
   N8N_WEBHOOK_URL=http://localhost:5678/webhook/ooda-approval
   ```

## Testing

### Trigger a signal that requires approval

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "monitor",
    "messages": [{
      "role": "user",
      "content": "{\"metric\": \"error_rate\", \"value\": 0.31, \"source\": \"payments-api\"}"
    }]
  }'
```

The backend will:
1. Run the monitor graph
2. Detect the signal (error_rate > 0.05)
3. Classify it as `error_burst` / `critical`
4. Decide action: `require_approval`
5. **Pause the graph** and POST to your n8n webhook
6. Wait for approval

Your n8n workflow will:
1. Receive the POST
2. Send an email/Slack notification
3. Wait for approver response
4. POST back to the callback URL with `{"approved": true/false, "approver": "..."}`

The backend will:
1. Receive the approval response
2. Resume the paused graph with the approval decision
3. Execute the action (or dismiss the signal)
4. Update the `Signal` row status to `executed` or `dismissed`
5. Finalize the `DecisionLog` row

## Advanced: Manual Approval via Admin Dashboard

If you prefer manual approval without email/Slack:

1. Add a **Wait** node (e.g., 24 hours) in the workflow
2. In the backend, expose a `/admin/signals/{id}/approve` endpoint (already exists)
3. Approvers visit `http://localhost:8000/admin/signals/<id>?approved=true`
4. The workflow polls `/v1/signals/{id}/approve_status` until approval arrives

## Troubleshooting

**Workflow not triggering?**
- Check `N8N_WEBHOOK_URL` in `.env`
- Verify n8n is running: `docker compose logs n8n`
- Check webhook URL in n8n matches (including protocol, host, path)

**Callback failing?**
- Ensure `Authorization: Bearer sk-local-dev` header matches `BACKEND_API_KEYS`
- Verify callback URL is reachable from within the n8n container
- Check backend logs: `docker compose logs backend | grep callback`

**Signal not pausing?**
- Verify `monitor_graph.py` includes `interrupt()` call in `approve_or_auto_act` node
- Check `langgraph.types.interrupt` is imported
- Review LangGraph checkpoint state in PostgreSQL: `SELECT * FROM checkpoint WHERE thread_id = '...'`

## Production Notes

- Use environment-based secrets in n8n (Settings → Variables)
- Rotate `BACKEND_API_KEYS` regularly
- Log all approvals to a separate audit table
- Set appropriate timeouts on **Wait** nodes (don't leave signals pending forever)
- Monitor webhook failures and retry logic
