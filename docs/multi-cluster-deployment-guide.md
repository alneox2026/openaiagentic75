# Multi-Stack Cloud Shell Deployment Guide

Use this guide whenever a copy of this template is deployed beside another
middleware in the same Google Cloud project.

## What is isolated automatically

For a repository cloned as `~/agentic4`, the standard Cloud Shell scripts use:

| Concern | Derived value |
| --- | --- |
| Container images | `agentic4-gateway`, `agentic4-persistence-worker` |
| Terraform remote state | `stacks/agentic4/middleware` |
| Firestore namespace | `agentic4` (for example, `customer_wallets_agentic4`) |

This keeps image tags, Terraform state, and application data separate. The
clone directory must be lowercase and start with a letter. Use
`MIDDLEWARE_STACK_NAME` only if the directory cannot meet that convention.

## Before the first deployment

1. Copy the template into a new repository and choose a unique lowercase
   repository/directory name, such as `agentic4`.
2. In `infra/terraform/variables.tf`, set unique Cloud Run service names,
   service-account IDs, and `pubsub_topic_name`. Keep service-account IDs at
   30 characters or fewer. The deploy script rejects the inherited
   `ceoagent-…-v3` names for a new stack.
3. Configure the agents in `config/agents.prod.yaml` and the desired billing
   catalog. The Cloud Shell deploy script generates isolated Firestore
   collection names by default. For an intentionally shared billing design,
   set only the needed per-collection `FIRESTORE_…_COLLECTION` overrides
   documented in `README.md`; keep unrelated middleware data isolated.
4. Commit and push the repository.

## Cloud Shell workflow

```bash
cd ~
git clone https://github.com/YOUR_ORG/agentic4.git
cd ~/agentic4

# Run this update sequence before every build/deployment.
git fetch origin main
git reset --hard origin/main

# Set these only when deploying outside the default project/region/repository.
export PROJECT_ID="YOUR_PROJECT_ID"
export REGION="us-central1"
export REPOSITORY="ceosystem"

bash ./scripts/cloudshell_build_middleware.sh
bash ./scripts/cloudshell_deploy_middleware.sh
```

The build script publishes immutable commit-tagged images and updates only this
stack's `:latest` tags. The deployment script resolves those tags to digests,
so a deployed revision is pinned to the exact images it planned with.

## Safety behavior

- Resource importing is disabled by default. `IMPORT_EXISTING_RESOURCES=true`
  is a recovery-only operation for a lost state file, after confirming every
  resource belongs to this stack.
- The deploy script stops when Terraform proposes a delete or replacement. Do
  not set `ALLOW_TERRAFORM_DELETES=true` unless the destruction has been
  explicitly reviewed and is intentional.
- Terraform owns the cancellation-request composite index. Do not run
  `firebase deploy --only firestore:indexes` for a stack created from this
  template.
- `scripts/provision_test_wallet.py` requires a stack namespace or explicit
  collection names, so it cannot silently write to shared `*_v3` collections.

## Existing legacy deployments

Do not point a new stack at an old Terraform state prefix. To update an
already-deployed legacy stack, first inspect its current backend prefix and
resource names. Pass its existing `TF_STATE_PREFIX` only after confirming it
is the state that owns the resources being updated. If state recovery is
needed, use the documented recovery import with explicit resource-name
environment variables. Never delete Cloud Run services, Pub/Sub topics, or
service accounts simply to make a 409 error disappear.
