from datetime import UTC, datetime

import pytest

from gitwise_api.evolution import compare_genomes
from gitwise_api.genome import (
    ArchitectureEdge,
    ArchitectureNode,
    GenomeEvidence,
    GenomeFact,
    SoftwareGenomeSnapshot,
)


def snapshot(commit: str) -> SoftwareGenomeSnapshot:
    return SoftwareGenomeSnapshot(
        repository="acme/widget",
        repository_url="https://github.com/acme/widget",
        default_branch="main",
        commit_sha=commit,
        tree_sha=f"tree-{commit}",
        is_private=False,
    )


def dependency(commit: str, constraint: str) -> GenomeFact:
    return GenomeFact(
        fact_id=f"fact-{commit}-{constraint}",
        fact_type="dependency.declared",
        value={
            "name": "Example-Lib",
            "constraint": constraint,
            "ecosystem": "npm",
            "group": "dependencies",
        },
        evidence=[GenomeEvidence(
            source_path="frontend/package.json",
            source_url=f"https://github.com/acme/widget/blob/{commit}/frontend/package.json",
            commit_sha=commit,
            extraction_method="github_git_blob_manifest_parser",
        )],
        confidence=1.0,
        verification_status="observed",
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_compares_dependency_add_remove_and_constraint_changes_with_evidence() -> None:
    base = snapshot("base1234567")
    head = snapshot("head1234567")
    base.facts = [
        dependency(base.commit_sha, "^1.0.0"),
        GenomeFact(
            fact_id="removed",
            fact_type="dependency.declared",
            value={"name": "old-lib", "constraint": "2.0", "ecosystem": "npm"},
            evidence=[GenomeEvidence(
                source_path="package.json",
                commit_sha=base.commit_sha,
                extraction_method="github_git_blob_manifest_parser",
            )],
            confidence=1.0,
            verification_status="observed",
        ),
    ]
    head.facts = [
        dependency(head.commit_sha, "^2.0.0"),
        GenomeFact(
            fact_id="added",
            fact_type="dependency.declared",
            value={"name": "new-lib", "constraint": "3.0", "ecosystem": "npm"},
            evidence=[GenomeEvidence(
                source_path="package.json",
                commit_sha=head.commit_sha,
                extraction_method="github_git_blob_manifest_parser",
            )],
            confidence=1.0,
            verification_status="observed",
        ),
    ]

    result = compare_genomes(base, head)
    changes = {(item.change, item.before and item.before.get("name") or item.after["name"]): item
               for item in result.changes}

    assert result.repository == "acme/widget"
    assert result.base_commit_sha == base.commit_sha
    assert result.head_commit_sha == head.commit_sha
    assert result.change_count == 3
    assert changes[("changed", "Example-Lib")].before["constraint"] == "^1.0.0"
    assert changes[("changed", "Example-Lib")].after_evidence[0].commit_sha == head.commit_sha
    assert changes[("removed", "old-lib")].before_evidence[0].commit_sha == base.commit_sha
    assert changes[("added", "new-lib")].after_evidence[0].commit_sha == head.commit_sha


def test_compares_architecture_nodes_and_imports_deterministically() -> None:
    base = snapshot("base1234567")
    head = snapshot("head1234567")
    base.architecture_nodes = [
        ArchitectureNode(module_name="pkg.core", source_path="src/pkg/core.py"),
        ArchitectureNode(module_name="pkg.old", source_path="src/pkg/old.py"),
    ]
    head.architecture_nodes = [
        ArchitectureNode(module_name="pkg.core", source_path="src/pkg/core.py"),
        ArchitectureNode(module_name="pkg.new", source_path="src/pkg/new.py"),
    ]
    base.architecture_edges = [
        ArchitectureEdge(
            source_module="pkg.service",
            target_module="pkg.old",
            source_path="src/pkg/service.py",
            line_number=1,
            commit_sha=base.commit_sha,
        )
    ]
    head.architecture_edges = [
        ArchitectureEdge(
            source_module="pkg.service",
            target_module="pkg.new",
            source_path="src/pkg/service.py",
            line_number=1,
            commit_sha=head.commit_sha,
        )
    ]

    first = compare_genomes(base, head)
    second = compare_genomes(base, head)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert [(item.category, item.change) for item in first.changes] == [
        ("architecture.import", "added"),
        ("architecture.import", "removed"),
        ("architecture.node", "added"),
        ("architecture.node", "removed"),
    ]
    assert all(
        item.after_evidence[0].commit_sha == head.commit_sha
        for item in first.changes
        if item.category == "architecture.import" and item.change == "added"
    )


def test_rejects_comparison_across_different_repositories() -> None:
    base = snapshot("base1234567")
    head = snapshot("head1234567")
    head.repository = "another/project"

    with pytest.raises(ValueError, match="same repository"):
        compare_genomes(base, head)


def test_ignores_non_dependency_facts() -> None:
    base = snapshot("base1234567")
    head = snapshot("head1234567")
    base.facts = [GenomeFact.observed(
        repository=base.repository,
        commit_sha=base.commit_sha,
        fact_type="repository.description",
        value="old description",
    )]
    head.facts = [GenomeFact.observed(
        repository=head.repository,
        commit_sha=head.commit_sha,
        fact_type="repository.description",
        value="new description",
    )]

    assert compare_genomes(base, head).changes == []


def test_unchanged_import_is_not_reported_as_changed_only_because_commit_advanced() -> None:
    base = snapshot("base1234567")
    head = snapshot("head1234567")
    for target, commit in ((base, base.commit_sha), (head, head.commit_sha)):
        target.architecture_edges = [ArchitectureEdge(
            source_module="pkg.service",
            target_module="pkg.core",
            source_path="src/pkg/service.py",
            line_number=4,
            commit_sha=commit,
        )]

    result = compare_genomes(base, head)

    assert result.changes == []
