"""The web image's build context must exclude files generated per checkout.

`frontend/tsconfig.tsbuildinfo` and `frontend/next-env.d.ts` are gitignored but
were uploaded into the `frontend/` build context, and they differ between
checkouts, so the builder's `COPY . .` missed the registry cache and re-ran the
whole `next build`. Playwright outputs are excluded for local docker builds too.
"""

from pathlib import Path

import pytest

DOCKERIGNORE = Path(__file__).resolve().parent.parent / "frontend" / ".dockerignore"


def _patterns() -> set[str]:
    lines = DOCKERIGNORE.read_text().splitlines()
    return {s for line in lines if (s := line.strip()) and not s.startswith("#")}


@pytest.mark.parametrize(
    "pattern",
    ["tsconfig.tsbuildinfo", "next-env.d.ts", "test-results", "playwright-report"],
)
def test_web_dockerignore_excludes_per_checkout_files(pattern: str) -> None:
    assert pattern in _patterns()


def test_web_dockerignore_keeps_existing_exclusions() -> None:
    assert {"node_modules", ".next", ".git", ".env*"} <= _patterns()
