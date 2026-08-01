.PHONY: install demo run check postgres-test-up postgres-test-down check-postgres stress-postgres

install:
	uv sync

demo:
	uv run python -m saferefund.demo

run:
	uv run uvicorn saferefund.main:app --reload

POSTGRES_TEST_URL ?= postgresql+asyncpg://saferefund:saferefund@localhost:54329/saferefund_test
POSTGRES_COMPOSE_FILE ?= docker-compose.postgres-test.yml
POSTGRES_COMPOSE_PROJECT ?= saferefund-pgtest
POSTGRES_STRESS_ITERATIONS ?= 25
POSTGRES_COMPOSE = docker compose -p $(POSTGRES_COMPOSE_PROJECT) -f $(POSTGRES_COMPOSE_FILE)

define postgres_compose_teardown
$(POSTGRES_COMPOSE) down -v --remove-orphans
endef

postgres-test-up:
	$(POSTGRES_COMPOSE) up -d --wait

postgres-test-down:
	$(POSTGRES_COMPOSE) down -v --remove-orphans

check-postgres:
	@set -e; \
	trap '$(postgres_compose_teardown)' EXIT INT TERM; \
	$(POSTGRES_COMPOSE) up -d --wait; \
	SAFEREFUND_TEST_POSTGRES_URL=$(POSTGRES_TEST_URL) uv run pytest tests/postgres -q

stress-postgres:
	@set -e; \
	trap '$(postgres_compose_teardown)' EXIT INT TERM; \
	$(POSTGRES_COMPOSE) up -d --wait; \
	iteration=1; \
	while [ $$iteration -le $(POSTGRES_STRESS_ITERATIONS) ]; do \
		echo "PostgreSQL stress iteration $$iteration/$(POSTGRES_STRESS_ITERATIONS)"; \
		SAFEREFUND_TEST_POSTGRES_URL=$(POSTGRES_TEST_URL) uv run pytest tests/postgres -q || exit 1; \
		iteration=$$((iteration + 1)); \
	done

check:
	uv run ruff format --check . && uv run ruff check . && uv run mypy src && uv run lint-imports && uv run pytest -q
