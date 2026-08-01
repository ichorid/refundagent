"""Integration-test fixtures for the HTTP application."""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund.adapters import mailer, payment, reset_adapters_for_tests, ticketing
from saferefund.agent.gateway import ModelGateway
from saferefund.agent.locks import reset_case_locks_for_tests
from saferefund.main import create_app
from tests.integration.adapter_mediation import wrap_asserting_mediated_caller
from tests.support.model_gateway import heuristic_gateway


@pytest.fixture(autouse=True)
def _every_adapter_call_is_mediated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert across every scenario that no adapter effect runs outside the gate.

    Holds under ``importlib``/``getattr`` because the wrapped callables are patched
    on the adapter modules themselves.
    """
    for module, name in (
        (mailer, "send"),
        (payment, "refund"),
        (ticketing, "escalate"),
    ):
        original = getattr(module, name)
        wrapped = wrap_asserting_mediated_caller(original)
        monkeypatch.setattr(module, name, wrapped, raising=False)


@pytest.fixture
def api_session_factory(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Provide a seeded database and reset adapter and lock state for HTTP tests."""
    reset_adapters_for_tests()
    reset_case_locks_for_tests()
    return seeded_session_factory


@pytest.fixture
def injected_api_session_factory(
    injected_seed_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Provide the injection-variant seed profile with reset adapter state."""
    reset_adapters_for_tests()
    reset_case_locks_for_tests()
    return injected_seed_session_factory


@pytest.fixture
def api_model_gateway() -> ModelGateway:
    """Default deterministic model gateway for smoke tests."""
    return heuristic_gateway()


@pytest.fixture
async def api_client(
    api_session_factory: async_sessionmaker[AsyncSession],
    api_model_gateway: ModelGateway,
) -> AsyncIterator[AsyncClient]:
    """HTTP client bound to an isolated seeded application instance."""
    app = create_app(
        session_factory=api_session_factory,
        model_gateway=api_model_gateway,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
