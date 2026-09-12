# loresentry-ai-chat

Agent / AI Chat service for Lore Sentry.

Handles per-project AI chat sessions: storing user messages and completed AI
responses, streaming response generation, cancel/retry handling, and agent
orchestration that requests RAG/GraphRAG context from `loresentry-graph-rag`.

- Stack: FastAPI
- Database: MySQL
- Deployed to the `prod` namespace of the `lore-sentry-k8s` EKS cluster via Argo CD
