import pytest
from gitwise_api.storage import GenomeSnapshotStore


def test_snapshot_store_requires_async_postgresql_url() -> None:
    with pytest.raises(ValueError, match="postgresql\\+asyncpg"):
        GenomeSnapshotStore.from_url("sqlite+aiosqlite:///:memory:")
