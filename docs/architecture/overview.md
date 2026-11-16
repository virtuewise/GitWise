# GitWise architecture — initial direction

## Goal

Build a repository intelligence platform that creates a useful, continuously updated model of a software project while keeping source evidence, provenance, and user control central.

## Implemented repository intelligence slice

The current read-only API supports:

1. Validating and normalizing public GitHub repository URLs.
2. Retrieving repository metadata and a bounded Git tree.
3. Inspecting the default branch or an explicitly supplied branch, tag, or commit ref.
4. Pinning each observation to the commit SHA resolved by GitHub.
5. Extracting bounded manifest facts and a conservative Python import graph.
6. Returning a versioned Software Genome snapshot with evidence and analysis-completeness metadata.

Endpoints:

- `POST /repositories/resolve` validates a repository URL without network access.
- `POST /repositories/inspect` creates a snapshot. The optional `ref` field selects a branch, tag, or commit; if omitted, the default branch is used.
- `POST /repositories/compare` accepts `url`, `base_ref`, and `head_ref`, ingests both refs, and compares their commit-pinned snapshots.

Private repositories will require a GitHub App installation and explicit authorization. Do not ask users to paste personal access tokens into the product.

## Software Genome record

The initial schema lives in `apps/api/gitwise_api/genome.py`. A snapshot records repository identity, the provider's immutable repository ID when supplied, the resolved commit and tree SHAs, the requested ref, bounded tree inventory, architecture analysis, and evidence-linked facts.

Each generated fact should carry:

- stable fact identifier and schema version
- repository identity and analyzed commit SHA
- fact type and normalized value
- source path and source range where available
- extraction method and observation timestamp
- confidence and verification state

Facts must be traceable to repository evidence. Avoid presenting inferred architecture as certainty.

## Ingestion implementation boundaries

- GitHub REST calls use explicit connect/request timeouts and map missing repositories, rate limits, and upstream failures to controlled API errors.
- The recursive tree inventory is capped at 2,000 entries; individual blobs above 1 MB are excluded from the analyzed inventory.
- The ingestion pass reads selected dependency manifests and bounded Python source files by Git blob SHA. Python architecture analysis uses the standard-library AST parser; it does not import, execute, or follow symlinks in repository code.
- Manifest extraction is bounded by file count, per-file size, aggregate bytes, and a maximum number of emitted dependency facts.
- Initial dependency parsers cover Python `pyproject.toml` and `requirements.txt`, npm `package.json`, Rust `Cargo.toml`, and Go `go.mod`.
- Dependency facts describe declarations only; they do not establish installation, runtime usage, vulnerability status, or compatibility.
- The Python architecture graph links only imports that resolve unambiguously to modules in the inspected tree. Each edge records its source path, line number, commit SHA, and extraction method.
- Architecture analysis reports discovered, selected, and analyzed file counts plus explicit limitations. A partial graph must never be presented as complete.

## Genome evolution foundation

The comparison engine lives in `apps/api/gitwise_api/evolution.py`. It emits deterministic added, removed, and changed records for:

- Python architecture modules.
- Commit-pinned Python import relationships.
- Declared dependencies, keyed by manifest path, ecosystem, normalized package name, and declaration group.

Changes retain before/after values and the evidence available on each side. Import edges carry source-path and commit provenance. Commit SHA changes alone do not count as architectural changes.

The public comparison endpoint currently performs fresh ingestion for each request. Historical retrieval is not exposed. The persistence contract is documented in [ADR 0001](decisions/0001-genome-persistence.md): immutable keys use provider repository ID, resolved commit SHA, and schema version; canonical digests exclude observation-time-only metadata; and tenant access grants remain separate from snapshot identity. The initial PostgreSQL migration (`apps/api/migrations/versions/0001_genome_history.py`) creates provider repository, revocable principal grant, and immutable snapshot tables. `GenomeSnapshotStore` requires an active write/admin grant, performs transactional insert-if-absent, and rejects digest conflicts. Snapshot rows reject updates at the database layer. Grant provisioning and authenticated API integration are intentionally not implemented yet; the store must only receive principal identities established by a verified authentication flow. The comparison engine does not claim runtime impact, compatibility, vulnerability, or causality.

## GitHub webhook trust and replay protection

The initial webhook security primitives live in `apps/api/gitwise_api/webhooks.py` and `apps/api/gitwise_api/webhook_deliveries.py`. Signature verification uses GitHub's SHA-256 HMAC header, the configured secret, the original raw request bytes, and constant-time comparison. It must run before JSON parsing or trusting event metadata.

Verified delivery IDs are recorded transactionally in PostgreSQL. The unique delivery key detects concurrent duplicates and conflicting reuse of an ID with different event content. Migrations `0002_github_webhook_deliveries` and `0003_webhook_delivery_processing` create the ledger and its recoverable processing state: pending, processing with a five-minute lease, processed, and failed, with attempt counts and bounded error codes. A worker can reclaim an expired lease or retry a failed delivery; a completed delivery is acknowledged as a duplicate. The API currently exposes `POST /webhooks/github` for the GitHub `ping` handshake only. It enforces a 1 MB body limit, verifies the raw-body signature before parsing JSON, and marks the no-op ping handler complete only after handling. Other event types return `501` until dedicated handlers can verify App installation ownership and apply grant changes transactionally. Lifecycle handlers must commit domain effects before marking delivery completion; delivery leases alone do not make multi-step external API work atomic. Operational retention policy remains outstanding. This endpoint is not yet a GitHub App installation lifecycle integration.

## GitHub App installation identity

`apps/api/gitwise_api/github_app.py` implements the App-authenticated identity boundary. It creates short-lived RS256 App JWTs, queries GitHub's App installation endpoint, and verifies that the returned installation ID and App ID match the configured credentials before minting an installation access token. Token creation can be scoped to an explicit repository-name allowlist; tokens are short-lived credentials and must remain in memory, never in logs or persistent storage.

The client now also supports a one-repository token exchange that checks the installation account, the returned repository's immutable numeric ID and full name, and an explicit `contents: read` permission before returning the token. The caller must compare that verified repository ID with the ID from the signed event; repository names alone are not authorization identities. Tokens remain short-lived and in memory only. The transactional grant store in `apps/api/gitwise_api/installation_grants.py` now provisions read-only installation grants from App-verified installation identity and repository records, supports targeted repository revocation, and revokes all grants for a deleted installation. PostgreSQL integration tests cover idempotent provisioning and overlapping installation principals. The store is not yet connected to webhook event dispatch: installation lifecycle events remain unsupported until signed event data is reconciled with App-authenticated repository identity and grant effects are committed in the same recoverable event-processing transaction.

## Proposed components

- **Web client:** repository onboarding, project overview, evidence explorer.
- **API:** authentication, repository orchestration, snapshot and comparison endpoints.
- **GitHub integration:** App installation, least-privilege permissions, webhook verification, rate-limit-aware API access.
- **Ingestion workers:** bounded tree traversal and manifest/document extraction.
- **Software Genome:** versioned structured facts, provenance, confidence, and source references.
- **Storage:** PostgreSQL for canonical records; vector search only when a concrete retrieval need is established.

## Security boundaries

- Start read-only and request minimum GitHub App permissions.
- Verify webhook signatures before processing events.
- Treat repository files, issues, and comments as untrusted input.
- Never execute repository code during ingestion.
- Apply file-count, file-size, response-size, and time limits to ingestion.
- Keep secrets out of logs and API responses.
- Make retention and model-provider data sharing explicit.

## Near-term engineering sequence

1. **Completed:** enforce upstream response-size limits, including streamed bodies without trustworthy `Content-Length` headers.
2. **Completed:** treat invalid or unparseable Python source as an explicit architecture-analysis limitation rather than silently reporting a complete graph.
3. **Initial persistence foundation:** capture GitHub's immutable repository ID; define backend-neutral snapshot keys and canonical content digests; add PostgreSQL migrations and a transactional grant-checked store. **Next:** implement GitHub App JWT authentication and verified installation ownership, grant provisioning/revocation, and tenant-isolation tests before exposing history.
4. Add incremental refresh and efficient comparison over stored observations.
5. Expand conservative architecture analysis beyond Python, with explicit per-language completeness.
6. Build evidence navigation and project overview workflows on top of the stable Genome API.
