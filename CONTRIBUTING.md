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

- Derived files are regenerated automatically: Python (uv) bumps change `uv.lock` but not the
  exported root `requirements.txt`, and Cloud Function (pip) bumps change
  `cloud_functions/creative_fanout/requirements.txt` but not its `requirements.lock.txt`.
  `.github/workflows/dependabot-lock-sync.yml` regenerates both on Dependabot PRs, pushes a
  `chore(deps): regenerate derived requirements files` commit as `github-actions[bot]`, then
  starts `python-ci` / `crf-deps` on it via `workflow_dispatch` (a `GITHUB_TOKEN` push starts no
  CI by itself). Judge the PR by those dispatched runs on the new head commit; the
  `pull_request` runs GitHub queues for the bot's push wait for approval and can be ignored.
  The first `python-ci` run (on Dependabot's own commit) is expected to fail the drift check.
- Once the fix-up commit is on its branch, Dependabot treats the PR as edited and refuses
  `@dependabot rebase`; comment `@dependabot recreate` instead when it needs updating (it
  rebuilds the branch from scratch, and the sync workflow runs again on it).
- Manual fallback (uv 0.12.19), if the workflow didn't run or its push was rejected:
  `uv export --format requirements-txt --no-hashes --no-dev --no-emit-project -o requirements.txt`,
  and for the Cloud Function the `uv pip compile` command in `requirements.lock.txt`'s header.
