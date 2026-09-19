# loresentry-ai-chat

Agent / AI Chat service for Lore Sentry.

Handles per-project AI chat sessions: storing user messages and completed AI
responses, streaming response generation, cancel/retry handling, and agent
orchestration that requests RAG/GraphRAG context from `loresentry-graph-rag`.

Reached only through `loresentry-gateway` — this service is `ClusterIP` and has
no route from outside the cluster.

```
Cloudflare → ALB → gateway → ai-chat
```

Browsers never reach this service, so it has no CORS configuration — the gateway is
the only CORS boundary.

## Stack

| | Version | Notes |
| --- | --- | --- |
| Python | **3.12** | Matches `loresentry-graph-rag`; container base is `python:3.12-slim`. |
| FastAPI | 0.141.1 | |
| uvicorn | 0.52.4 (`[standard]`) | ASGI server. |
| pytest | 9.1.1 | |
| httpx | 0.28.1 | Required by `fastapi.testclient`. |
| Flyway | 12.11 (CLI) | Copied into the image from `flyway/flyway`; only the PostgreSQL plugin and driver are kept. |
| Port | 8000 | Platform convention, shared with every other service. |

Dependencies are pinned exactly rather than floored (`==`, not `>=`) so that a
CI run and a production image built a month apart resolve to the same tree.

## Endpoints

| Method | Path | Behaviour |
| --- | --- | --- |
| `GET` | `/health` | `{"status":"ok"}`. Used by the Kubernetes probes. |
| `GET` | `/` | `{"service":"ai-chat-api"}` |

The gateway exposes this service publicly at `GET /ai-chat`, which calls `/` here
and returns the payload nested under `upstream`.

## Schema

Migrations live in [`db/migration`](db/migration). The container entrypoint
runs `flyway migrate` against `DB_HOST`/`DB_PORT`/`DB_NAME` with
`DB_USERNAME`/`DB_PASSWORD` and only then starts uvicorn, so a failed migration
keeps the pod from becoming ready.

| Table | Purpose |
| --- | --- |
| `chat_sessions` | Per-project chat session: owner user id, title, soft delete |
| `chat_messages` | Completed user messages and AI responses (`COMPLETE`/`FAILED`/`CANCELLED`); streaming chunks are not stored |

Project and user ids are plain values; there are no cross-database foreign keys.
Running uvicorn directly, as below, skips the migration step.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
curl localhost:8000/health
```

## Test

```bash
pytest
```

No AWS access required. `tests/test_migrations.py` applies the migrations to a
`postgres:18` container through Testcontainers, so Docker must be running.

## Deploy

`main` push runs [`.github/workflows/ci-cd.yaml`](.github/workflows/ci-cd.yaml):

```
pytest → docker build → ECR ai-chat/api:build-<run>-<attempt>
       → invoke loresentry-update-gitops → commit to loresentry-gitops → Argo CD
```

CI never touches Kubernetes. The image tag in the GitOps repository's
`workload/overlays/prod/kustomization.yaml` is the deployment record, and a
rollback is `git revert` of that commit.

Deployed to the `prod` namespace of the `lore-sentry-k8s` EKS cluster via Argo CD.

## Not implemented yet

- Data access for sessions and messages on top of the schema.
- Streaming response generation, cancel/retry handling.
- Agent orchestration against `loresentry-graph-rag`.
