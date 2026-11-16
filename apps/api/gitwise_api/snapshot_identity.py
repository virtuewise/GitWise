"""Stable, backend-neutral identity helpers for immutable Software Genome snapshots."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from gitwise_api.genome import SoftwareGenomeSnapshot


def snapshot_storage_key(snapshot: SoftwareGenomeSnapshot) -> str:
    """Return the canonical key for a provider repository and resolved commit.

    GitHub's numeric repository ID survives owner/name changes. The mutable
    repository slug is retained as display metadata, never used as the sole key.
    """
    if snapshot.provider_repository_id is None:
        raise ValueError("A provider repository ID is required for durable snapshot identity.")
    return (
        f"github:{snapshot.provider_repository_id}:"
        f"{snapshot.commit_sha.lower()}:schema-{snapshot.schema_version}"
    )


def _canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        result = {key: _canonicalize(item) for key, item in value.items()}
        if "observed_at" in result and "fact_id" in result:
            result.pop("observed_at")
        if "generated_at" in result:
            result.pop("generated_at")
        return result
    if isinstance(value, list):
        normalized = [_canonicalize(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ),
        )
    return value


def snapshot_content_digest(snapshot: SoftwareGenomeSnapshot) -> str:
    """Hash snapshot content while excluding observation-time-only metadata.

    Equivalent observations of the same commit therefore produce the same
    digest even when fetched at different times or returned in a different
    order by an upstream API.
    """
    payload = snapshot.model_dump(mode="json")
    canonical = _canonicalize(payload)
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()
