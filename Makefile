.PHONY: install demo run check

install:
	uv sync

demo:
	uv run python -m saferefund.demo

run:
	uv run uvicorn saferefund.main:app --reload

check:
	uv run ruff format --check . && uv run ruff check . && uv run mypy src && uv run pytest -q
