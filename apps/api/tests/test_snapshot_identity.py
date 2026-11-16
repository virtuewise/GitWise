from datetime import UTC, datetime

import pytest
from gitwise_api.genome import GenomeFact, SoftwareGenomeSnapshot
from gitwise_api.snapshot_identity import snapshot_content_digest, snapshot_storage_key
from pydantic import ValidationError


def make_snapshot(*, generated_at: datetime, repository_id: int = 42) -> SoftwareGenomeSnapshot:
    return SoftwareGenomeSnapshot(
        repository="acme/widget",
        repository_url="https://github.com/acme/widget",
        provider_repository_id=repository_id,
        default_branch="main",
        commit_sha="ABCDEF1234567",
        tree_sha="tree1234567",
        is_private=False,
        generated_at=generated_at,
        facts=[
            GenomeFact.observed(
                repository="acme/widget",
                commit_sha="ABCDEF1234567",
                fact_type="project.configuration",
                value={"language": "Python"},
                source_path="pyproject.toml",
                source_url="https://github.com/acme/widget/blob/ABCDEF1234567/pyproject.toml",
                extraction_method="manifest_parser",
            )
        ],
    )


def test_storage_key_uses_provider_id_commit_and_schema_version() -> None:
    snapshot = make_snapshot(generated_at=datetime(2026, 1, 1, tzinfo=UTC))

    assert snapshot_storage_key(snapshot) == "github:42:abcdef1234567:schema-1.0"


def test_storage_key_requires_stable_provider_identity() -> None:
    snapshot = make_snapshot(generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    snapshot.provider_repository_id = None

    with pytest.raises(ValueError, match="provider repository ID"):
        snapshot_storage_key(snapshot)


def test_content_digest_ignores_observation_timestamps() -> None:
    first = make_snapshot(generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = make_snapshot(generated_at=datetime(2026, 2, 1, tzinfo=UTC))

    assert first.facts[0].observed_at != second.facts[0].observed_at
    assert snapshot_content_digest(first) == snapshot_content_digest(second)


def test_content_digest_ignores_mutable_repository_metadata_and_ref() -> None:
    first = make_snapshot(generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = make_snapshot(generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    second.repository = "new-owner/renamed-widget"
    second.repository_url = "https://github.com/new-owner/renamed-widget"
    second.description = "A changed description"
    second.default_branch = "trunk"
    second.requested_ref = "release/v2"
    second.primary_language = "Rust"
    second.is_private = True
    second.facts.append(GenomeFact.observed(
        repository="new-owner/renamed-widget",
        commit_sha=second.commit_sha,
        fact_type="repository.description",
        value="A changed description",
    ))

    assert snapshot_content_digest(first) == snapshot_content_digest(second)


def test_content_digest_ignores_fact_ids_and_mutable_source_urls() -> None:
    first = make_snapshot(generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = make_snapshot(generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    second.facts[0].fact_id = "different-id"
    second.facts[0].evidence[0].source_url = (
        "https://github.com/renamed-owner/renamed-repo/blob/ABCDEF1234567/pyproject.toml"
    )

    assert snapshot_content_digest(first) == snapshot_content_digest(second)


def test_content_digest_changes_when_commit_derived_content_changes() -> None:
    first = make_snapshot(generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = make_snapshot(generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    second.facts[0].value = {"language": "Python", "dependencies": ["httpx>=0.27"]}

    assert snapshot_content_digest(first) != snapshot_content_digest(second)


def test_content_digest_is_independent_of_collection_order() -> None:
    first = make_snapshot(generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = make_snapshot(generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    extra_first = GenomeFact.observed(
        repository="acme/widget",
        commit_sha=first.commit_sha,
        fact_type="project.manifest",
        value="package.json",
    )
    extra_second = GenomeFact.observed(
        repository="acme/widget",
        commit_sha=second.commit_sha,
        fact_type="project.manifest",
        value="package.json",
    )
    first.facts.append(extra_first)
    second.facts = [extra_second, second.facts[0]]

    assert snapshot_content_digest(first) == snapshot_content_digest(second)


def test_content_digest_preserves_order_inside_fact_values() -> None:
    first = make_snapshot(generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = make_snapshot(generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    first.facts[0].value = ["compile", "test", "package"]
    second.facts[0].value = ["package", "test", "compile"]

    assert snapshot_content_digest(first) != snapshot_content_digest(second)


def make_snapshot_with_fact(fact: GenomeFact) -> SoftwareGenomeSnapshot:
    return SoftwareGenomeSnapshot(
        repository="acme/widget",
        repository_url="https://github.com/acme/widget",
        provider_repository_id=42,
        default_branch="main",
        commit_sha="ABCDEF1234567",
        tree_sha="tree1234567",
        is_private=False,
        facts=[fact],
    )


def test_fact_identity_is_independent_of_mapping_insertion_order() -> None:
    common = {
        "repository": "acme/widget",
        "commit_sha": "ABCDEF1234567",
        "fact_type": "configuration.settings",
    }
    first = GenomeFact.observed(**common, value={"alpha": 1, "beta": 2})
    second = GenomeFact.observed(**common, value={"beta": 2, "alpha": 1})

    assert first.fact_id == second.fact_id
    assert snapshot_content_digest(make_snapshot_with_fact(first)) == snapshot_content_digest(
        make_snapshot_with_fact(second)
    )


def test_fact_values_reject_non_finite_numbers() -> None:
    with pytest.raises(ValueError):
        GenomeFact.observed(
            repository="acme/widget",
            commit_sha="ABCDEF1234567",
            fact_type="configuration.threshold",
            value=float("nan"),
        )


def test_fact_values_reject_non_json_python_objects() -> None:
    with pytest.raises(ValidationError):
        GenomeFact(
            fact_id="fact",
            fact_type="configuration.options",
            value={"enabled": {1, 2}},
            confidence=1.0,
            verification_status="observed",
        )
