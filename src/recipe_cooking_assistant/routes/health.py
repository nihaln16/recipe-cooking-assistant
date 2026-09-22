from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness check. Must not call OpenAI or read recipe data."""
    return {"status": "ok"}
