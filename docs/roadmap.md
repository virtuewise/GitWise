# GitWise product roadmap

GitWise is being built in five deliberate phases. Each phase should deliver a useful product before the next expands its scope. The phases are strategic direction, not a promise that every capability ships at once.

## Phase 1 — Repository intelligence

**Build first · Target: usable MVP**

Make a repository understandable from a single connected GitHub project.

- [ ] Repository understanding: identify languages, frameworks, manifests, key files, conventions, and project structure.
- [ ] Architecture maps: explain major modules, relationships, dependencies, and important entry points with evidence.
- [ ] Project memory: preserve repository facts, technical decisions, conventions, and meaningful changes over time.
- [ ] Pull request intelligence: summarize proposed changes, identify affected areas, explain likely impact, and provide evidence-linked review context.
- [ ] Repository overview: present findings in a clear, navigable project workspace.
- [ ] Incremental refresh: update understanding as commits and pull requests change the repository.

**MVP acceptance goals**
- A developer can connect an authorized repository and receive a useful, evidence-backed project overview.
- Architecture and dependency findings link back to source files or repository metadata.
- Project memory is versioned and tied to the commit from which it was derived.
- Pull request analysis is understandable, relevant, and clearly distinguishes facts from inferences.
- Initial integration is read-only and requests the minimum GitHub permissions required.

## Phase 2 — Development intelligence

**Target: become part of everyday development**

Help developers make better engineering decisions during normal development work.

- [ ] Intelligent testing: recommend tests based on changed code, coverage gaps, and known project behavior.
- [ ] Impact prediction: estimate affected modules, APIs, services, and downstream dependencies before changes merge.
- [ ] Security insights: identify risky dependencies, insecure patterns, exposed secrets, and configuration weaknesses with actionable evidence.
- [ ] Development recommendations: suggest maintainability, reliability, performance, and documentation improvements.
- [ ] Developer workflow integration: surface useful intelligence in pull requests and the places developers already work.

**Exit goal:** GitWise provides timely, actionable development guidance that teams choose to use routinely.

## Phase 3 — Autonomous collaboration

**Target: trusted AI-assisted engineering**

Introduce coordinated AI agents that can perform bounded engineering work under human supervision.

- [ ] Issue investigation: inspect a reported problem, find relevant code, and produce a traceable diagnosis.
- [ ] Task planning: split work into scoped steps with explicit assumptions and expected outcomes.
- [ ] Coordinated agents: delegate research, implementation, testing, and review to specialized agents with clear responsibilities.
- [ ] Proposed changes: create branches and draft pull requests for human review.
- [ ] Verification: run approved checks in isolated, resource-limited environments and report results.
- [ ] Human approval: require explicit authorization before consequential writes, merges, deployments, or permission changes.
- [ ] Audit trail: record agent identity, tools, actions, evidence, test results, and approvals.

**Exit goal:** developers can safely delegate well-scoped tasks while retaining control and accountability.

## Phase 4 — Organization intelligence

**Target: enterprise infrastructure**

Understand how repositories, teams, services, and systems relate across an organization.

- [ ] Multi-repository Software Genome connecting shared libraries, services, APIs, and dependencies.
- [ ] Organization architecture maps and service ownership context.
- [ ] Cross-repository change-impact analysis.
- [ ] Team and system knowledge with explicit access controls.
- [ ] Portfolio-level security, reliability, maintenance, and modernization insights.
- [ ] Enterprise governance, auditability, policy controls, and deployment options.

**Exit goal:** engineering leaders and teams can reason about their software estate as a connected system.

## Phase 5 — Global software intelligence

**Target: an industry-wide platform**

Build an open, interoperable ecosystem for software knowledge and trusted collaboration.

- [ ] Interoperable Software Genome formats and documented APIs.
- [ ] Portable project histories, architecture knowledge, and provenance.
- [ ] Extensible integrations for developer platforms and engineering tools.
- [ ] A permission-aware ecosystem of specialized tools and trusted agents.
- [ ] Standards and governance that support portability, attribution, privacy, and user control.
- [ ] Cross-project knowledge sharing only where owners explicitly authorize it.

**Exit goal:** GitWise becomes a platform others can integrate with and build upon, without requiring projects to surrender ownership of their data.

## Delivery principles

1. Ship a narrow, genuinely useful Phase 1 before expanding into later phases.
2. Evidence before answers: important findings should cite source files, symbols, commits, or metadata.
3. Read-only by default; grant write capabilities only for a clear, user-approved workflow.
4. Keep humans accountable for consequential engineering decisions.
5. Treat repository content and external contributions as untrusted input.
6. Make project knowledge portable, versioned, permission-aware, and auditable.
7. Validate each phase with real developers and measurable adoption before scaling scope.

## Current engineering focus

The immediate work is Phase 1: complete safe repository ingestion, build the Software Genome foundation, generate architecture insights, preserve project memory, and deliver pull request intelligence. Later phases should build on this evidence-backed foundation rather than becoming separate disconnected products.

## Current implementation status

The repository now has a first deterministic Genome comparison engine for two supplied snapshots. It covers Python module/import graph differences and declared dependency differences, with tests for additions, removals, constraint changes, provenance, repository identity validation, and deterministic output. The API can inspect an explicit branch, tag, or commit and compare two refs through fresh, commit-pinned observations. Ingestion also enforces streamed upstream response-size limits and reports invalid Python source as a completeness limitation.

This is not yet historical project memory: snapshots are not persisted, comparisons re-fetch both refs, and there is no historical snapshot retrieval API. A persistence contract is now documented in [ADR 0001](architecture/decisions/0001-genome-persistence.md), with stable provider repository identity, immutable commit/schema keys, canonical content digests, and a separate access-grant model. No database backend or historical API is implemented yet; retention, authorization, and transactional idempotency must ship with that backend.
