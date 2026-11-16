"""Stable, backend-neutral identity helpers for immutable Software Genome snapshots."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from gitwise_api.genome import SoftwareGenomeSnapshot

# These fields represent collections whose order is not meaningful to the
# Software Genome. Lists inside fact values are intentionally not included:
# their order may carry domain meaning and must affect the digest.
_UNORDERED_LIST_FIELDS = {
    "architecture_edges",
    "architecture_nodes",
    "evidence",
    "facts",
    "limitations",
    "tree",
}


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


def _canonicalize(value: Any, *, field_name: str | None = None) -> Any:
    if isinstance(value, dict):
        result = {
            key: _canonicalize(item, field_name=key)
            for key, item in value.items()
        }
        if "observed_at" in result and "fact_id" in result:
            result.pop("observed_at")
        if field_name is None and "generated_at" in result:
            result.pop("generated_at")
        return result
    if isinstance(value, list):
        normalized = [
            _canonicalize(item)
            for item in value
        ]
        if field_name not in _UNORDERED_LIST_FIELDS:
            return normalized
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
    digest when unordered Genome collections arrive in a different order.
    Ordered lists inside fact values remain ordered and affect the digest.
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
