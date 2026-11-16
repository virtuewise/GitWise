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

# Repository metadata can change without the commit changing. Keep it in the
# stored observation for display/audit, but do not let it alter commit content.
_MUTABLE_REPOSITORY_FACT_PREFIX = "repository."

# Digest v1 is deliberately an allowlisted projection. This prevents newly
# added presentation/provenance fields from silently changing persistence
# identity. Changing these semantics requires a schema-version decision.
_COMMIT_CONTENT_FIELDS = (
    "schema_version",
    "provider_repository_id",
    "commit_sha",
    "tree_sha",
    "truncated",
    "files_analyzed",
    "tree",
    "architecture_analysis",
    "architecture_nodes",
    "architecture_edges",
)


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
        normalized = [_canonicalize(item) for item in value]
        if field_name not in _UNORDERED_LIST_FIELDS:
            return normalized
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ),
        )
    return value


def _commit_content_projection(snapshot: SoftwareGenomeSnapshot) -> dict[str, Any]:
    """Project a snapshot onto immutable, commit-derived Genome content.

    Excludes mutable repository metadata (including repository.* facts),
    observation timestamps, generated fact IDs, and URL provenance. Evidence
    still retains commit, path, and extraction method, so content remains
    traceable without depending on a mutable owner/name URL.
    """
    payload = snapshot.model_dump(mode="json")
    projection = {field: payload[field] for field in _COMMIT_CONTENT_FIELDS}

    commit_facts = []
    for fact in payload["facts"]:
        if fact["fact_type"].startswith(_MUTABLE_REPOSITORY_FACT_PREFIX):
            continue
        stable_fact = {
            key: fact[key]
            for key in (
                "schema_version",
                "fact_type",
                "value",
                "confidence",
                "verification_status",
            )
        }
        stable_fact["evidence"] = [
            {
                key: evidence[key]
                for key in ("source_path", "commit_sha", "extraction_method")
            }
            for evidence in fact["evidence"]
        ]
        commit_facts.append(stable_fact)
    projection["facts"] = commit_facts
    return projection


def snapshot_content_digest(snapshot: SoftwareGenomeSnapshot) -> str:
    """Hash canonical commit-derived content, excluding mutable metadata.

    Re-observing the same commit under another ref or repository slug yields
    the same digest. Commit content, extraction results, and ordered fact-value
    arrays remain digest-significant.
    """
    canonical = _canonicalize(_commit_content_projection(snapshot))
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()
