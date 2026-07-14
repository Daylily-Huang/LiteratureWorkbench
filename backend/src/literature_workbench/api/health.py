from fastapi import APIRouter, Request
from pydantic import BaseModel


router = APIRouter(prefix="/api")


class HealthResponse(BaseModel):
    status: str
    version: str
    network_scope: str


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=request.app.state.settings.version,
        network_scope="loopback-only",
    )
