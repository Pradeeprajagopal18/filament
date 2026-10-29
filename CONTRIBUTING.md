# Contributing

1. Read [AGENTS.md](AGENTS.md) — it applies to humans too.
2. `make setup`, then create a branch.
3. Make the change with tests. New API capabilities also need a CLI command and an `api.ts` method.
4. Run `make lint test` and `fil ready scan .` — the repo should stay grade A.
5. Open a pull request. CODEOWNERS review every change; CI must be green.
