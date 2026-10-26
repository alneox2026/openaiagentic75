# OpenAI Managed Agents middleware architecture

```text
FlutterFlow client
  ├─ Firebase ID token
  ├─ message + optional middleware thread_id/client_turn_id
  └─ SSE response
        │
        ▼
Cloud Run gateway
  ├─ verifies Firebase identity and resolves server-owned billing subject
  ├─ creates/loads OpenAI-hosted session from saved remote_agent_id
  ├─ reserves wallet credit through Firestore transaction
  ├─ subscribes to session events, then submits an idempotent input event
  ├─ streams output and retains raw root/subagent turn usage reports
  └─ publishes completed turn and raw reports to Pub/Sub
        │
        ▼
Persistence worker
  ├─ recovers missing usage from the session with bounded read-only lookups
  ├─ aggregates tokens and applies the versioned per-turn rate card
  ├─ persists transcript and idempotency marker
  ├─ creates the agent billing ledger entry (usage-only recovery is allowed later)
  └─ settles when usage is ready, or releases unresolved holds after expiry
```

## OpenAI API contract

- `POST /v1/agents/sessions` uses the configured saved `agent_id` and `{type: "openai_hosted"}` environment. The saved session ID is stored in the server-owned thread record.
- `GET /v1/agents/sessions/{session_id}/events?stream=true` is opened before `POST /v1/agents/sessions/{session_id}/events` submits `agent.session.input.message`.
- The message submission carries the middleware's stable turn ID as `Idempotency-Key`.
- The stream ends only on root `agent.session.turn.completed`, `turn.failed`, or `turn.cancelled`; subagent terminal events are retained for usage aggregation.
- The gateway sends `Authorization: Bearer ...` and `OpenAI-Beta: agents=v1`. It does not call the Agents SDK.

## Billing and state boundaries

- Authentication, billing identity, reservation identity, Agent ID selection, and usage fields are server-owned. Clients cannot authoritatively set them.
- Firestore collection names are resolved through Terraform configuration. Runtime transcript collections may be stack-scoped; shared wallet, transaction, and billing-period collections retain the configured central names.
- The gateway streams; the worker owns durable transcript and financial mutations.
- Token aggregation and pricing happen in the persistence worker after the model response, not in the gateway stream. The customer response is not held open for usage recovery or pricing.
- Billing is a versioned per-turn token-rate charge, not exact provider-cost pass-through. Agents API usage is best effort, cache-write tokens are not separately exposed, and hosted environment/tool charges are excluded. See the root README's limitations.
- The worker retries read-only usage lookups when stream reports omit usage. If counts remain unavailable, the ledger records unavailable/unpriced rather than inventing token counts.

## Operations

- Store `OPENAI_API_KEY` in Secret Manager; never embed it in the registry or client. Terraform grants the gateway and worker access; the worker uses the key only for read-only missing-usage recovery.
- The registry model must match the saved Agent. Unknown models or missing price-card entries fail closed.
- This worker currently marks local thread deletion complete without deleting the OpenAI-hosted session. Do not promise remote session deletion until that lifecycle is implemented and tested.
