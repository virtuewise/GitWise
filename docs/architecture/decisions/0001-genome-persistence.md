# ADR 0001: Immutable Software Genome snapshot identity and persistence

- **Status:** Accepted; initial PostgreSQL schema and transactional store are implemented. Runtime API integration remains deferred.
- **Date:** 2026-11-16
- **Decision owners:** GitWise maintainers

## Context

GitWise currently creates commit-pinned Software Genome observations and compares two freshly ingested snapshots. It does not retain historical snapshots. Durable history must support retries, repository renames, multiple owners/installations, schema evolution, and deletion without confusing a mutable Git ref with immutable source identity.

Choosing a database before defining these guarantees would risk encoding an incomplete ownership model into storage.

## Decisions

### 1. Separate repository identity, access grants, and snapshots

The durable model must distinguish:

- **Provider repository:** the stable upstream identity (GitHub numeric repository ID), with mutable owner/name and canonical URL as display metadata.
- **Access grant:** the owner or GitHub App installation authorized to access that repository. Grants are revocable and are not inferred from a repository slug.
- **Snapshot:** an immutable observation of one provider repository at one resolved commit and one Genome schema version.

A future multi-tenant schema must enforce authorization at the repository/access-grant boundary on every read, comparison, export, and background job. A globally unique snapshot key is not an authorization mechanism. Do not expose private-repository existence or metadata across tenants.

### 2. Use a stable, idempotent snapshot key

The canonical key is:

`provider : provider_repository_id : resolved_commit_sha : schema_version`

For the initial GitHub provider, the implementation helper emits `github:{repository_id}:{commit_sha}:schema-{schema_version}`. Repository owner/name and requested branch/tag are not key components: names and refs can change while the commit remains the same.

A durable write must be insert-if-absent. If the key already exists, compare the canonical content digest. An identical digest is an idempotent success; a different digest is a conflict requiring investigation, never a silent overwrite. The original observation timestamp and ingestion metadata must remain auditable.

The digest represents **commit-derived Genome content**, not the mutable GitHub repository record. Digest v1 includes the resolved provider repository ID, commit/tree SHAs, tree inventory, truncation and analysis coverage, architecture analysis/nodes/edges, and commit-derived facts. It excludes owner/name, repository URL, description, default branch, requested ref, primary language and visibility; it also excludes `repository.*` metadata facts, generated fact IDs, observation timestamps and evidence source URLs. Evidence commit SHA, source path and extraction method remain digest-significant. This ensures a repository rename, metadata edit, alternate ref name or URL change cannot make the same commit appear to have different source-derived content.

Only explicitly unordered collections (tree entries, architecture nodes/edges, facts, evidence and limitation codes) are normalized. Ordering inside fact values and other domain payloads remains significant. The digest projection is allowlisted rather than hashing the full presentation model: adding new mutable metadata must not silently change the persistence contract. Any future change to digest semantics must be versioned deliberately alongside the Genome schema before production data is written.

### 3. Keep snapshots immutable and schema-versioned

- Genome fact values must be strict JSON values: objects with string keys, arrays, strings, finite numbers, booleans, or null. Reject Python-only values and non-finite numbers at the model/identity boundary rather than coercing them during persistence.

- Never update a stored snapshot in place to represent a newer commit or schema.
- A new commit or schema version creates a new snapshot record.
- Preserve source commit, tree SHA, extraction method, source path/URL, confidence, verification state, and analysis limitations.
- Treat incomplete analysis as a valid but explicitly partial observation.
- Store a canonical content digest to detect corruption and unexpected non-determinism.
- Keep the original requested ref as provenance/display metadata, not identity.

### 4. PostgreSQL is the canonical persistence backend, but not yet an API runtime dependency

PostgreSQL is the preferred first durable backend because it supports transactional uniqueness, relational access-control joins, indexing, and JSONB for versioned Genome payloads. The initial implementation should use migrations and explicit repository/access-grant/snapshot tables. Avoid adding vector storage until measured retrieval needs justify it.

Before enabling private repositories, implement and test the authorization model, installation ownership, revocation behavior, and tenant-isolation constraints. Do not treat GitHub Developer Program membership as an installation grant or elevated API authorization.

### 5. Retention and deletion are first-class lifecycle operations

Define retention per repository owner before production storage. Deletion must remove or render inaccessible the snapshot payload and derived indexes according to the published policy, while retaining only the minimum legally/operationally required audit record. Revoking an installation must immediately block future reads and jobs; it must not leave background workers with reusable access.

## GitHub user-token lifecycle

GitHub App user access tokens and refresh tokens are encrypted at rest. The session schema records both expiry timestamps. Before returning an access token, the session store checks its expiry using the PostgreSQL clock and refreshes within a two-minute safety window. Refresh exchanges are serialized by locking the session row, preventing concurrent workers from consuming the same rotating refresh token. The newly returned token pair and expiry values are encrypted and persisted in the same database transaction. A missing or expired refresh token, unknown access-token expiry, or provider rejection fails closed.

The provider exchange and local database commit cannot form a distributed transaction. If GitHub rotates a refresh token but the database transaction subsequently fails, the old token may no longer work; the safe recovery is a fresh user authorization rather than reuse of stale credentials. Legacy sessions with a refresh token but no recorded refresh expiry cannot be refreshed.

Logout and token revocation

Local logout is committed before any network call to GitHub. The transaction serializes against refresh using the session row lock, marks the session revoked, clears the encrypted refresh token and both expiry timestamps, and replaces the non-null access-token ciphertext with an encrypted non-credential sentinel. A single-session logout then best-effort calls GitHub's `DELETE /applications/{client_id}/token` endpoint to revoke that user access token. Logout-all locks and sanitizes every active session for the verified GitHub user, then best-effort calls `DELETE /applications/{client_id}/grant` to revoke the user's complete GitHub App authorization and associated tokens. Provider failures never roll back local logout or restore local token material. These operations occur after the local transaction commits so database locks are not held across network calls. PostgreSQL tests assert credential cleanup, scope of revocation, and local fail-closed behavior when GitHub returns an error.


## Implementation sequence

1. **Completed:** expose GitHub's immutable numeric repository ID in new Software Genome snapshots.
2. **Completed:** add backend-neutral canonical snapshot keys and content digests, with timestamp/order invariance tests; preserve semantically ordered arrays inside fact values.
3. **Implemented, integration verification in CI:** PostgreSQL migrations define provider repositories, revocable principal grants, and immutable snapshots. The transactional insert-if-absent store requires an active write grant and rejects digest conflicts. CI provisions PostgreSQL 16, applies the migrations, and exercises authorization denial, idempotent writes, digest conflicts, and database-enforced immutability. The store is not yet wired to an authenticated API or deployed database.
4. **Implemented:** verify GitHub App installation ownership and one-repository immutable identity; process signed installation create/delete/suspend/unsuspend and repository add/remove events; provision idempotent read-only grants; atomically reconcile repository additions/removals; revoke all grants on deletion/suspension; and test overlapping installation principals in PostgreSQL. **Implemented read-authorization foundation:** verify a GitHub user and enumerate accessible installation IDs through a session-supplied GitHub App user access token; authorize snapshot reads only through active grants belonging to those installations; test unrelated-principal denial and revocation in PostgreSQL. **Next:** add the server-managed GitHub App user authorization/session lifecycle and end-to-end authorization before exposing snapshot operations.
5. Add retention, deletion, audit, and installation-revocation workflows.
6. Add historical retrieval and comparisons over stored snapshots; then add incremental refresh.

## Explicit non-goals

This decision does not enable private-repository ingestion or a public historical API. Those capabilities should ship only with authenticated user-to-installation ownership, cross-tenant authorization, retention, and operational controls.
