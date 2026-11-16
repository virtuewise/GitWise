# GitWise

**The intelligence layer for software development.**

GitWise connects to GitHub repositories and builds a continuously evolving understanding of their code, architecture, dependencies, history, decisions, and risks.

Its long-term foundation is the **Software Genome**: a structured, evidence-backed record of what makes a software project what it is.

## Product vision

GitWise is designed to grow through five phases:

1. **Repository intelligence** — repository understanding, architecture maps, project memory, and pull request intelligence. Target: a usable MVP.
2. **Development intelligence** — intelligent testing, impact prediction, security insights, and development recommendations. Target: everyday development.
3. **Autonomous collaboration** — coordinated AI agents that investigate issues, propose changes, run tests, and submit pull requests under human supervision. Target: trusted AI-assisted engineering.
4. **Organization intelligence** — connected understanding across repositories, teams, services, and systems. Target: enterprise infrastructure.
5. **Global software intelligence** — an interoperable ecosystem for software knowledge, project histories, architecture intelligence, and trusted agent collaboration. Target: an industry-wide platform.

See [the detailed roadmap](docs/roadmap.md) for scope, acceptance goals, and delivery principles.

## Current project status

GitWise is at the foundation stage, building Phase 1. `POST /repositories/inspect` resolves a public GitHub repository and returns a bounded, commit-pinned Software Genome snapshot. Supply an optional `ref` to inspect a branch, tag, or commit. `POST /repositories/compare` accepts `base_ref` and `head_ref` and compares the two freshly ingested snapshots across supported Python architecture relationships and declared dependencies. The service statically parses selected files; it does not execute repository code. Snapshots are not yet exposed through the API. The repository includes PostgreSQL-backed immutable snapshot storage foundations, a grant-checked write path, GitHub webhook signature verification, durable delivery replay protection with retryable processing leases, and a narrowly scoped authenticated `ping` webhook endpoint. Verified GitHub App installation ownership, grant provisioning/revocation, tenant-isolated history APIs, and production database deployment remain unfinished.

## Product principles

- **Evidence before answers:** every finding should point to the files, symbols, commits, or metadata that support it.
- **Read-only by default:** repository access starts with the minimum permissions required.
- **Human control:** AI suggestions do not modify code or repository settings without explicit authorization.
- **Incremental understanding:** update project knowledge when repository contents change instead of rebuilding everything unnecessarily.
- **Portable project memory:** keep the Software Genome structured, versioned, and exportable.
- **Privacy and ownership:** repository owners control access to their code and whether knowledge may be shared.

## Initial capabilities

- **Repository Intelligence** — inspect public repository metadata and a bounded file tree; identify common project manifests and documentation entrypoints.
- **Architecture Vision** — map modules and dependencies with evidence.
- **Project Memory** — preserve decisions, conventions, and important historical context.
- **Pull Request Intelligence** — explain proposed changes and likely impact.
- **Guardian** — planned security, reliability, and maintenance insights.
- **GitWise Agents** — planned bounded development tasks under human supervision.

## Initial architecture

- `apps/api` — Python API (FastAPI)
- `apps/web` — web application (planned)
- `packages` — shared schemas and libraries (planned)
- `docs` — product and engineering decisions

## Local development

Requirements: Python 3.11+

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -e ".[dev]"
uvicorn gitwise_api.main:app --reload
```

The API will be available at http://127.0.0.1:8000 and its OpenAPI docs at http://127.0.0.1:8000/docs. Use `POST /repositories/inspect` with `{"url":"https://github.com/owner/repository","ref":"main"}` to create a read-only Software Genome snapshot. Use `POST /repositories/compare` with `{"url":"https://github.com/owner/repository","base_ref":"v1.0.0","head_ref":"main"}` to compare two refs.

## Configuration

Copy `.env.example` to `.env` and configure values as needed. Never commit tokens, private keys, or repository credentials.

## GitHub webhook endpoint

Configure `GITWISE_GITHUB_WEBHOOK_SECRET` with the webhook secret from the GitHub App and configure `GITWISE_DATABASE_URL` with PostgreSQL. `POST /webhooks/github` verifies `X-Hub-Signature-256` against the original raw body, enforces a 1 MB request limit, and records GitHub `ping` deliveries with recoverable processing state. Concurrent deliveries receive an in-progress response, failed or expired processing leases can be retried, completed deliveries are acknowledged as duplicates, and conflicting reuse is rejected. Other event types deliberately return `501` until verified installation-ownership and grant-lifecycle handlers are implemented. Do not enable installation or repository permission events yet.

## PostgreSQL snapshot storage

The first persistence migration creates provider repository records, revocable GitHub-user/App-installation access grants, and immutable Software Genome snapshots. The transactional store requires an active `write` or `admin` grant and treats a repeated identical write as idempotent; a different digest for the same snapshot identity is a conflict.

Configure `GITWISE_DATABASE_URL` with a PostgreSQL `asyncpg` URL, then apply the schema with:

```bash
alembic upgrade head
```

The migration and store are foundations only: no authenticated API endpoint provisions grants or exposes historical snapshots yet. Do not create grants directly from unverified request data, and do not enable private-repository ingestion until GitHub identity/installation verification, grant provisioning, revocation, and tenant-isolation tests are implemented.

## Security

GitWise should request the narrowest GitHub App permissions possible. Do not send private repository contents to external model providers without clear user consent and an explicit data-handling policy.

## License

A license has not yet been selected. Until one is added, all rights are reserved by the repository owner.
