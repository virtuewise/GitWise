from __future__ import annotations

import base64
import json
import re
import tomllib
from urllib.parse import quote

import httpx
from fastapi import HTTPException, status

from gitwise_api.architecture import extract_python_imports, module_name_for_path
from gitwise_api.genome import (
    ArchitectureAnalysis,
    ArchitectureEdge,
    ArchitectureNode,
    GenomeFact,
    RepositoryTreeEntry,
    SoftwareGenomeSnapshot,
)
from gitwise_api.repositories import RepositoryReference

GITHUB_API = "https://api.github.com"
MAX_TREE_ENTRIES = 2_000
MAX_FILE_SIZE_BYTES = 1_000_000
MAX_MANIFESTS_TO_READ = 40
MAX_TOTAL_MANIFEST_BYTES = 5_000_000
MAX_DEPENDENCY_FACTS = 1_000
MAX_PYTHON_FILES_TO_READ = 100
MAX_PYTHON_FILE_SIZE_BYTES = 256_000
MAX_TOTAL_PYTHON_BYTES = 2_000_000
MANIFESTS = {
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "Pipfile": "python",
    "package.json": "javascript",
    "pnpm-lock.yaml": "javascript",
    "yarn.lock": "javascript",
    "package-lock.json": "javascript",
    "go.mod": "go",
    "Cargo.toml": "rust",
    "pom.xml": "java",
    "build.gradle": "java",
    "Gemfile": "ruby",
    "composer.json": "php",
    "*.sln": "dotnet",
    "*.csproj": "dotnet",
}


class GitHubRepositoryClient:
    """Read-only GitHub API client. Repository code is parsed statically and never executed."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=GITHUB_API,
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "GitWise-Repository-Intelligence/0.1",
            },
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, path: str) -> dict:
        try:
            response = await self._client.get(path)
        except httpx.TimeoutException as exc:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="GitHub did not respond before the ingestion timeout.",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Unable to communicate with GitHub.",
            ) from exc

        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="GitHub repository or ref not found.")
        if response.status_code in {403, 429}:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GitHub API rate limit or access policy prevented this inspection.",
                headers={"Retry-After": response.headers.get("Retry-After", "60")},
            )
        if response.is_error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="GitHub returned an error while inspecting this repository.",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="GitHub returned an invalid response.",
            ) from exc
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="GitHub returned an unexpected response shape.",
            )
        return payload

    async def inspect(self, repository: RepositoryReference) -> SoftwareGenomeSnapshot:
        encoded_owner = quote(repository.owner, safe="")
        encoded_name = quote(repository.name, safe="")
        base = f"/repos/{encoded_owner}/{encoded_name}"
        metadata = await self._get(base)
        default_branch = metadata.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch:
            raise HTTPException(status_code=502, detail="GitHub repository metadata is incomplete.")

        branch = await self._get(f"{base}/branches/{quote(default_branch, safe='')}")
        commit = branch.get("commit")
        if not isinstance(commit, dict):
            raise HTTPException(status_code=502, detail="GitHub returned an invalid branch commit.")
        commit_details = commit.get("commit")
        tree = commit_details.get("tree") if isinstance(commit_details, dict) else None
        commit_sha = commit.get("sha")
        tree_sha = tree.get("sha") if isinstance(tree, dict) else None
        if not isinstance(commit_sha, str) or not isinstance(tree_sha, str):
            raise HTTPException(
                status_code=502,
                detail="GitHub did not provide a valid branch commit.",
            )

        tree_payload = await self._get(
            f"{base}/git/trees/{quote(tree_sha, safe='')}?recursive=1"
        )
        raw_tree = tree_payload.get("tree")
        if not isinstance(raw_tree, list):
            raise HTTPException(
                status_code=502,
                detail="GitHub did not provide a valid repository tree.",
            )

        entries: list[RepositoryTreeEntry] = []
        for item in raw_tree[:MAX_TREE_ENTRIES]:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            kind = item.get("type")
            sha = item.get("sha")
            if not isinstance(path, str) or not path or path.startswith("/"):
                continue
            if "\\" in path or ".." in path.split("/") or not isinstance(sha, str):
                continue
            if kind not in {"blob", "tree", "commit"}:
                continue
            size = item.get("size")
            if size is not None and (
                not isinstance(size, int) or isinstance(size, bool) or size < 0
            ):
                continue
            if kind == "blob" and size is not None and size > MAX_FILE_SIZE_BYTES:
                continue
            mode = item.get("mode")
            entries.append(
                RepositoryTreeEntry(path=path, kind=kind, sha=sha, size=size, mode=mode)
            )

        facts = [
            GenomeFact.observed(
                repository=repository.full_name,
                commit_sha=commit_sha,
                fact_type="repository.default_branch",
                value=default_branch,
                source_url=repository.canonical_url,
            ),
            GenomeFact.observed(
                repository=repository.full_name,
                commit_sha=commit_sha,
                fact_type="repository.visibility",
                value=metadata.get("visibility", "unknown"),
                source_url=repository.canonical_url,
            ),
        ]
        description = metadata.get("description")
        if description:
            facts.append(GenomeFact.observed(
                repository=repository.full_name,
                commit_sha=commit_sha,
                fact_type="repository.description",
                value=description,
                source_url=repository.canonical_url,
            ))
        language = metadata.get("language")
        if language:
            facts.append(GenomeFact.observed(
                repository=repository.full_name,
                commit_sha=commit_sha,
                fact_type="repository.primary_language",
                value=language,
                source_url=repository.canonical_url,
            ))

        blob_entries = {
            entry.path: entry
            for entry in entries
            if entry.kind == "blob" and entry.size is not None
        }
        manifest_paths = sorted(
            path
            for path in blob_entries
            if path.rsplit("/", 1)[-1] in {
                "pyproject.toml",
                "requirements.txt",
                "package.json",
                "Cargo.toml",
                "go.mod",
            }
        )[:MAX_MANIFESTS_TO_READ]
        total_manifest_bytes = 0
        dependency_facts_emitted = 0
        for path in manifest_paths:
            entry = blob_entries[path]
            if entry.size is None or entry.size > MAX_FILE_SIZE_BYTES:
                continue
            if total_manifest_bytes + entry.size > MAX_TOTAL_MANIFEST_BYTES:
                break
            blob_payload = await self._get(f"{base}/git/blobs/{quote(entry.sha, safe='')}")
            encoded = blob_payload.get("content")
            encoding = blob_payload.get("encoding")
            if not isinstance(encoded, str) or encoding != "base64":
                continue
            try:
                compact_content = re.sub(r"\s+", "", encoded)
                raw_content = base64.b64decode(compact_content, validate=True)
                if len(raw_content) > MAX_FILE_SIZE_BYTES:
                    continue
                text_content = raw_content.decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                continue
            total_manifest_bytes += len(raw_content)
            filename = path.rsplit("/", 1)[-1]
            dependencies: list[dict[str, str]] = []
            try:
                if filename == "pyproject.toml":
                    document = tomllib.loads(text_content)
                    project = document.get("project", {})
                    declared = list(project.get("dependencies", []))
                    optional = project.get("optional-dependencies", {})
                    if isinstance(optional, dict):
                        for group, values in optional.items():
                            if isinstance(values, list):
                                declared.extend(values)
                    for requirement in declared:
                        if isinstance(requirement, str):
                            match = re.match(r"\s*([A-Za-z0-9_.-]+)\s*(.*)", requirement)
                            if match:
                                dependencies.append({
                                    "name": match.group(1),
                                    "constraint": match.group(2).strip(),
                                    "ecosystem": "pypi",
                                })
                elif filename == "requirements.txt":
                    for line in text_content.splitlines():
                        candidate = line.strip()
                        if not candidate or candidate.startswith(("#", "-")):
                            continue
                        match = re.match(r"([A-Za-z0-9_.-]+)\s*(.*)", candidate)
                        if match:
                            dependencies.append({
                                "name": match.group(1),
                                "constraint": match.group(2).strip(),
                                "ecosystem": "pypi",
                            })
                elif filename == "package.json":
                    document = json.loads(text_content)
                    sections = (
                        "dependencies",
                        "devDependencies",
                        "peerDependencies",
                        "optionalDependencies",
                    )
                    for section in sections:
                        declared = document.get(section, {})
                        if isinstance(declared, dict):
                            for name, constraint in declared.items():
                                if isinstance(name, str) and isinstance(constraint, str):
                                    dependencies.append({
                                        "name": name,
                                        "constraint": constraint,
                                        "ecosystem": "npm",
                                        "group": section,
                                    })
                elif filename == "Cargo.toml":
                    document = tomllib.loads(text_content)
                    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
                        declared = document.get(section, {})
                        if isinstance(declared, dict):
                            for name, constraint in declared.items():
                                if isinstance(name, str):
                                    version = (
                                        constraint
                                        if isinstance(constraint, str)
                                        else constraint.get("version", "")
                                    )
                                    dependencies.append({
                                        "name": name,
                                        "constraint": version if isinstance(version, str) else "",
                                        "ecosystem": "crates.io",
                                        "group": section,
                                    })
                elif filename == "go.mod":
                    in_require_block = False
                    for line in text_content.splitlines():
                        candidate = line.strip()
                        if candidate.startswith("require ("):
                            in_require_block = True
                            continue
                        if in_require_block and candidate == ")":
                            in_require_block = False
                            continue
                        if candidate.startswith("require ") or in_require_block:
                            candidate = candidate.removeprefix("require ").strip()
                            parts = candidate.split()
                            if len(parts) >= 2 and not parts[0].startswith("//"):
                                dependencies.append({
                                    "name": parts[0],
                                    "constraint": parts[1],
                                    "ecosystem": "go",
                                })
            except (tomllib.TOMLDecodeError, json.JSONDecodeError, TypeError, AttributeError):
                continue

            for dependency in dependencies:
                if dependency_facts_emitted >= MAX_DEPENDENCY_FACTS:
                    break
                facts.append(GenomeFact.observed(
                    repository=repository.full_name,
                    commit_sha=commit_sha,
                    fact_type="dependency.declared",
                    value=dependency,
                    source_path=path,
                    source_url=(
                        f"{repository.canonical_url}/blob/{commit_sha}/{quote(path, safe='/')}"
                    ),
                    extraction_method="github_git_blob_manifest_parser",
                ))
                dependency_facts_emitted += 1
            if dependency_facts_emitted >= MAX_DEPENDENCY_FACTS:
                break

        python_tree_entries = [
            item for item in raw_tree
            if isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and item["path"].endswith(".py")
            and module_name_for_path(item["path"]) is not None
        ]
        python_discovered = len(python_tree_entries)
        python_candidates = sorted(
            (
                entry for entry in blob_entries.values()
                if entry.path.endswith(".py")
                and module_name_for_path(entry.path) is not None
            ),
            key=lambda entry: entry.path,
        )
        eligible_python_sources = [
            entry for entry in python_candidates
            if entry.size is not None and entry.size <= MAX_PYTHON_FILE_SIZE_BYTES
        ]
        python_sources = eligible_python_sources[:MAX_PYTHON_FILES_TO_READ]
        architecture_limitations: set[str] = set()
        if bool(tree_payload.get("truncated")) or len(raw_tree) > MAX_TREE_ENTRIES:
            architecture_limitations.add("repository_tree_truncated")
        if len(eligible_python_sources) > MAX_PYTHON_FILES_TO_READ:
            architecture_limitations.add("max_python_files")
        if len(python_candidates) < python_discovered or any(
            not isinstance(item.get("size"), int)
            or isinstance(item.get("size"), bool)
            or item.get("size", 0) > MAX_PYTHON_FILE_SIZE_BYTES
            for item in python_tree_entries
        ):
            architecture_limitations.add("python_file_size_or_metadata")

        module_by_path = {
            entry.path: module_name_for_path(entry.path)
            for entry in python_sources
        }
        module_paths: dict[str, list[str]] = {}
        for source_path, module_name in module_by_path.items():
            if module_name is not None:
                module_paths.setdefault(module_name, []).append(source_path)

        architecture_nodes = [
            ArchitectureNode(module_name=module_name, source_path=source_path)
            for source_path, module_name in module_by_path.items()
            if module_name is not None
        ]
        architecture_edges: list[ArchitectureEdge] = []
        total_python_bytes = 0
        python_files_analyzed = 0
        for entry in python_sources:
            if entry.size is None or total_python_bytes + entry.size > MAX_TOTAL_PYTHON_BYTES:
                architecture_limitations.add("max_total_python_bytes")
                break
            blob_payload = await self._get(
                f"{base}/git/blobs/{quote(entry.sha, safe='')}"
            )
            encoded = blob_payload.get("content")
            if not isinstance(encoded, str) or blob_payload.get("encoding") != "base64":
                architecture_limitations.add("unreadable_python_blob")
                continue
            try:
                raw_content = base64.b64decode(
                    re.sub(r"\\s+", "", encoded),
                    validate=True,
                )
                if len(raw_content) > MAX_PYTHON_FILE_SIZE_BYTES:
                    architecture_limitations.add("python_file_size_or_metadata")
                    continue
                declared_size = blob_payload.get("size")
                if not isinstance(declared_size, int) or declared_size != len(raw_content):
                    architecture_limitations.add("python_blob_size_mismatch")
                    continue
                source = raw_content.decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                architecture_limitations.add("unreadable_python_blob")
                continue

            if total_python_bytes + len(raw_content) > MAX_TOTAL_PYTHON_BYTES:
                architecture_limitations.add("max_total_python_bytes")
                break
            total_python_bytes += len(raw_content)
            python_files_analyzed += 1
            source_module = module_by_path[entry.path]
            if source_module is None:
                continue
            for imported in extract_python_imports(source, source_module, entry.path):
                candidates = [
                    imported.target_module,
                    *(
                        ".".join(imported.target_module.split(".")[:index])
                        for index in range(len(imported.target_module.split(".")) - 1, 0, -1)
                    ),
                ]
                target_module = next(
                    (
                        candidate for candidate in candidates
                        if len(module_paths.get(candidate, [])) == 1
                    ),
                    None,
                )
                if target_module is None or target_module == source_module:
                    continue
                architecture_edges.append(
                    ArchitectureEdge(
                        source_module=source_module,
                        target_module=target_module,
                        source_path=entry.path,
                        line_number=imported.line_number,
                        commit_sha=commit_sha,
                    )
                )

        unique_edges = {
            (
                edge.source_module,
                edge.target_module,
                edge.source_path,
                edge.line_number,
            ): edge
            for edge in architecture_edges
        }
        architecture_edges = list(unique_edges.values())
        architecture_analysis = ArchitectureAnalysis(
            status="partial" if architecture_limitations else "complete",
            files_discovered=python_discovered,
            files_selected=len(python_sources),
            files_analyzed=python_files_analyzed,
            limitations=sorted(architecture_limitations),
        )

        blob_paths = {entry.path for entry in entries if entry.kind == "blob"}
        for manifest, ecosystem in MANIFESTS.items():
            matching = sorted(
                path for path in blob_paths
                if path.rsplit("/", 1)[-1] == manifest
                or (manifest.startswith("*") and path.endswith(manifest[1:]))
            )
            for path in matching:
                facts.append(GenomeFact.observed(
                    repository=repository.full_name,
                    commit_sha=commit_sha,
                    fact_type="project.manifest",
                    value={"ecosystem": ecosystem, "path": path},
                    source_path=path,
                    source_url=(
                        f"{repository.canonical_url}/blob/{commit_sha}/{quote(path, safe='/')}"
                    ),
                ))

        readme_paths = sorted(
            path for path in blob_paths
            if path.rsplit("/", 1)[-1].lower().startswith("readme")
        )
        for path in readme_paths[:5]:
            facts.append(GenomeFact.observed(
                repository=repository.full_name,
                commit_sha=commit_sha,
                fact_type="project.documentation_entrypoint",
                value=path,
                source_path=path,
                source_url=f"{repository.canonical_url}/blob/{commit_sha}/{quote(path, safe='/')}",
            ))

        return SoftwareGenomeSnapshot(
            repository=repository.full_name,
            repository_url=repository.canonical_url,
            description=description,
            default_branch=default_branch,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            primary_language=language,
            is_private=bool(metadata.get("private", False)),
            truncated=bool(tree_payload.get("truncated")) or len(raw_tree) > MAX_TREE_ENTRIES,
            files_analyzed=sum(entry.kind == "blob" for entry in entries),
            tree=entries,
            architecture_analysis=architecture_analysis,
            architecture_nodes=architecture_nodes,
            architecture_edges=architecture_edges,
            facts=facts,
        )
