# GitWise

**The intelligence layer for software development.**

GitWise connects to GitHub repositories and builds a continuously evolving understanding of their code, architecture, dependencies, history, decisions, and risks.

Its long-term foundation is the **Software Genome**: a structured, evidence-backed record of what makes a software project what it is.

## Project status

GitWise is at the foundation stage. The first milestone is a read-only repository intelligence service that can safely inspect a repository, produce a structured project snapshot, and explain its findings with links back to source files.

## Product principles

- **Evidence before answers:** every finding should point to the files, symbols, commits, or metadata that support it.
- **Read-only by default:** repository access starts with the minimum permissions required.
- **Human control:** AI suggestions do not modify code or repository settings without explicit authorization.
- **Incremental understanding:** update project knowledge when repository contents change instead of rebuilding everything unnecessarily.
- **Portable project memory:** keep the Software Genome structured and exportable.

## Planned capabilities

1. **Repository Intelligence** — understand repository structure, languages, frameworks, and key project files.
2. **Architecture Vision** — map modules and dependencies with evidence.
3. **Project Memory** — preserve decisions, conventions, and important historical context.
4. **Guardian** — surface actionable security, reliability, and maintenance risks.
5. **GitWise Agents** — coordinate bounded development tasks under human supervision.

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

The API will be available at http://127.0.0.1:8000 and its OpenAPI docs at http://127.0.0.1:8000/docs.

## Configuration

Copy `.env.example` to `.env` and configure values as needed. Never commit tokens, private keys, or repository credentials.

## Security

GitWise should request the narrowest GitHub App permissions possible. Do not send private repository contents to external model providers without clear user consent and an explicit data-handling policy.

## License

A license has not yet been selected. Until one is added, all rights are reserved by the repository owner.
