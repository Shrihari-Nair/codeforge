.PHONY: install lint typecheck secrets test check up down infra-check demo demo-down

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

# Free, scripted run: no Gemini key and no GitHub token. The "wrong_fix" scenario makes the first
# fix fail verification, so you see the replan and the second attempt pass. Then open :3100.
demo:
	@test -f .env || python3 scripts/init_env.py
	AEOS_DASHBOARD_LLM=fake AEOS_DASHBOARD_REPLAY=wrong_fix AEOS_GITHUB_REPO= AEOS_GITHUB_TOKEN= \
		docker compose up -d --build
	@echo "Dashboard: http://localhost:3100  (submit the pre-filled task, then approve it)"

demo-down:
	docker compose down
