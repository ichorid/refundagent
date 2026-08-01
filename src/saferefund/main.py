"""FastAPI application entry point and factory."""

from fastapi import FastAPI

from saferefund import db
from saferefund.agent import HeuristicModel, Model
from saferefund.api import mount_routes


def create_app(*, model: Model | None = None) -> FastAPI:
    """Build the HTTP application with optional model injection."""
    app = FastAPI(title="SafeRefundAgent")
    app.state.model = model or HeuristicModel()

    @app.on_event("startup")
    def _create_tables() -> None:
        db.create_all()

    mount_routes(app)
    return app


app = create_app()
