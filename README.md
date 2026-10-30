# loresentry-ai-chat

Agent / AI Chat service for Lore Sentry.

Answers an author's questions about their own project. Each turn shows the model
a map of the project from `loresentry-content` (every document and memo with its
size, revision, the author's properties and a summary, plus relations). The model
asks for the documents it needs, most necessary first; the server reads them in
that order up to a per-step size limit. When the model writes an answer, two
reviews run before anything is sent: whether the documents read are enough to
answer (without seeing any text the model wrote) and whether the answer is
grounded in them. A rejected answer goes back to the model with the reasons.
The passing answer is sent over SSE with the documents it relied on. Sessions,
messages, held documents and per-turn metrics live in the `ai_chat` database.
There is no vector store and no agent framework; the loop is `app/agent.py`.

A second process from the same image, the summarizer (`python -m app.summarizer`),
consumes `content.project.changed.v1` and `content.project.changed.v2` and keeps
a summary of every document for the map.

Product requirements and the API contract:
[`docs/AI_CHAT_REQUIREMENTS.md`](https://github.com/soma-lorekeeper/docs/blob/main/AI_CHAT_REQUIREMENTS.md).
Design: [`AI_CHAT_NAVIGATION_DESIGN.md`](https://github.com/soma-lorekeeper/docs/blob/main/AI_CHAT_NAVIGATION_DESIGN.md),
[`AI_CHAT_NAVIGATION_IMPLEMENTATION.md`](https://github.com/soma-lorekeeper/docs/blob/main/AI_CHAT_NAVIGATION_IMPLEMENTATION.md).

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
| openai | 3.24.0 | Responses API with function tools and JSON schema output. Same version as `loresentry-graph-rag`. |
| confluent-kafka | 2.16.0 | Summarizer consumer. Same version as `loresentry-graph-rag`. |
| boto3 / botocore | 1.43.108 | KMS `Decrypt` of project keys in the summarizer. Same version as `loresentry-graph-rag`. |
| cryptography | 50.0.2 | AES-256-GCM for manuscript envelopes. |
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
| `GET` | `/projects/{id}/chat/availability` | `{status, size, limit}`; always `available` (kept for the frontend) |
| `GET` | `/projects/{id}/chat/sessions` | The caller's sessions, newest first |
| `POST` | `/projects/{id}/chat/sessions` | New session titled "새 대화"/"New conversation" |
| `GET` | `/projects/{id}/chat/search?q=` | Title matches first, then message text, with a snippet |
| `PATCH` | `/chat/sessions/{id}` | Rename |
| `DELETE` | `/chat/sessions/{id}` | Delete now, stopping its answer |
| `GET` | `/chat/sessions/{id}/messages` | `{messages, generating}` |
| `POST` | `/chat/sessions/{id}/messages` | Ask; answers as `text/event-stream` |
| `POST` | `/chat/sessions/{id}/cancel` | Stop the answer being written |
| `POST` | `/projects/{id}/setting-checks` | Start a setting error check of one chapter, `202` with the run |
| `GET` | `/projects/{id}/setting-checks/summary` | `{running, chapters}`: the latest run of each checked chapter |
| `GET` | `/projects/{id}/setting-checks?document_id=` | `{runs}` of a chapter, newest first |
| `GET` | `/projects/{id}/setting-checks/verdicts` | `{verdicts}`: every verdict of the project with its latest finding, newest first |
| `GET` | `/setting-checks/{id}` | One run with its findings |
| `POST` | `/setting-checks/{id}/cancel` | Stop a running check (`204` also when it is not running) |
| `PUT`, `DELETE` | `/setting-check-findings/{id}/verdict` | The author's verdict (`INTENDED`, `NOT_CONFLICT`) and reason |
| `DELETE` | `/users/me/data` | Account deletion (called by the gateway); also setting checks |
| `DELETE` | `/projects/{id}/chat/data` | Project deletion (called by the gateway); also setting checks |

### A turn

1. Checks before streaming answer with a JSON error: the question, the session,
   project access (`chat-size` on content), one answer at a time per session
   (`CHAT_ANSWER_IN_PROGRESS`), `replaces` must be the last question, and a
   repeated `client_message_id` replays the saved answer instead of asking again.
2. The question and a `GENERATING` row in `chat_turns` are committed, and the
   answer runs as a background task. Closing the browser does not stop it; only
   `cancel` or deleting the session does. A watcher polls the turn row, so a
   cancel reaching another replica still stops it.
3. Events: `phase` (`gathering` → `reading` → `writing`), `citation`, `delta`,
   then `done` with the saved question and answer (with `grounding`), or `error`.
   `: ping` every 15 seconds keeps the gateway and ALB from closing a quiet stream.
4. Input: project and author, the map (`chat-map` on content, with summaries from
   `document_summaries`; cache breakpoint), the documents this session already
   holds, re-read at their current revision (breakpoint), earlier turns, then the
   question. Ids are the last 8 hex digits of each UUID.
5. Loop, at most `CHAT_MAX_STEPS`: the model either calls `request_documents`
   (read through `chat-documents` up to `CHAT_STEP_MAX_CHARS`, in its order) or
   writes an answer. An answer gets the sufficiency check and the answer check in
   parallel; both must pass. Otherwise a `<review>` with the missing facts and
   problems is appended and the loop goes on. The last step must answer; without
   a pass, one more answer is written that says the manuscript gave no certain
   answer (`NOT_ENOUGH`). `CHAT_ANSWER_RESERVE_SECONDS` keeps time for it.
6. The passing answer goes through the citation parser: `<cite id="…" quote="…"/>`
   becomes `[[n]]` and `citations`, only documents actually read count, and a
   quote is kept only if it appears in that source.
7. Only finished answers are saved, with the documents read added to
   `chat_held_documents`. Tokens (summed over calls), steps, reviews, rejections
   and timings go to `chat_turns`; logs are JSON and never carry story text.

### Setting error check

Compares one manuscript chapter with the chapters before it and the setting
documents, and reports where the chapter contradicts them. It runs as a
background task in the API process (one replica), one running check per project.

1. `READING`: the chapter must be a manuscript on the chat map. The chapter,
   every earlier chapter and every setting document are read through
   `chat-documents` in batches of 100, settings with their `core_facts` (the
   paragraphs the author marked as core settings). Past `SETTING_CHECK_MAX_CHARS`
   the oldest chapters are left out; if the chapter and the settings alone do
   not fit, the run fails with `CHAT_CONTEXT_TOO_LARGE`.
2. `FINDING`: two passes in parallel with JSON schema output: the whole pass
   (chapter against earlier chapters and settings) and, when there are core
   facts, the core pass (every core fact against the chapter). Both quotes of a
   candidate must occur in their documents after whitespace and quote-mark
   normalisation, or it is dropped. Candidates with the same source and an
   overlapping quote are merged.
3. `JUDGING`: one call per candidate, `SETTING_CHECK_JUDGE_CONCURRENCY` at a
   time, with the chapter, the source document and the author's earlier
   verdicts on that source. Only `CONFLICT` is kept (not `CHANGED_OVER_TIME`,
   `CHARACTER_PERSPECTIVE` or `INSUFFICIENT`). The judge writes the
   `explanation` the author reads: three or four sentences on what the source
   establishes, what the chapter shows and why both cannot hold (an empty one
   falls back to the finding pass's summary and direction). It also names the shortest
   phrase in each quote that carries the contradiction (`quote_focus` and
   `source.quote_focus`); a phrase that is not in its quote after the same
   normalisation, or is longer than 120 characters, is stored as null.
4. Findings are saved in chapter order and the run becomes `DONE`, with tokens
   summed into the run. Checks do not count toward the daily chat usage.

A verdict is stored per project by fingerprint (`sha256` of kind, source,
normalised source quote and normalised quote), so it applies to the same finding
in later runs. The verdict ledger lists each verdict with the newest finding of
that fingerprint, its chapter and when it was decided. A cancelled run keeps no findings. A failed run keeps
`failure_code` (`CHAT_MODEL_UNAVAILABLE`, `CONTENT_UNAVAILABLE`,
`INTERNAL_ERROR`, …); a run still `RUNNING` after `SETTING_CHECK_STALE_SECONDS`
or stopped by a shutdown becomes `INTERRUPTED`.

### Summarizer

Consumes `FileChanged`/`ProjectDeleted` from every topic in `SUMMARY_TOPICS`
(group `ai-chat-summarizer`) into `summary_queue`, newest event only. A topic that
does not exist is logged and passed over. Events of one document are ordered by the
`ce_sequence` header (`event_seq`); `file.revision_no` is only kept to show which
revision a summary was written from, since a rename or a move does not raise it.
A document is removed when the event is `DELETED` or its `file.state` is
`TRASHED`, whatever the `change_type`. An event that does not follow content's
current contract (for example without `file.state` or `ce_sequence`) is logged
without its text and skipped. A removal that arrives after a newer change does
not undo it, but a change that arrives after a newer removal is summarized again
until the next event; a trashed document is not on the chat map, so it is never
shown. Every `SUMMARY_TICK_SECONDS` it takes up
to `SUMMARY_MAX_PER_MINUTE` documents untouched for `SUMMARY_IDLE_MINUTES`,
compares sentence fingerprints with the last summary and summarizes again only
when at least `min(SUMMARY_MIN_CHANGE_CHARS, length × SUMMARY_MIN_CHANGE_RATIO)`
characters changed. Bodies up to 500 characters are used as their own summary.
Trash, delete and project deletion remove summaries; project deletion also
removes setting checks and verdicts. Liveness is the mtime of
`SUMMARY_HEARTBEAT_PATH`.

A FileChanged either carries `file.body_text` or, from content's encrypted
manuscripts, `file.body_text_enc` (a manuscript envelope, purpose `body_text`,
subject the document id) with the project's `wrapped_key`; both forms are accepted
on both topics. The envelope and the wrapped key are queued as received
(`body_enc`, `wrapped_key`, with `body` empty) and opened only when the document is
summarized: KMS `Decrypt` of the wrapped key (`MANUSCRIPT_KEY_ID`, encryption
context `{"project_id": …}`, credentials from Pod Identity), cached in memory per
project for 10 minutes (at most 1000 keys), then AES-256-GCM and zlib
(`app/manuscript.py`). A KMS failure leaves the row queued for another minute
without using an attempt; an envelope that does not open is logged without its
text and dropped.

When the queued row has a wrapped key, every summary written for it, a copied
short body or a model summary, is stored only as an envelope (purpose `summary`,
subject the document id) in `summary_enc` with the same `wrapped_key`, and
`summary` is null. When such a row leaves the summary unchanged and the stored
summary is still plain text, it is sealed in place without calling the model, so
replaying FileChanged v2 for every document leaves no plain summary behind. Rows
from plain events keep plain summaries. The API opens sealed summaries for the map
with the same key cache, off the event loop; a summary whose key or envelope does
not open is left out of the map and logged without its text.

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
| `CHAT_TURN_DEADLINE_SECONDS` | `170` | Whole turn; below the gateway's 180s |
| `CHAT_STEP_MAX_CHARS` | `40000` | Characters read per step |
| `CHAT_MAX_STEPS` | `4` | |
| `CHAT_HELD_MAX_CHARS` | `200000` | Oldest held documents are dropped past this |
| `CHAT_HISTORY_MAX_CHARS` | `40000` | Earlier turns kept in the input |
| `CHAT_ANSWER_RESERVE_SECONDS` | `75` | Stop reading when less time is left |
| `CHAT_JUDGE_MODEL`, `CHAT_JUDGE_REASONING_EFFORT` | `OPENAI_MODEL`, `low` | Both reviews |
| `SETTING_CHECK_MAX_CHARS` | `600000` | Characters read by one setting check |
| `SETTING_CHECK_REASONING_EFFORT` | `medium` | Finding passes (`OPENAI_MODEL`) |
| `SETTING_CHECK_JUDGE_REASONING_EFFORT`, `SETTING_CHECK_JUDGE_MAX_OUTPUT_TOKENS` | `low`, `4000` | Judging (`CHAT_JUDGE_MODEL`) |
| `SETTING_CHECK_JUDGE_CONCURRENCY` | `4` | Judge calls at a time |
| `SETTING_CHECK_TIMEOUT_SECONDS` | `300` | Per model call |
| `SETTING_CHECK_DEADLINE_SECONDS`, `SETTING_CHECK_STALE_SECONDS` | `600`, `900` | Whole run; `RUNNING` older than this is `INTERRUPTED` |
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Summarizer |
| `SUMMARY_TOPICS` | `content.project.changed.v1,content.project.changed.v2` | Summarizer, comma-separated; `SUMMARY_TOPIC` still names a single topic |
| `MANUSCRIPT_KEY_ID` | — | API and summarizer. KMS key of the manuscript project keys, prod `alias/lore-sentry-manuscript`, dev `alias/lore-sentry-manuscript-dev`; unset, encrypted bodies stay queued and sealed summaries are left out of the map |
| `MANUSCRIPT_KEY_REGION` | `ap-northeast-2` | API and summarizer |
| `SUMMARY_OPENAI_API_KEY`, `SUMMARY_MODEL` | `OPENAI_API_KEY`, `OPENAI_MODEL` | Summarizer |
| `SUMMARY_IDLE_MINUTES`, `SUMMARY_MIN_CHANGE_CHARS`, `SUMMARY_MIN_CHANGE_RATIO` | `10`, `1000`, `0.3` | Summarizer |

## Schema

Migrations live in [`db/migration`](db/migration). The container entrypoint
runs `flyway migrate` against `DB_HOST`/`DB_PORT`/`DB_NAME` with
`DB_USERNAME`/`DB_PASSWORD` and only then starts uvicorn, so a failed migration
keeps the pod from becoming ready.

| Table | Purpose |
| --- | --- |
| `chat_sessions` | Per-project session: owner, title, preview of the last answer |
| `chat_messages` | Finished questions (intent, quote, `client_message_id`) and answers (citations); streaming chunks are not stored |
| `chat_turns` | One row per attempt: status, failure code, model, tokens, timings, steps, reviews and grounding. At most one `GENERATING` per session |
| `chat_held_documents` | Documents and memos a session has read, in reading order |
| `document_summaries` | One summary per document with its revision, sentence fingerprint and the `event_seq` it stands for; the summary is either in `summary` or sealed in `summary_enc` with `wrapped_key` |
| `summary_queue` | Latest change per document (by `event_seq`) waiting to be summarized, with its body either in `body` or sealed in `body_enc` with `wrapped_key` |
| `setting_check_runs` | One row per setting check: chapter and revision, status and stage, what was read, tokens. At most one `RUNNING` per project |
| `setting_check_findings` | Findings of a finished run in chapter order, with the explanation, both quotes, their focus phrases and the fingerprint |
| `setting_check_verdicts` | The author's verdict and reason per project and fingerprint, kept across runs |

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
