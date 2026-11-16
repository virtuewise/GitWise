from fastapi import FastAPI

from gitwise_api.config import get_settings

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    description="Repository intelligence and Software Genome services for GitWise.",
    version="0.1.0",
)


@app.get("/health", tags=["operations"])
async def health_check() -> dict[str, str]:
    """Return process health without exposing configuration or secrets."""
    return {"status": "ok", "service": "gitwise-api"}


@app.get("/", tags=["operations"])
async def root() -> dict[str, str]:
    return {
        "name": "GitWise",
        "message": "The intelligence layer for software development.",
        "docs": "/docs",
    }
