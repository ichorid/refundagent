"""FastAPI application entry point and factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from saferefund.agent.gateway import ModelGateway
from saferefund.api.routes import inbound_router, operator_router, verification_router
from saferefund.db import (
    DEFAULT_DATABASE_URL,
    create_all,
    create_database_engine,
    create_session_factory,
    dispose_database,
)


def create_app(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    model_gateway: ModelGateway | None = None,
    database_engine: AsyncEngine | None = None,
) -> FastAPI:
    """Build the HTTP application with injectable persistence and model gateway."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if database_engine is not None:
            await create_all(database_engine)
            yield
            await dispose_database(database_engine)
            return
        yield

    app = FastAPI(title="SafeRefundAgent", lifespan=lifespan)
    app.state.session_factory = session_factory
    app.state.model_gateway = model_gateway or ModelGateway.heuristic_subprocess()
    app.include_router(inbound_router)
    app.include_router(operator_router)
    app.include_router(verification_router)
    return app


_default_engine = create_database_engine(DEFAULT_DATABASE_URL)
_default_session_factory = create_session_factory(_default_engine)
app = create_app(
    session_factory=_default_session_factory,
    database_engine=_default_engine,
)
