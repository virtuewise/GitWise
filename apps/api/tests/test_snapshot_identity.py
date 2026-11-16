from datetime import UTC, datetime

import pytest
from gitwise_api.genome import GenomeFact, SoftwareGenomeSnapshot
from gitwise_api.snapshot_identity import snapshot_content_digest, snapshot_storage_key


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
                fact_type="repository.primary_language",
                value="Python",
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


def test_content_digest_changes_when_observed_content_changes() -> None:
    first = make_snapshot(generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = make_snapshot(generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    second.description = "Updated description"

    assert snapshot_content_digest(first) != snapshot_content_digest(second)


def test_content_digest_is_independent_of_collection_order() -> None:
    first = make_snapshot(generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = make_snapshot(generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    first.facts.append(GenomeFact.observed(
        repository="acme/widget",
        commit_sha=first.commit_sha,
        fact_type="repository.description",
        value="A widget",
    ))
    second.facts = list(reversed(first.facts))

    assert snapshot_content_digest(first) == snapshot_content_digest(second)


def test_content_digest_preserves_order_inside_fact_values() -> None:
    first = make_snapshot(generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = make_snapshot(generated_at=datetime(2026, 1, 1, tzinfo=UTC))
    first.facts[0].value = ["compile", "test", "package"]
    second.facts[0].value = ["package", "test", "compile"]

    assert snapshot_content_digest(first) != snapshot_content_digest(second)
