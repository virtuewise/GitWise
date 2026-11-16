from __future__ import annotations

from typing import Any, Literal

from gitwise_api.genome import GenomeEvidence, SoftwareGenomeSnapshot
from pydantic import BaseModel, ConfigDict, Field


ChangeKind = Literal["added", "removed", "changed"]
ChangeCategory = Literal["architecture.node", "architecture.import", "dependency.declared"]


class GenomeChange(BaseModel):
    """A deterministic difference between two commit-pinned Genome observations."""

    model_config = ConfigDict(extra="forbid")

    category: ChangeCategory
    change: ChangeKind
    identity: str
    before: Any | None = None
    after: Any | None = None
    before_evidence: list[GenomeEvidence] = Field(default_factory=list)
    after_evidence: list[GenomeEvidence] = Field(default_factory=list)


class GenomeComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    repository: str
    base_commit_sha: str
    head_commit_sha: str
    changes: list[GenomeChange] = Field(default_factory=list)

    @property
    def change_count(self) -> int:
        return len(self.changes)


def _dependency_identity(value: Any, source_path: str | None) -> str | None:
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    ecosystem = value.get("ecosystem")
    if not isinstance(name, str) or not isinstance(ecosystem, str):
        return None
    group = value.get("group", "")
    if not isinstance(group, str):
        group = str(group)
    return "|".join((
        source_path or "",
        ecosystem.casefold(),
        name.casefold(),
        group.casefold(),
    ))


def _evidence(fact: Any) -> list[GenomeEvidence]:
    return list(fact.evidence)


def compare_genomes(
    base: SoftwareGenomeSnapshot,
    head: SoftwareGenomeSnapshot,
) -> GenomeComparison:
    """Compare two snapshots of the same repository without guessing runtime impact.

    Architecture imports are keyed by source module, target module, source path,
    and line number. Dependency declarations are keyed by manifest, ecosystem,
    normalized package name, and declaration group.
    """
    if base.repository.casefold() != head.repository.casefold():
        raise ValueError("Genome snapshots must belong to the same repository.")

    changes: list[GenomeChange] = []

    base_nodes = {
        f"{node.module_name}|{node.source_path}": node
        for node in base.architecture_nodes
    }
    head_nodes = {
        f"{node.module_name}|{node.source_path}": node
        for node in head.architecture_nodes
    }
    for identity in sorted(base_nodes.keys() | head_nodes.keys()):
        old = base_nodes.get(identity)
        new = head_nodes.get(identity)
        if old == new:
            continue
        changes.append(GenomeChange(
            category="architecture.node",
            change="added" if old is None else "removed" if new is None else "changed",
            identity=identity,
            before=old.model_dump(mode="json") if old else None,
            after=new.model_dump(mode="json") if new else None,
        ))

    def edge_map(snapshot: SoftwareGenomeSnapshot) -> dict[str, Any]:
        return {
            "|".join((
                edge.source_module,
                edge.target_module,
                edge.source_path,
                str(edge.line_number),
            )): edge
            for edge in snapshot.architecture_edges
        }

    base_edges = edge_map(base)
    head_edges = edge_map(head)
    for identity in sorted(base_edges.keys() | head_edges.keys()):
        old = base_edges.get(identity)
        new = head_edges.get(identity)
        if (
            old is not None
            and new is not None
            and old.model_dump(exclude={"commit_sha"})
            == new.model_dump(exclude={"commit_sha"})
        ):
            continue
        changes.append(GenomeChange(
            category="architecture.import",
            change="added" if old is None else "removed" if new is None else "changed",
            identity=identity,
            before=old.model_dump(mode="json") if old else None,
            after=new.model_dump(mode="json") if new else None,
            before_evidence=(
                [GenomeEvidence(
                    source_path=old.source_path,
                    commit_sha=old.commit_sha,
                    extraction_method=old.extraction_method,
                )] if old else []
            ),
            after_evidence=(
                [GenomeEvidence(
                    source_path=new.source_path,
                    commit_sha=new.commit_sha,
                    extraction_method=new.extraction_method,
                )] if new else []
            ),
        ))

    def dependency_map(snapshot: SoftwareGenomeSnapshot) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for fact in snapshot.facts:
            if fact.fact_type != "dependency.declared":
                continue
            identity = _dependency_identity(
                fact.value,
                fact.evidence[0].source_path if fact.evidence else None,
            )
            if identity is not None:
                result[identity] = fact
        return result

    base_dependencies = dependency_map(base)
    head_dependencies = dependency_map(head)
    for identity in sorted(base_dependencies.keys() | head_dependencies.keys()):
        old = base_dependencies.get(identity)
        new = head_dependencies.get(identity)
        if old is not None and new is not None and old.value == new.value:
            continue
        changes.append(GenomeChange(
            category="dependency.declared",
            change="added" if old is None else "removed" if new is None else "changed",
            identity=identity,
            before=old.value if old else None,
            after=new.value if new else None,
            before_evidence=_evidence(old) if old else [],
            after_evidence=_evidence(new) if new else [],
        ))

    changes.sort(key=lambda item: (item.category, item.identity, item.change))
    return GenomeComparison(
        repository=head.repository,
        base_commit_sha=base.commit_sha,
        head_commit_sha=head.commit_sha,
        changes=changes,
    )
