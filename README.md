# OpenAI Managed Agents Billing Middleware Template

This template exposes an OpenAI **Agents API** saved agent through a Firebase-authenticated Cloud Run gateway, streams its output to the app, and charges reported model-token usage through the existing central billing flow. It uses raw HTTP against the Agents API; it does not use the Agents SDK.

The template is a new sibling of the existing middleware templates. It follows the managed-agent gateway/persistence-worker boundary and retains Terraform-configured Firestore collection names so the same wallet and billing records can be shared.

## Request flow

1. The gateway authenticates the Firebase user and derives the billing subject, request ID, and idempotent turn ID on the server.
2. For a follow-up, it validates ownership of the existing thread and resolves the provider session from server-owned storage; this step must not create a provider session.
3. The gateway reserves wallet credit. If reservation fails, it returns the billing error without creating a provider session or submitting input to the Agents API.
4. Only after reservation succeeds, a new conversation creates its OpenAI-hosted session from the saved Agent ID. The OpenAI session ID is held in the server-owned thread record; clients must not choose it. The gateway then subscribes to the session event stream and submits the message with an `Idempotency-Key`.
5. The gateway streams text to the client and waits for the root turn to complete, fail, or cancel. Session-idle or stream-close alone is not considered success.
6. After the response completes, the gateway publishes raw per-turn usage reports to Pub/Sub. The persistence worker aggregates and prices those reports, writes the immutable billing ledger, and settles the wallet. Token counting and pricing do not run in the response-stream path.

## Billing preflight invariant — keep this order

Treat this ordering as a hard billing and side-effect boundary:

- Authenticate and validate existing thread/session ownership using server-owned state only.
- Reserve wallet credit. If reservation fails (including HTTP 402), return without creating a
  provider-side session or sending agent/model input.
- Only after reservation succeeds may the gateway create a provider-side session or submit
  agent/model input.
- If session creation fails after reservation, release the reservation idempotently.
- Keep buffered and streaming paths aligned; test that zero-credit requests return 402 with no
  provider-session or agent calls.

Purely local ID generation may happen before reservation only when it has no provider-side side effects.

Use the streaming route:

```http
POST /v1/agents/openai_managed_agent/chat/stream
Authorization: Bearer <Firebase ID token>
Content-Type: application/json

{"message":"Hello","client_turn_id":"a-unique-client-turn-id"}
```

For a follow-up, send the returned `thread_id` and (optionally) the returned `session_id`. The gateway checks thread ownership and resolves the provider session from Firestore. Never accept a client-provided billing subject, wallet amount, provider Agent ID, or usage report as authoritative.

## Agent and model configuration

The provided OpenAI Agent ID is configured in both `config/agents.dev.yaml` and `config/agents.prod.yaml`:

```yaml
remote_agent_id: agent_bc86941978354b21a682cde68398daca57998fc854d74ce3a1
model: gpt-6-luna
```

`openai_managed_agent` is this middleware's local route name, while `MaxBaris` is the saved Agent's dashboard display name. They do not need to match: the middleware addresses the saved Agent by `remote_agent_id`. The Agent's tools, instructions, reasoning, and multi-agent settings remain managed in the OpenAI dashboard and are not duplicated here.

The registry's `model` must match the model on the saved Agent and must have a current explicit rate card in `config/billing.prod.yaml`. The gateway checks the model returned while creating a session and fails closed on a mismatch. Update the model and catalog together if the saved Agent uses a different model. Agent ID is configuration, not a secret.

## Per-turn token-rate charging and its limits

Charging uses a **per-turn token rate card after the response**, not exact provider-cost pass-through. The worker prices reported `input_tokens` (splitting cached input where reported) and `output_tokens` (including reasoning) using the versioned local catalog. It aggregates root-agent and observed subagent turns. The response stream is not held open for usage recovery, token aggregation, or pricing; the existing persistence worker owns the immutable billing ledger and idempotent wallet settlement.

The Agents API reports best-effort usage; it can be null or revised after a turn, and does not expose a separate cache-write-token count. After the response, the worker makes up to three bounded, read-only lookup attempts when stream events omitted usage, with each attempt limited to three pages of recent session turns. The middleware therefore:

- Records a per-turn token-rate charge and `pricing_warnings` in the ledger rather than calling it an exact invoice total.
- Prices unreported cache writes as zero (not as a guessed surcharge), so estimates can understate model cost when a model charges for cache writes.
- Uses the short-context rate card as an explicit estimate because Agents API reports turn totals rather than per-model-call context tiers; a warning is added when aggregate input crosses the catalog threshold.
- Excludes OpenAI-hosted container/sandbox, tool, and third-party charges; those require separate provider usage data and reconciliation.
- Missing Agents API usage stays pending without delaying the streamed response or charging a partial/zero amount. Wallet reservation settlement/release is independent of provider-usage lookup: even after the hold is released, the worker continues targeted turn lookups with exponential backoff (defaults: 5 minutes to 6 hours) for up to 7 days. If usage arrives late, it is recorded as a separate immutable debit; any unpaid shortfall remains tracked and the worker continues trying to collect it when wallet funds are available. Missing or unresolved usage does not block another turn: the gateway rejects a request for insufficient credit only when the available balance cannot cover that request's configured reservation. If usage remains unavailable after the retention window, the turn is marked for manual billing review, but the account can continue using available credits. The system never guesses a zero charge. Configure this policy with `billing_usage_reconciliation_initial_backoff_seconds`, `billing_usage_reconciliation_max_backoff_seconds`, and `billing_usage_reconciliation_retention_seconds`.

The Cloud Scheduler billing reconciliation job defaults to every 5 minutes (`*/5 * * * *`) in this template; its schedule is configurable with `billing_reconciliation_schedule`.

Check the current [Agents API usage documentation](https://developers.openai.com/api/docs/guides/agents-api/observability) and [OpenAI API pricing](https://developers.openai.com/api/docs/pricing) before production pricing changes. The included `gpt-6-luna` catalog matches the existing OpenAI middleware catalog in this workspace; it is not a substitute for reviewing current rates.

## Credentials

The only provider credential required is an OpenAI API key authorized for Agents API read/write operations, Responses inference, and the saved Agent. Store it in Google Secret Manager; do not put it in YAML, Terraform variables, command-line arguments, source control, or FlutterFlow. Terraform grants the gateway and persistence-worker service accounts access to the secret. The worker uses the key only for bounded, read-only recovery of missing turn usage; it does not submit agent input.

The Terraform example references an existing secret:

```hcl
openai_api_key_secret_id      = "openai-api-key"
openai_api_key_secret_version = "1"
```

If the secret does not already exist, create it, then add a version without putting the key in shell history or a command argument:

```bash
gcloud secrets create openai-api-key --replication-policy=automatic
read -s -p "OpenAI API key: " OPENAI_API_KEY
printf '%s' "$OPENAI_API_KEY" | gcloud secrets versions add openai-api-key --data-file=-
unset OPENAI_API_KEY
```

If the secret already exists, skip the create command and add a new version. No API key is needed for unit tests or template construction; a live OpenAI smoke test and deployment require the key.

For local live testing, provide the key to the gateway process as the `OPENAI_API_KEY` environment variable. This template does not load secrets from a `.env` file. Do not paste the key into the agent YAML, Terraform variables, or source code.

## Build, deploy, and test

Use the existing Cloud Shell scripts, after reviewing `config/agents.prod.yaml`, `config/billing.prod.yaml`, Terraform project/region values, and the selected shared billing collection names:

```bash
bash scripts/cloudshell_build_middleware.sh
bash scripts/cloudshell_deploy_middleware.sh
```

Billing enforcement remains controlled by Terraform. Before enabling prepaid enforcement, verify reservation and settlement settings, wallet ownership, collection resolution, deployment identity, and end-to-end test results.

Run the test suite from this directory:

```bash
python -m pytest -q
```

The tests mock upstream HTTP and Firestore; passing them does not validate the supplied Agent ID, API key permissions, saved Agent's actual model, current OpenAI rates, or production wallet integration.

## Important lifecycle note

Archiving/deleting a local thread updates the middleware's thread lifecycle. The current persistence worker does not delete the corresponding OpenAI-hosted session. OpenAI session deletion/retention should be decided and implemented separately before promising remote data deletion to end users.
