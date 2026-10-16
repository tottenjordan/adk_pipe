# Contributing

[CODE_STANDARDS.md](CODE_STANDARDS.md) is the authoritative guide for packaging (`uv`),
linting and formatting (`ruff`), type checking (`ty`), testing (`pytest`) and commits.

## Workflow

- Branch off `main`, open a PR, and merge only when CI is green.
- Delete the branch only after `gh pr view N --json state` prints `MERGED`.
- Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, ...).
- Never add `Co-Authored-By` or "Generated with" trailers to commits or PR bodies.
- Write the failing test first. For each behaviour change, do one mutation check:
  break the new code, watch the test go red, restore it.

## Commands

```bash
uv sync
uv run ruff format . && uv run ruff check .
uv run ty check
GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 16
cd frontend && npm test   # Vitest
```

`make help` lists shortcuts for these.

## Docs

[CLAUDE.md](CLAUDE.md) is the index (commands, architecture, invariants); subsystem detail
lives in [docs/reference/](docs/reference/).

## Dependabot PRs

- Python (uv) bumps change `uv.lock`; regenerate the exported requirements on the PR branch,
  or CI's drift check fails:
  `uv export --format requirements-txt --no-hashes --no-dev --no-emit-project -o requirements.txt`
- After a `cloud_functions/creative_fanout/requirements.txt` bump, regenerate
  `requirements.lock.txt` with the `uv pip compile` command in its header.
