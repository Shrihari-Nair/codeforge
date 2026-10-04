.PHONY: install lint typecheck secrets test check up down infra-check

install:
	uv sync

lint:
	uv run ruff check . && uv run ruff format --check .

typecheck:
	uv run mypy src

test:
	uv run pytest

check: lint typecheck secrets test

up:
	docker compose up -d --wait

down:
	docker compose down

secrets:
	uv run python scripts/check_no_secrets.py

infra-check:
	uv run python scripts/check_infra.py
