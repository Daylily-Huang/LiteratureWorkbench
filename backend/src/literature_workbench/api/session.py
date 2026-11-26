from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Request, Response
from pydantic import BaseModel, ConfigDict


router = APIRouter(prefix="/api/session", tags=["session"])


class BootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str


@router.post("/bootstrap", status_code=204)
def bootstrap_session(
    request: Request,
    payload: Annotated[BootstrapRequest, Body()],
) -> Response:
    if request.url.query:
        return Response(status_code=404)
    session = request.app.state.local_session
    if not session.consume_bootstrap_token(payload.token):
        return Response(status_code=403)
    response = Response(status_code=204)
    response.set_cookie(
        session.cookie_name,
        session.session_cookie,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )
    return response


@router.get("/current")
def current_session(request: Request) -> dict[str, str]:
    session = request.app.state.local_session
    return {
        "csrf_token": session.csrf_token,
        "session_generation": session.session_generation,
    }
