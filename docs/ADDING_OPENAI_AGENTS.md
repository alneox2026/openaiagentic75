# Registering an OpenAI managed Agent

This middleware is for saved agents created with OpenAI's Agents API. It does not support the OpenAI Agents SDK as an upstream execution path.

1. Create/choose a saved Agent with an OpenAI API project key that can access it.
2. Add a registry entry to `config/agents.dev.yaml` and `config/agents.prod.yaml` containing an internal `agent_id`, `backend: openai_managed`, provider `remote_agent_id`, and the exact saved Agent model.
3. Add a versioned model rate card to `config/billing.prod.yaml` and `config/billing.test.yaml`. Keep `pricing_version` explicit and verify the current OpenAI rates before changing it.
4. Store the project API key in Secret Manager and reference it with `openai_api_key_secret_id` and `openai_api_key_secret_version` in Terraform configuration. It needs Agents API read/write and Responses inference permissions; Terraform gives the worker read access only for post-response usage recovery.
5. Run unit tests, then a non-production end-to-end test with wallet reservations and settlement verified in Firestore before production billing enforcement.

The client must send a Firebase ID token and only the message plus an optional server-owned middleware `thread_id` and stable `client_turn_id`. Never expose the provider Agent ID, API key, billing subject, reservation amount, or usage metadata to the client.

The gateway verifies the saved Agent's model during session creation and fails closed if it does not match the registry billing model. A new saved Agent model must not receive requests until the rate card and configuration are updated together.

Token counting and pricing happen in the persistence worker after the response, using a versioned per-turn token rate card. This is not exact provider-cost pass-through. Agents API usage is best effort; cache-write token count and per-model-call context tiers are not available in reported turn usage, and hosted environment/tool costs are excluded. The ledger preserves these limitations as warnings. If bounded worker lookups cannot recover usage, the ledger marks the charge unavailable rather than guessing.
