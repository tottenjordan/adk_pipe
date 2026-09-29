"""Repo-owned Agent Runtime call sites must use agentplatform, not the
deprecated vertexai.Client().agent_engines API (P1). Any mention of
vertexai or agent_engines in the scanned trees fails, including in comments
and strings (``vertexai=`` keyword arguments such as
``genai.Client(vertexai=True)`` are allowed). ADK's own
VertexAiSessionService still uses vertexai internally; that's out of scope.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCANNED = ["deployment", "cloud_functions", "runserver", "experiments"]
LEGACY = re.compile(r"\bvertexai\b(?!\s*=)|\bagent_engines\b")


def test_no_vertexai_or_agent_engines_usage():
    offenders = [
        f"{path.relative_to(ROOT)}:{n}: {line.strip()}"
        for d in SCANNED
        for path in sorted((ROOT / d).rglob("*.py"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if LEGACY.search(line)
    ]
    assert not offenders, "\n".join(offenders)
