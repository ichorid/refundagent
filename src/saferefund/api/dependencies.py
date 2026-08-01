"""FastAPI dependency providers for database sessions and model gateway selection."""

from typing import Annotated, cast

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund.agent.gateway import ModelGateway


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """Return the application session factory configured at startup."""
    if not hasattr(request.app.state, "session_factory"):
        message = "Application session factory is not configured."
        raise RuntimeError(message)
    return cast(
        "async_sessionmaker[AsyncSession]",
        request.app.state.session_factory,
    )


def get_model_gateway(request: Request) -> ModelGateway:
    """Return the configured model gateway owned by the trusted boundary."""
    model_gateway = cast(
        "ModelGateway | None",
        getattr(request.app.state, "model_gateway", None),
    )
    if model_gateway is None:
        return ModelGateway.heuristic_subprocess()
    if type(model_gateway) is not ModelGateway:
        message = "Application model gateway must be a trusted ModelGateway instance"
        raise TypeError(message)
    return model_gateway


SessionFactoryDep = Annotated[
    async_sessionmaker[AsyncSession],
    Depends(get_session_factory),
]
ModelGatewayDep = Annotated[ModelGateway, Depends(get_model_gateway)]
