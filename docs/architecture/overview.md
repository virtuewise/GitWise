# GitWise architecture — initial direction

## Goal

Build a repository intelligence platform that creates a useful, continuously updated model of a software project while keeping source evidence and user control central.

## First milestone: repository snapshot

The first vertical slice is implemented as a read-only public-repository inspection path. It should:

1. Accept a public GitHub repository URL.
2. Validate and normalize the URL (allow only GitHub repository URLs; reject credentials, arbitrary hosts, and non-repository paths).
3. Retrieve repository metadata and a bounded file tree through GitHub's API.
4. Identify project manifests and documentation (for example README, pyproject.toml, package.json, go.mod, Cargo.toml).
5. Pin the snapshot to the default-branch commit SHA.
6. Produce a versioned Software Genome with evidence-linked observed facts.
7. Expose the snapshot through `POST /repositories/inspect`.

Private repositories will require a GitHub App installation and explicit authorization. Do not ask users to paste personal access tokens into the product.

## Proposed components

- **Web client:** repository onboarding, project overview, evidence explorer.
- **API:** authentication, repository orchestration, snapshot endpoints.
- **GitHub integration:** App installation, least-privilege permissions, webhook verification, rate-limit-aware API access.
- **Ingestion workers:** bounded tree traversal and manifest/document extraction.
- **Software Genome:** versioned structured facts, provenance, confidence, and source references.
- **Storage:** PostgreSQL for canonical records; vector search only when a concrete retrieval need is established.

## Software Genome record

The initial schema lives in `apps/api/gitwise_api/genome.py`. Each generated fact should carry:

- stable fact identifier and schema version
- repository identity and analyzed commit SHA
- fact type and normalized value
- source path and source range where available
- extraction method and timestamp
- confidence / verification state

Facts must be traceable to repository evidence. Avoid presenting inferred architecture as certainty.

## Ingestion implementation boundaries

- GitHub REST calls use explicit connect/request timeouts and map missing repositories, rate limits, and upstream failures to controlled API errors.
- The recursive tree response is capped at 2,000 entries; individual blobs above 1 MB are excluded from the analyzed tree inventory.
- The ingestion pass requests the recursive commit-pinned Git tree and reads selected dependency manifests and bounded Python source files by Git blob SHA. Python architecture analysis uses the standard-library AST parser; it does not import, execute, or follow symlinks in repository code.
- Manifest extraction is bounded by file count, per-file size, aggregate bytes, and a maximum number of emitted dependency facts.
- Initial parsers cover Python `pyproject.toml` and `requirements.txt`, npm `package.json`, Rust `Cargo.toml`, and Go `go.mod`. Unsupported formats and malformed manifests are skipped rather than guessed.
- Dependency facts describe declarations only; they do not establish installation, runtime usage, vulnerability status, or compatibility.
- Dependency facts describe declarations only; they do not establish installation, runtime usage, vulnerability status, or compatibility.\n- The initial Python architecture graph links only imports that resolve unambiguously to modules in the inspected tree. Each edge records its source path, line number, commit SHA, and extraction method.\n- Architecture analysis reports discovered, selected, and analyzed file counts plus explicit limitations. A partial graph must never be presented as complete.

## Security boundaries

- Start read-only and request minimum GitHub App permissions.
- Verify webhook signatures before processing events.
- Treat repository files, issues, and comments as untrusted input.
- Never execute repository code during ingestion.
- Apply file-count, file-size, and time limits to ingestion.
- Keep secrets out of logs and API responses.
- Make retention and model-provider data sharing explicit.

## Near-term sequence

1. API foundation and tests.
2. Safe GitHub repository URL parsing and metadata ingestion.
3. File-tree and manifest analysis.
4. Versioned Software Genome schema.
5. Repository overview endpoint and first web screen.\n6. Commit-pinned Python import graph with explicit completeness metadata.
