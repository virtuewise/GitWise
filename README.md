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

GitWise is at the foundation stage, building Phase 1. `POST /repositories/inspect` resolves a public GitHub repository and returns a bounded, commit-pinned Software Genome snapshot. Supply an optional `ref` to inspect a branch, tag, or commit. `POST /repositories/compare` accepts `base_ref` and `head_ref` and compares the two freshly ingested snapshots across supported Python architecture relationships and declared dependencies. The service statically parses selected files; it does not execute repository code. Snapshots are not yet exposed through the API. The repository includes PostgreSQL-backed immutable snapshot storage foundations, a grant-checked write path, GitHub webhook signature verification, durable delivery replay protection with retryable, fenced processing leases; verified GitHub App installation ownership; and one-repository token scope verification against immutable repository IDs. Signed GitHub App installation lifecycle events now invoke App-verified identity checks and transactional, read-only grant reconciliation. A GitHub user identity client can verify the authenticated user and the installations visible to that user; the internal snapshot store can authorize reads through those verified installation IDs. GitHub App user authorization/session issuance, tenant-isolated history APIs, and production database deployment remain unfinished.

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

Configure `GITWISE_GITHUB_WEBHOOK_SECRET` with the webhook secret from the GitHub App and configure `GITWISE_DATABASE_URL` with PostgreSQL. `POST /webhooks/github` verifies `X-Hub-Signature-256` against the original raw body, enforces a 1 MB request limit, and records GitHub deliveries with recoverable processing state. It handles `ping`, `installation` (`created`, `deleted`, `suspend`, `unsuspend`), and `installation_repositories` (`added`, `removed`) events. Lifecycle events require `GITWISE_GITHUB_APP_ID`, `GITWISE_GITHUB_APP_PRIVATE_KEY`, `GITWISE_GITHUB_WEBHOOK_SECRET`, and PostgreSQL. Concurrent deliveries receive an in-progress response, failed or expired processing leases can be retried, completed deliveries are acknowledged as duplicates, and conflicting reuse is rejected. Other event types deliberately return `501`. Installation repository additions are verified against GitHub using one-repository, read-scoped tokens and immutable numeric repository IDs. Every installation/repository lifecycle delivery is serialized per installation with a PostgreSQL advisory lock and reconciled against GitHub's current repository list using a short-lived metadata-only token. The grant set is updated in one PostgreSQL transaction, so delayed or out-of-order add/remove events cannot overwrite newer access state; still-selected repositories remain granted and only inaccessible repositories are revoked. Deletion and confirmed suspension revoke all grants for the installation. Installation creation and unsuspension reconcile grants against GitHub's current installation repository listing, obtained with a short-lived metadata-only token; only currently accessible repositories remain granted.

## GitHub App identity foundation

The GitHub App client authenticates with short-lived RS256 App JWTs, verifies that an installation belongs to the configured App, and can mint a token scoped to one repository. Before returning that token, it checks the installation account, the repository's immutable numeric ID and full name from GitHub's token response, and the explicit contents: read permission. A repository name supplied by an event is never treated as sufficient identity. The lifecycle processor in `github_installation_events.py` verifies signed event identity against the configured App, then confirms added repositories with GitHub before granting access. Deleted/suspended installations have their active grants revoked. Grant reconciliation is transactional and idempotent; delivery completion follows the committed database effects.

Webhook delivery records now have explicit pending, processing, processed, and failed states, retry leases, attempt counts, and per-claim fencing tokens. Stale workers cannot complete a delivery after a newer claim has taken ownership. Lifecycle event effects are applied transactionally to the grant tables before delivery completion. The external GitHub identity check is intentionally outside the database transaction; if processing fails after the grant transaction commits, replay is safe because grant operations are idempotent.

## PostgreSQL snapshot storage

The first persistence migration creates provider repository records, revocable GitHub-user/App-installation access grants, and immutable Software Genome snapshots. The transactional store requires an active `write` or `admin` grant for writes and an active `read`, `write`, or `admin` grant for reads. Missing and unauthorized snapshots return the same access-denied result; reads lock the active grant against concurrent revocation. Repeated identical writes are idempotent; a different digest for the same snapshot identity is a conflict.

Configure `GITWISE_DATABASE_URL` with a PostgreSQL `asyncpg` URL, then apply the schema with:

```bash
alembic upgrade head
```

The snapshot store remains separate from the webhook grant lifecycle. Its internal read methods enforce active grants, including reads through installation IDs returned by the verified GitHub user client. Historical snapshots are exposed only through authenticated endpoints that derive principal identity from a server-managed session and GitHub, never from request parameters. Do not create grants directly from unverified request data. Private-repository ingestion remains disabled.

## GitHub App user sign-in

`GET /auth/github/login` starts the GitHub App authorization-code flow with PKCE and a single-use `state` that is also bound to the browser by a short-lived `HttpOnly` cookie. `GET /auth/github/callback` consumes the state atomically, exchanges the code on the server, asks GitHub to verify the user, and creates a server-side session. The browser only receives an opaque `HttpOnly`, `Secure`, `SameSite=Lax` session cookie and a readable CSRF cookie; GitHub tokens never leave the server and are stored encrypted (`GITWISE_TOKEN_ENCRYPTION_KEYS`, newest key first, so keys can be rotated). Sign-in always returns to `GITWISE_PUBLIC_BASE_URL`; the destination is never taken from the request. Sessions have idle and absolute expiry. The session store refreshes expiring GitHub access tokens server-side, serializes refresh-token exchanges with a PostgreSQL row lock, and persists the newly rotated encrypted token pair and expiry timestamps together. Expired refresh credentials or provider failures fail closed; users may need to authorize again. Legacy sessions without a known refresh-token expiry are not refreshed. `POST /auth/session/rotate`, `/auth/logout` and `/auth/logout/all` require the `X-CSRF-Token` header. Identity comes only from the session, never from request parameters. Sign-in is disabled unless the database, token keys, GitHub App client credentials and public base URL are all configured.

## Authenticated Genome history

`GET /repositories/{provider_repository_id}/snapshots` lists stored snapshots for a repository and `GET /snapshots/{snapshot_key}` returns one. Both are read-only and require a signed-in session. On every request GitWise asks GitHub who the session's user is and which App installations they can reach, then reads only through active installation grants, so revoked grants, removed installation membership and revoked GitHub tokens take effect on the next request. Missing, unreadable and revoked repositories all return the same `404`; request-supplied user or installation IDs are ignored. Responses are `private, no-store`. The endpoints answer `503` if sign-in or storage is not configured. Private-repository ingestion remains disabled.

## Security

GitWise should request the narrowest GitHub App permissions possible. Do not send private repository contents to external model providers without clear user consent and an explicit data-handling policy.

## License

A license has not yet been selected. Until one is added, all rights are reserved by the repository owner.
