from urllib.parse import urlparse

from fastapi import HTTPException, status
from pydantic import BaseModel, Field


class RepositoryReference(BaseModel):
    owner: str
    name: str
    full_name: str
    canonical_url: str


class RepositoryInspectRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


def parse_github_repository_url(url: str) -> RepositoryReference:
    """Parse a GitHub repository URL without making network requests."""
    parsed = urlparse(url.strip())

    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide an HTTPS URL for a repository hosted on github.com.",
        )

    if parsed.username or parsed.password or parsed.port:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Credentials and custom ports are not allowed in repository URLs.",
        )

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="URL must point directly to a GitHub repository (owner/repository).",
        )

    owner, name = parts
    if owner in {".", ".."} or name in {".", ".."} or name.endswith(".git"):
        if name.endswith(".git"):
            name = name[:-4]
        else:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid repository path.",
            )

    if not owner or not name or any(char in owner + name for char in "\\?#"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid repository owner or name.",
        )

    full_name = f"{owner}/{name}"
    return RepositoryReference(
        owner=owner,
        name=name,
        full_name=full_name,
        canonical_url=f"https://github.com/{full_name}",
    )
