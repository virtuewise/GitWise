from __future__ import annotations

from datetime import datetime, timezone
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
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

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
            )],
            confidence=1.0,
            verification_status="observed",
        )


class RepositoryTreeEntry(BaseModel):
    path: str
    kind: Literal["blob", "tree", "commit"]
    sha: str
    size: int | None = None


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
    facts: list[GenomeFact] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
