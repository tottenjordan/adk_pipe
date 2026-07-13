# Code Standards

Standards and conventions that **must** be adhered to when writing code and making
environment changes in this repository. When in doubt, follow this document.

> Related skill: the [`modern-python`](https://github.com/jswortz/my-skills/tree/main/modern-python)
> skill captures the broader "modern Python tooling" rationale (uv / ruff / ty / pytest).
> This document is the repo-specific, authoritative subset.

## 1. Version Control & Commits

- **Never** add `Co-Authored-By` trailers to commits or pull requests.
- Do not commit or push unless explicitly asked. When asked, branch off `main`
  first rather than committing directly to it.
- Keep commit messages concise and descriptive of the change.

## 2. Python Packaging & Environments

Use **`uv` for everything** related to packages and environments. Never invoke bare
`pip` or `python`.

- Add / remove dependencies with `uv add <pkg>` / `uv remove <pkg>` — never edit the
  dependency lists in `pyproject.toml` by hand.
- Dev / test tooling goes in `[dependency-groups]` (PEP 735), e.g.
  `uv add --group dev <pkg>` — not `[project.optional-dependencies]`.
- Install with `uv sync` (or `uv sync --all-groups`).
- Run everything through `uv run <cmd>`; never manually activate a virtualenv
  (`source .venv/bin/activate`) or call the interpreter directly.
- Use `uv run --with <pkg> <cmd>` for one-off, ad-hoc dependencies.
- Commit `uv.lock` to version control.
- Target Python **`>=3.13`** (matches `pyproject.toml`).

## 3. Linting & Formatting

Use **`ruff` for both linting and formatting**. Never use `black`, `flake8`,
`isort`, or `pyupgrade`.

```bash
uv run ruff format .        # format
uv run ruff format --check  # verify formatting in CI
uv run ruff check .         # lint
uv run ruff check --fix .   # lint + autofix
```

## 4. Type Checking

Use **`ty`** for type checking (from the Astral team). Never use `mypy` or `pyright`.

```bash
uv run ty check src/   # or the relevant package directories
```

## 5. Testing

- **`pytest`** is the test runner. Python tests live in `tests/` (see
  [tests/README.md](./tests/README.md) for the suite layout); frontend tests
  live in `frontend/src/__tests__/` (Vitest).
- Enforce a coverage minimum where practical (`--cov`, `--cov-fail-under`).
- `ty` type-checking is part of the test/verification loop, not a separate optional
  step — code should pass `ty check` before it is considered done.

```bash
uv run pytest tests/ -v
uv run ty check
```

## 6. GCP Locations & Regions

Models and other cloud resources use **different** locations — keep them separate:

- **`GOOGLE_CLOUD_LOCATION=global`** — the Vertex GenAI model endpoint. The gemini-3
  models (`gemini-3.5-flash`, `gemini-3.1-pro-preview`, `gemini-3.1-flash-lite`) are
  only served from `global`; requesting them in `us-central1` returns `404 NOT_FOUND`.
- **`GCP_REGION=us-central1`** — the default region for all other resources
  (BigQuery, GCS, PubSub, Cloud Run Functions, Agent Engine).

When adding code that talks to Vertex models, target `global`; for any other GCP
resource, target `GCP_REGION`.

## 7. Anti-Patterns to Avoid

| Avoid | Use instead |
|-------|-------------|
| bare `pip install` / `python script.py` | `uv add` / `uv run` |
| `uv pip install` | `uv add` / `uv sync` |
| Editing `pyproject.toml` deps by hand | `uv add` / `uv remove` |
| `black` / `flake8` / `isort` | `ruff format` / `ruff check` |
| `mypy` / `pyright` | `ty` |
| `[project.optional-dependencies]` for dev tools | `[dependency-groups]` (PEP 735) |
| `source .venv/bin/activate` | `uv run <cmd>` |
| `requirements.txt` | `pyproject.toml` (projects) / PEP 723 (scripts) |
| `Co-Authored-By` trailers | (never add them) |

## 8. Current State / Known Gaps

These reflect the repo as of writing and are follow-ups, not exceptions to the rules:

- `ruff` and `ty` are **not yet** declared in `pyproject.toml` `[dependency-groups]`.
  They should be added via `uv add --group dev ruff ty` before the standards above are
  fully enforceable.
- `ruff` is partially configured (`[tool.ruff.lint.per-file-ignores]`); `[tool.ty]` is
  not configured yet.
- The `dev` dependency group currently contains only `pytest`.
