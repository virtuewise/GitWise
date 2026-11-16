# ADR 0001: Immutable Software Genome snapshot identity and persistence

- **Status:** Accepted as the persistence contract; backend implementation is deferred.
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

A durable write must be insert-if-absent. If the key already exists, compare the canonical content digest. An identical digest is an idempotent success; a different digest is a conflict requiring investigation, never a silent overwrite. The original observation timestamp and ingestion metadata must remain auditable. Digest canonicalization may normalize only collections whose order is explicitly non-semantic (such as snapshot tree entries, architecture nodes/edges, facts, evidence, and limitation codes). It must preserve ordering inside fact values and other domain payloads, because those arrays may encode meaningful sequence.

### 3. Keep snapshots immutable and schema-versioned

- Never update a stored snapshot in place to represent a newer commit or schema.
- A new commit or schema version creates a new snapshot record.
- Preserve source commit, tree SHA, extraction method, source path/URL, confidence, verification state, and analysis limitations.
- Treat incomplete analysis as a valid but explicitly partial observation.
- Store a canonical content digest to detect corruption and unexpected non-determinism.
- Keep the original requested ref as provenance/display metadata, not identity.

### 4. PostgreSQL is the preferred canonical store, not a current runtime dependency

PostgreSQL is the preferred first durable backend because it supports transactional uniqueness, relational access-control joins, indexing, and JSONB for versioned Genome payloads. The initial implementation should use migrations and explicit repository/access-grant/snapshot tables. Avoid adding vector storage until measured retrieval needs justify it.

Before enabling private repositories, implement and test the authorization model, installation ownership, revocation behavior, and tenant-isolation constraints. Do not treat GitHub Developer Program membership as an installation grant or elevated API authorization.

### 5. Retention and deletion are first-class lifecycle operations

Define retention per repository owner before production storage. Deletion must remove or render inaccessible the snapshot payload and derived indexes according to the published policy, while retaining only the minimum legally/operationally required audit record. Revoking an installation must immediately block future reads and jobs; it must not leave background workers with reusable access.

## Implementation sequence

1. **Completed:** expose GitHub's immutable numeric repository ID in new Software Genome snapshots.
2. **Completed:** add backend-neutral canonical snapshot keys and content digests, with timestamp/order invariance tests; preserve semantically ordered arrays inside fact values.
3. Add PostgreSQL migrations and a transactional store with insert-if-absent/conflict semantics.
4. Add owner/installation access grants and enforce authorization before any snapshot operation.
5. Add retention, deletion, audit, and installation-revocation workflows.
6. Add historical retrieval and comparisons over stored snapshots; then add incremental refresh.

## Explicit non-goals

This decision does not add database dependencies, credentials, private-repository access, background workers, or a public historical API. Those capabilities should ship only with their operational and security controls.
