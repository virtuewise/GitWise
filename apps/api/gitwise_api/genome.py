from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class GenomeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_path: str | None = None
    source_url: str | None = None
    commit_sha: str = Field(min_length=7)
    extraction_method: str = "github_repository_metadata"


class GenomeFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: str
    schema_version: Literal["1.0"] = "1.0"
    fact_type: str
    value: Any
    evidence: list[GenomeEvidence] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    verification_status: Literal["observed", "inferred"]
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def observed(
        cls,
        *,
        repository: str,
        commit_sha: str,
        fact_type: str,
        value: Any,
        source_path: str | None = None,
        source_url: str | None = None,
        extraction_method: str = "github_repository_metadata",
    ) -> GenomeFact:
        identity = f"{repository}:{commit_sha}:{fact_type}:{source_path or ''}:{value}"
        return cls(
            fact_id=sha256(identity.encode("utf-8")).hexdigest(),
            fact_type=fact_type,
            value=value,
            evidence=[GenomeEvidence(
                source_path=source_path,
                source_url=source_url,
                commit_sha=commit_sha,
                extraction_method=extraction_method,
            )],
            confidence=1.0,
            verification_status="observed",
        )


class RepositoryTreeEntry(BaseModel):
    path: str
    kind: Literal["blob", "tree", "commit"]
    sha: str
    size: int | None = None
    mode: str | None = None


class ArchitectureNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    module_name: str
    source_path: str
    language: Literal["python"] = "python"


class ArchitectureEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_module: str
    target_module: str
    relationship: Literal["imports"] = "imports"
    source_path: str
    line_number: int = Field(ge=1)
    commit_sha: str = Field(min_length=7)
    extraction_method: str = "python_ast_import_analyzer"


class ArchitectureAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["complete", "partial"] = "complete"
    files_discovered: int = Field(default=0, ge=0)
    files_selected: int = Field(default=0, ge=0)
    files_analyzed: int = Field(default=0, ge=0)
    limitations: list[str] = Field(default_factory=list)


class SoftwareGenomeSnapshot(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    repository: str
    repository_url: str
    description: str | None = None
    default_branch: str
    commit_sha: str
    tree_sha: str
    primary_language: str | None = None
    is_private: bool
    truncated: bool = False
    files_analyzed: int = 0
    tree: list[RepositoryTreeEntry] = Field(default_factory=list)
    architecture_analysis: ArchitectureAnalysis = Field(default_factory=ArchitectureAnalysis)
    architecture_nodes: list[ArchitectureNode] = Field(default_factory=list)
    architecture_edges: list[ArchitectureEdge] = Field(default_factory=list)
    facts: list[GenomeFact] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
