# Contributing to Scorched

Thanks for your interest in contributing. Scorched is a personal trading framework that's open-sourced for the community to learn from, fork, and improve.

## How to Contribute

### Reporting Bugs

Open a [GitHub Issue](https://github.com/willcassell/scorched/issues) with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Relevant logs (`docker compose logs tradebot --tail=100`)

### Suggesting Features

Open a GitHub Issue with the `enhancement` label. Describe the use case, not just the feature — understanding *why* helps evaluate the idea.

### Submitting Code

1. Fork the repo and create a branch from `main`
2. Make your changes — keep PRs focused on one thing
3. Run the tests: `docker compose exec tradebot pytest`
4. Open a PR with a clear description of what changed and why

### What Makes a Good PR

- **Focused** — one fix or feature per PR, not a grab bag
- **Tested** — if you're touching risk management, broker logic, or the Claude pipeline, add or update tests
- **Documented** — if you add a new data source, config option, or endpoint, update the relevant docs (CLAUDE.md, README, DEPLOY.md)
- **No scope creep** — don't refactor surrounding code or add "improvements" beyond what the PR is about

## Development Setup

```bash
git clone https://github.com/willcassell/scorched.git
cd scorched
cp .env.example .env
# Edit .env — set ANTHROPIC_API_KEY at minimum
docker compose up -d --build
```

Verify: `curl http://localhost:8000/health`

### Running Tests

```bash
docker compose exec tradebot pytest
```

## Code Style

- Python, formatted with Ruff (line length 100)
- No unnecessary abstractions — three similar lines > a premature helper
- Comments only where the logic isn't self-evident
- All trading-day logic uses `market_today()` / `market_now()` from `tz.py` — never `date.today()`

## Pre-commit hooks (recommended)

The repo ships with a `.pre-commit-config.yaml` that runs `scripts/check_strategy_docs.py`
before every commit, catching drift between `strategy.json` and the markdown docs that
describe it. To enable locally:

```bash
pip install pre-commit
pre-commit install
```

Every `git commit` then runs the lint. The same check runs in CI via
`.github/workflows/docs-sync.yml`, so PRs are protected even if you skip the local hook.

To run the check manually at any time:
```bash
python3 scripts/check_strategy_docs.py
```

When `strategy.json` changes a numeric rule, append the OLD value's phrasing to
`STALE_PATTERNS` in `scripts/check_strategy_docs.py` so future regressions get caught.

## What We're Not Looking For

- Enterprise auth/RBAC systems — this is a personal tool
- Alternative AI providers — the Claude pipeline is core to the design
- Complex deployment orchestration (Kubernetes, Terraform) — Docker Compose on a single VM is the target
- Backtesting engines — the paper broker is for workflow validation, not historical simulation
