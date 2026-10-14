# loresentry-ai-chat

Agent / AI Chat service for Lore Sentry.

Answers an author's questions about their own project. Each turn reads the whole
project from `loresentry-content` (manuscripts, setting documents, relations and
memos), sends it to OpenAI in one call, and streams the answer back over SSE with
the documents it relied on. Sessions, messages and per-turn metrics live in the
`ai_chat` database. There is no vector store and no agent framework.

Product requirements and the API contract:
[`docs/AI_CHAT_REQUIREMENTS.md`](https://github.com/soma-lorekeeper/docs/blob/main/AI_CHAT_REQUIREMENTS.md).

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
| httpx | 0.28.1 | Client for `loresentry-content`; also used by `fastapi.testclient`. |
| psycopg / psycopg-pool | 3.3.5 / 3.3.3 | Async connection pool. |
| openai | 3.24.0 | Responses API, streamed. Same version as `loresentry-graph-rag`. |
| ruff | 0.17.0 | Lint and format, run in CI. |
| Flyway | 12.11 (CLI) | Copied into the image from `flyway/flyway`; only the PostgreSQL plugin and driver are kept. |
| Port | 8000 | Platform convention, shared with every other service. |

Dependencies are pinned exactly rather than floored (`==`, not `>=`) so that a
CI run and a production image built a month apart resolve to the same tree.

## Endpoints

Every chat route needs `X-User-Id`. The gateway adds `X-User-Locale` and
`X-User-Display-Name` (percent-encoded) when creating a session and sending a
question. Errors are `{code, message, next_action}`.

| Method | Path | Behaviour |
| --- | --- | --- |
| `GET` | `/health`, `/health/db`, `/` | Probes and service name |
| `GET` | `/projects/{id}/chat/availability` | `{status, size, limit}`; `too-large` past `CHAT_CONTEXT_LIMIT` |
| `GET` | `/projects/{id}/chat/sessions` | The caller's sessions, newest first |
| `POST` | `/projects/{id}/chat/sessions` | New session titled "새 대화"/"New conversation" |
| `GET` | `/projects/{id}/chat/search?q=` | Title matches first, then message text, with a snippet |
| `PATCH` | `/chat/sessions/{id}` | Rename |
| `DELETE` | `/chat/sessions/{id}` | Delete now, stopping its answer |
| `GET` | `/chat/sessions/{id}/messages` | `{messages, generating}` |
| `POST` | `/chat/sessions/{id}/messages` | Ask; answers as `text/event-stream` |
| `POST` | `/chat/sessions/{id}/cancel` | Stop the answer being written |
| `DELETE` | `/users/me/data` | Account deletion (called by the gateway) |
| `DELETE` | `/projects/{id}/chat/data` | Project deletion (called by the gateway) |

### A turn

1. Checks before streaming answer with a JSON error: the question, the session,
   the project size (`chat-size` on content), one answer at a time per session
   (`CHAT_ANSWER_IN_PROGRESS`), `replaces` must be the last question, and a
   repeated `client_message_id` replays the saved answer instead of asking again.
2. The question and a `GENERATING` row in `chat_turns` are committed, and the
   answer runs as a background task. Closing the browser does not stop it; only
   `cancel` or deleting the session does. A watcher polls the turn row, so a
   cancel reaching another replica still stops it.
3. Events: `phase` (`gathering` → `reading` → `writing`), `citation`, `delta`,
   then `done` with the saved question and answer, or `error`. `: ping` every
   15 seconds keeps the gateway and ALB from closing a quiet stream.
4. The prompt is built in a fixed order so OpenAI's prompt cache can reuse it:
   project and author, earlier chapters, setting documents, relations, memos
   (explicit cache breakpoint), the last three chapters (breakpoint), earlier
   turns of the session (breakpoint), then the question. The model marks sources
   as `<cite id="D12" quote="…"/>`; the server turns them into `[[n]]` and
   `citations`, and keeps a quote only if it really appears in that source.
5. Only finished answers are saved. Tokens (including cached), timings and the
   snapshot size go to `chat_turns`; logs are JSON and never carry story text.

## Configuration

| Variable | Default | |
| --- | --- | --- |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USERNAME`, `DB_PASSWORD` | local `ai_chat` | |
| `CONTENT_BASE_URL` | `http://localhost:8001` | prod `http://content-api` |
| `OPENAI_API_KEY` | — | Secret `ai-chat-openai` |
| `OPENAI_MODEL` | `gpt-6-luna` | |
| `OPENAI_REASONING_EFFORT` | `medium` | Empty to omit |
| `OPENAI_MAX_OUTPUT_TOKENS` | `32000` | Includes reasoning tokens |
| `OPENAI_TIMEOUT_SECONDS` | `120` | Per read from OpenAI |
| `CHAT_TURN_DEADLINE_SECONDS` | `170` | Whole model call; below the gateway's 180s |
| `CHAT_CONTEXT_LIMIT` | `1400000` | Characters |
| `CHAT_RECENT_MANUSCRIPTS` | `3` | Chapters placed after the cache breakpoint |

## Schema

Migrations live in [`db/migration`](db/migration). The container entrypoint
runs `flyway migrate` against `DB_HOST`/`DB_PORT`/`DB_NAME` with
`DB_USERNAME`/`DB_PASSWORD` and only then starts uvicorn, so a failed migration
keeps the pod from becoming ready.

| Table | Purpose |
| --- | --- |
| `chat_sessions` | Per-project session: owner, title, preview of the last answer |
| `chat_messages` | Finished questions (intent, quote, `client_message_id`) and answers (citations); streaming chunks are not stored |
| `chat_turns` | One row per attempt: status, failure code, model, tokens, timings. At most one `GENERATING` per session |

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

No AWS or OpenAI access required: the API tests use a fake content source and a
fake model. `tests/test_migrations.py` and `tests/test_chat_api.py` run against a
`postgres:18` container through Testcontainers, so Docker must be running.

```bash
ruff check app tests && ruff format --check app tests
```

## Deploy

`main` push runs [`.github/workflows/ci-cd.yaml`](.github/workflows/ci-cd.yaml):

```
ruff → pytest → docker build → ECR ai-chat/api:build-<run>-<attempt>
       → invoke loresentry-update-gitops → commit to loresentry-gitops → Argo CD
```

CI never touches Kubernetes. The image tag in the GitOps repository's
`workload/overlays/prod/kustomization.yaml` is the deployment record, and a
rollback is `git revert` of that commit.

Deployed to the `prod` namespace of the `lore-sentry-k8s` EKS cluster via Argo CD.

## Not implemented yet

- Relations come from `loresentry-content` (`document_relations`), not from
  `loresentry-graph-rag`, until the graph service is ready.
- No usage limits; the gateway lets only allow-listed users in.
