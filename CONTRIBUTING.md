# Contributing

## Setup

```bash
uv sync                      # Python deps
python3 scripts/init_env.py  # .env with fresh local secrets (never overwrites)
docker compose up -d postgres redis
cd web && npm ci             # only if you touch the dashboard
```

## Run the tests without touching your dashboard's data

The test suite shares the development database by default, and some tests clear the task queue.
Point it at a throwaway database instead:

```bash
docker compose exec -T postgres psql -U aeos -d aeos -c "CREATE DATABASE aeos_test"
export AEOS_DATABASE_URL=postgresql://aeos:aeos@localhost:5432/aeos_test
export AEOS_READONLY_DATABASE_URL=postgresql://aeos_readonly:aeos_readonly@localhost:5432/aeos_test
uv run pytest tests/api tests/store tests/core tests/agents   # fast
uv run pytest tests/graph                                     # slow: real sandboxes, ~25 minutes
```

If your `.env` has `AEOS_GITHUB_REPO` and `AEOS_GITHUB_TOKEN`, also set `AEOS_TARGET_ENABLED=false` for the
test run. Otherwise a task submitted without a `kind` defaults to a "change request" on the real repo, the
scripted model has no answers for that, and `tests/graph` fails with "FakeLLM script exhausted".

Do not run two test processes against the same database at once; give each its own.

`make check` runs lint, mypy, the secret scan and the default test run.

## Changing the dashboard

The dashboard follows [DESIGN.md](DESIGN.md) (dark canvas, one accent, hairline borders). After a change:

```bash
cd web && npx tsc --noEmit
docker compose build web && docker compose up -d --no-deps web   # the image bakes the build in
```

## Conventions

- Conventional commits, one logical change each.
- Tests ship with the change. LLM-dependent code is tested against the scripted fake model, so CI is
  free and deterministic; anything that needs a real model is marked `live` and excluded by default.
- Agents never run code on the host. Execution goes through the sandbox.
- A non-obvious choice gets an entry in [docs/DECISIONS.md](docs/DECISIONS.md).
- Keep known limits honest: if something is not covered, say so in the README or the relevant doc.
