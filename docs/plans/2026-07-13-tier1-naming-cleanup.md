# Tier 1 Repo Naming Cleanup + Subagent-Driven-Development Skill Set — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task
> — OR, once **Tasks 1–2** land, use the newly-created `subagent-driven-development` skill for
> Tasks 3–6 (fresh implementer subagent per task + spec-then-quality review).
> On execution, copy this plan to `docs/plans/2026-07-13-tier1-naming-cleanup.md` first.

**Goal:** (1) Author `subagent-driven-development` plus the four skills it references
(`requesting-code-review`, `receiving-code-review`, `test-driven-development`,
`finishing-a-development-branch`) at user scope, then (2) do the low-risk "Tier 1" naming cleanup of
the repo: rename the misspelled `cloud_funktions/` → `cloud_functions/` and its opaque sub-packages,
tidy the trawler stub, and update all documentation — with **no `src/` move** and **no change to the
deployed Cloud Run services or Agent Engine deploy path**.

**Architecture:** Keep the flat top-level package layout (a `src/` move is deliberately out of scope —
Agent Engine's `extra_packages` staging does `tarfile.add(path)`, preserving the given relative path as
the arcname, so `./src/creative_agent` would only be importable as `src.creative_agent` and break every
bare import; the clean `sys_paths` fix is not exposed on the `vertexai.Client()` deploy API we use).
Tier 1 is a pure source-tree rename: `git mv` + import/doc updates, verified by the existing pytest
suite. Deployed service names (`creative-worker-crf`, `creative-trawler-crf`) and their env vars are
**unchanged** — only the local `--source` deploy path moves. Skills are user-scoped Markdown files
(not committed to the repo).

**Tech Stack:** Python 3.13, `uv`, `uvx ruff`, pytest, `git mv` (history-preserving), functions-framework
/ `gcloud run deploy`, ADK skills (Markdown + YAML frontmatter).

**Standing constraints:** `export PATH="$HOME/.local/bin:$PATH"` before any `uv`/`uvx`; never add
`Co-Authored-By` trailers; `uvx ruff` for lint+format (ruff not in venv); pytest + `ty`; commit
per-task; **only push/PR when the user asks**; do NOT commit the PyPI-re-resolved `uv.lock` or
`.python-version`.

---

## Context

**Why:** The audit for a possible `src/` restructure concluded the deploy tooling is too tightly
coupled to the flat layout for a `src/` move to be worth it — but two naming warts remain and are
cheap to fix: (1) `cloud_funktions/` (the "k" is a gag), and (2) `creative_crf/` / `trawler_crf/`
("crf" = Cloud Run Function, redundant once the parent is `cloud_functions/`, and the names don't say
what each does; `trawler_crf/main.py` is a `# TODO` stub).

**Prompted by:** the user's request to (a) port the `subagent-driven-development` skill (from
`github.com/jswortz/my-skills`) so future plans run with a fresh-subagent-per-task + two-stage-review
loop, (b) **also port the four skills it references** so its cross-links resolve for real, and (c)
capture Tier 1 as an executable plan. The skills are created first so the rest of this plan can be
executed with them.

**Outcome:** a five-skill execution toolkit at `~/.claude/skills/`; a clean, professionally-named
source tree; all documentation updated; zero change to running infrastructure; the full pytest suite
still green.

**Chosen names (decided with the user):**
- `cloud_funktions/` → `cloud_functions/`
- `cloud_funktions/creative_crf/` → `cloud_functions/creative_fanout/` (holds BOTH orchestrator
  `crf_entrypoint` and worker `agent_worker_entrypoint`)
- `cloud_funktions/trawler_crf/` → `cloud_functions/trawler_scheduler/` (the scheduler stub)

**Rename blast radius (verified):** live imports/paths in `deployment/test_deployment.py`,
`tests/test_crf_entrypoint.py`, `tests/test_crf_logic.py`, `tests/test_crf_worker_async.py`,
`deployment/deploy_agent.py` (1), `README.md` (3), `CLAUDE.md` (5),
`docs/notes/ambient-agents-vs-cloud-functions.md` (5), `.gitignore:9`. **Historical records in
`docs/plans/2026-07-12-*.md` and `docs/plans/2026-07-13-*.md` are intentionally left untouched.**

**Environment adaptations (the source skills assume tools this setup lacks):** they use a
`superpowers:code-reviewer` agent type, a raw "Task tool", `TodoWrite`, and `using-git-worktrees`.
Map these across ALL five skills to: `subagent_type: "general-purpose"` (via the **Agent tool**);
`TaskCreate`/`TaskUpdate`/`TaskList`; and worktree steps made **conditional** (this repo usually
isn't in a worktree). Cross-references use the bare skill names installed here
(`executing-plans`, `writing-plans`, `writing-skills`, plus the four created in Task 2).

---

## Task 1: Author the `subagent-driven-development` skill (user scope)

**Files (create — user-level, no git step):**
- `~/.claude/skills/subagent-driven-development/{SKILL.md, implementer-prompt.md, spec-reviewer-prompt.md, code-quality-reviewer-prompt.md}`

**Step 1: Write `SKILL.md`:**

```markdown
---
name: subagent-driven-development
description: Use when executing an implementation plan whose tasks are mostly independent, in the current session, with a fresh subagent per task and a two-stage (spec then quality) review after each.
---

# Subagent-Driven Development

## Overview
Execute an implementation plan by dispatching a fresh subagent per task, then a mandatory two-stage
review — spec compliance first, code quality second. Core principle: **fresh subagent per task +
two-stage review (spec then quality) = high quality, fast iteration.**

## When to Use
- You have a written plan (e.g. from `writing-plans`) with mostly independent tasks
- You want to stay in the current session (no parallel-session handoff)
- You want fresh context per task (no pollution) plus layered review

Use `executing-plans` instead when tasks are tightly coupled or you prefer batch execution in a
separate session.

## The Process
### Setup
1. Read the plan once, yourself (the controller).
2. Extract each task's full text + surrounding context.
3. Create one `TaskCreate` entry per task.

### Per-task loop
1. Dispatch ONE implementer subagent — Agent tool, `subagent_type: "general-purpose"` — using
   `implementer-prompt.md`. Paste the task text inline; **never make the subagent read the plan file.**
   Subagents follow `test-driven-development` for each task.
2. If it asks questions → answer fully → re-dispatch.
3. It implements, tests, commits, self-reviews, and reports.
4. Dispatch the spec reviewer (`general-purpose`, read-only) using `spec-reviewer-prompt.md`.
   Issues → implementer fixes (per `receiving-code-review`) → re-review until ✅.
5. Dispatch the code-quality reviewer (`general-purpose`, read-only) using
   `code-quality-reviewer-prompt.md` (aligned with `requesting-code-review`'s `code-reviewer.md`).
   Issues → implementer fixes → re-review until ✅.
6. Mark the task complete (`TaskUpdate`).
7. Next task.

### After all tasks
- Dispatch a final reviewer over the whole diff (base..HEAD) via `requesting-code-review`.
- Complete the work with `finishing-a-development-branch`.

## Spec vs Quality review (separate, ordered)
| Stage | Purpose | Checks |
|---|---|---|
| Spec compliance (first) | Built what was asked? | Missing features, extra/unrequested additions, misunderstandings |
| Code quality (second) | Well-built? | Naming, structure, magic numbers, test coverage |

**Never start the quality review before spec compliance is ✅ — order matters.**

## Environment notes (this setup)
- `subagent_type` for implementer AND reviewers = `general-purpose`; reviewers report only, never edit.
- Task tracking via `TaskCreate` / `TaskUpdate` / `TaskList` (not `TodoWrite`).
- Subagents must honor standing constraints: `uv`/`uvx` only, `uvx ruff` for lint+format, no
  `Co-Authored-By` trailers, don't stage `uv.lock`/`.python-version`, commit per task, push only when asked.

## Common Mistakes (Red Flags)
Never: skip a review stage; proceed with open review issues; run parallel implementers; make a
subagent read the plan file; omit scene-setting context; ignore subagent questions; accept
"close enough" on spec; skip re-review after fixes; treat implementer self-review as a substitute for
the review stages. **If a task fails:** dispatch a fix subagent with specific instructions rather than
fixing by hand.

## Integration
| Skill | Role |
|---|---|
| `writing-plans` | Creates the plan this skill executes |
| `test-driven-development` | Subagents use this for each task |
| `requesting-code-review` | Template for the reviewer subagents |
| `receiving-code-review` | How the implementer responds to review |
| `finishing-a-development-branch` | Completes the work after all tasks |
| `executing-plans` | Alternative for parallel-session batch execution |
| `writing-skills` | How this skill itself was authored |
```

**Step 2–4: Write the three prompt templates** `implementer-prompt.md`, `spec-reviewer-prompt.md`,
`code-quality-reviewer-prompt.md` — content exactly as specified below.

*`implementer-prompt.md`:*
```markdown
# Implementer Subagent Prompt

Dispatch: Agent tool, `subagent_type: "general-purpose"`, description "Implement Task N: <name>".

---
You are implementing **Task N: <name>** from an approved plan. Work ONLY on this task.

## Task Description
<paste the task's full text here — do NOT tell the subagent to read the plan file>

## Context
<where this fits in the codebase, dependencies, files involved, patterns to follow>

## Before You Begin
If anything about requirements, approach, dependencies, or scope is unclear, ASK NOW before writing
code. Do not guess.

## Your Job
1. Implement the task following `test-driven-development` (write the failing test first).
2. Write/adjust tests that verify real behavior, not mocks.
3. Verify: run the relevant tests + `uvx ruff check`/`uvx ruff format`; confirm green.
4. Commit (per-task; no `Co-Authored-By`; don't stage `uv.lock`/`.python-version`).
5. Self-review, then report.

## Self-Review (before reporting)
- Completeness: did I fully implement everything in the spec?
- Quality: are names clear and accurate; does it follow existing patterns?
- Discipline: did I avoid overbuilding / unrequested features?
- Testing: do the tests verify behavior (not just mock behavior)?
Fix anything you find before reporting.

## Report Format
- What was implemented
- Testing: commands run + results
- Files changed
- Self-review findings
- Concerns / follow-ups
```

*`spec-reviewer-prompt.md`:*
```markdown
# Spec Compliance Reviewer Prompt

Dispatch: Agent tool, `subagent_type: "general-purpose"` (READ-ONLY — report only, never edit),
description "Review spec compliance for Task N".

---
Verify the implementer built exactly what **Task N** requested — nothing more, nothing less. The
implementer finished quickly and their report may be incomplete, inaccurate, or optimistic. Do NOT
take their word for it.

## Requirements (source of truth)
<paste the task's requirements here>

## Implementer's Report
<paste the implementer's report here>

## Do NOT
- Trust their claims about completeness
- Accept their interpretation of requirements

## DO
- Read the actual code (diff base..HEAD)
- Compare implementation to requirements line by line
- Check for missing pieces they claimed to implement

## Check three categories
1. Missing requirements — skipped or falsely claimed
2. Extra/unneeded work — over-engineering or unrequested features
3. Misunderstandings — wrong interpretation / wrong problem / wrong approach

## Output
- ✅ Spec compliant (everything matches after code inspection), OR
- ❌ Issues found — with `file:line` references

**Verify by reading code, not by trusting the report.**
```

*`code-quality-reviewer-prompt.md`:*
```markdown
# Code Quality Reviewer Prompt

Use ONLY after spec compliance is ✅. Dispatch: Agent tool, `subagent_type: "general-purpose"`
(READ-ONLY — report only, never edit), description "Review code quality for Task N".
This mirrors `requesting-code-review`'s `code-reviewer.md` template.

---
Verify the **Task N** implementation is well-built: clean, tested, maintainable.

## Inputs
- WHAT_WAS_IMPLEMENTED: <from the implementer's report>
- PLAN_OR_REQUIREMENTS: <task + plan reference>
- BASE_SHA: <commit before the task>
- HEAD_SHA: <current commit>
- DESCRIPTION: <one-line task summary>

## Review the diff (BASE_SHA..HEAD_SHA) for
- Naming clarity and accuracy
- Structure / duplication / magic numbers
- Test coverage and whether tests assert real behavior
- Adherence to existing codebase patterns and `CODE_STANDARDS.md`

## Output
- Strengths
- Issues, grouped Critical / Important / Minor (with `file:line`)
- Assessment: ✅ ready, or ❌ changes required
```

**Step 5: Verify wiring** — `ls` the 4 files; grep them for `superpowers:`, `TodoWrite`,
`Task tool` → adapt any hits; confirm valid frontmatter. **No commit.**

---

## Task 2: Author the four referenced skills (user scope)

Port each **verbatim** from its raw GitHub source, applying the shared environment adaptations from
Context (agent type → `general-purpose` via the Agent tool; `TodoWrite` → `Task*` tools; worktree
steps → conditional). Keep each skill's frontmatter `name`/`description` exactly as below so
discovery works. **User-level; no git step.**

**2a. `requesting-code-review`** — `~/.claude/skills/requesting-code-review/{SKILL.md, code-reviewer.md}`
- Source: `raw.githubusercontent.com/jswortz/my-skills/main/requesting-code-review/{SKILL.md,code-reviewer.md}`
- Frontmatter: `name: requesting-code-review` / `description: Use when completing tasks, implementing major features, or before merging to verify work meets requirements`
- Port `code-reviewer.md` (the 5-domain checklist template: Code Quality, Architecture, Testing,
  Requirements, Production Readiness; output = Strengths / Critical-Important-Minor issues with
  `file:line` / Recommendations / "Ready to merge?" verdict) verbatim.
- **Adapt:** "Dispatch a `superpowers:code-reviewer` subagent via the Task tool" → "Dispatch a
  `general-purpose` subagent via the Agent tool, filling the `code-reviewer.md` template."

**2b. `receiving-code-review`** — `~/.claude/skills/receiving-code-review/SKILL.md`
- Source: `.../receiving-code-review/SKILL.md` (self-contained, no sibling files).
- Frontmatter: `name: receiving-code-review` / `description: Use when receiving code review feedback,
  before implementing suggestions, especially if feedback seems unclear or technically questionable -
  requires technical rigor and verification, not performative agreement or blind implementation`
- Port verbatim (keep the 6-step response pattern, the no-performative-agreement rule, YAGNI check,
  push-back guidance, GitHub-thread-reply note). No agent-type adaptations needed.

**2c. `test-driven-development`** — `~/.claude/skills/test-driven-development/{SKILL.md, testing-anti-patterns.md}`
- Source: `.../test-driven-development/{SKILL.md,testing-anti-patterns.md}`
- Frontmatter: `name: test-driven-development` / `description: Use when implementing any feature or
  bugfix, before writing implementation code`
- Port SKILL.md verbatim (Iron Law, RED-GREEN-REFACTOR graphviz, rationalizations table, red flags,
  verification checklist) and its sibling `testing-anti-patterns.md` (the five anti-patterns + quick
  lookup) verbatim; keep the `@testing-anti-patterns.md` reference.
- **Adapt (one line):** examples are TS/`npm test`; add a short note "In this repo: `uv run pytest`
  + `uvx ruff`." Do not rewrite the examples.

**2d. `finishing-a-development-branch`** — `~/.claude/skills/finishing-a-development-branch/SKILL.md`
- Source: `.../finishing-a-development-branch/SKILL.md`
- Frontmatter: `name: finishing-a-development-branch` / `description: Use when implementation is
  complete, all tests pass, and you need to decide how to integrate the work - guides completion of
  development work by presenting structured options for merge, PR, or cleanup`
- Port verbatim (verify-tests-first; the exact 4 options: merge / push+PR / keep / discard; typed
  "discard" confirmation; the option/cleanup quick-reference table).
- **Adapt:** test command → `uv run pytest` + `uvx ruff`; make the Step-5 worktree cleanup
  **conditional** ("if working in a git worktree …") and soften the `using-git-worktrees` reference
  (not installed); honor the standing "only push/PR when the user asks" constraint under Option 2.

**Verify (Task 2):** `ls ~/.claude/skills/{requesting-code-review,receiving-code-review,test-driven-development,finishing-a-development-branch}`;
grep all new skill files for `superpowers:`, `TodoWrite`, `Task tool` → zero unadapted hits; confirm
each SKILL.md has valid frontmatter and that every `[[skill]]`/cross-reference now resolves to a real
skill dir under `~/.claude/skills/`. **No commit.**

---

## Task 3: Rename the directories + update all live imports & `.gitignore`

Rename + import fixes in ONE commit so the tree never lands with a red suite.

**Files:** `git mv cloud_funktions cloud_functions`; `git mv cloud_functions/creative_crf cloud_functions/creative_fanout`;
`git mv cloud_functions/trawler_crf cloud_functions/trawler_scheduler`; edit import strings in
`deployment/test_deployment.py`, `tests/test_crf_entrypoint.py`, `tests/test_crf_logic.py`,
`tests/test_crf_worker_async.py`, `deployment/deploy_agent.py`; edit `.gitignore:9` →
`**cloud_functions/creative_fanout/message.json`.

**Steps:**
1. `git mv` the three directories (history preserved). Relative imports inside `main.py`
   (`from .config`, `from .session`) need no change.
2. Update every live reference `cloud_funktions.creative_crf` → `cloud_functions.creative_fanout`
   (and any `trawler_crf` → `trawler_scheduler`). Test file *names* stay; only import strings change.
3. Update `.gitignore` line 9.
4. **Safety-net grep** (exclude `.venv`, `docs/plans/2026-07-*`):
   `grep -rn "cloud_funktions\|creative_crf\|trawler_crf" --include='*.py' --include='*.toml' --include='*.yml' .`
   → expected **no hits**.
5. Verify: `uv run pytest tests/ -q` (green, same count ≈173+), `uvx ruff check .` +
   `uvx ruff format --check .`, `uv run python -c "import cloud_functions.creative_fanout.main; print('OK')"`.
6. Commit: `refactor(cloud_functions): rename cloud_funktions→cloud_functions + crf subpkgs (creative_fanout, trawler_scheduler)`.

---

## Task 4: Tidy the `trawler_scheduler` stub

**Files:** `cloud_functions/trawler_scheduler/main.py` (currently a bare `# TODO`).

**Steps:**
1. Replace `# TODO` with a module docstring: this is the greenfield trend-trawler scheduler entrypoint
   (Cloud Scheduler → orchestrator), pointing at `docs/notes/ambient-agents-vs-cloud-functions.md`, plus
   a clearly-marked `# TODO(scheduler):`. Do **not** implement scheduling logic.
2. Verify: `uvx ruff check cloud_functions/trawler_scheduler/`.
3. Commit: `docs(trawler_scheduler): document the stub entrypoint + scope`.

---

## Task 5: Update ALL documentation

**Files:** `README.md` (3 refs — the `cd cloud_funktions/creative_crf` / `--source` deploy blocks),
`CLAUDE.md` (5 refs — architecture + Key Files), `docs/notes/ambient-agents-vs-cloud-functions.md`
(5 pointers + "Related" list), and any stray reference surfaced by the sweep below.

**Steps:**
1. Update every `cloud_funktions/creative_crf` → `cloud_functions/creative_fanout` (and `trawler_crf`
   → `trawler_scheduler`) in the live docs, including the worker deploy `cd`/`--source` path.
   **Deployed service names/env vars stay the same** — do not rename `creative-worker-crf` etc.
2. **Leave `docs/plans/2026-07-12-*.md` and `docs/plans/2026-07-13-*.md` unchanged** (historical).
3. **Full doc sweep:** `grep -rn "cloud_funktions\|creative_crf\|trawler_crf" --include='*.md' .`
   (exclude `docs/plans/2026-07-*`) → resolve any remaining live hit; expected clean afterward.
4. Commit: `docs: update deploy paths + pointers for cloud_functions rename`.

---

## Task 6: Verification & optional live re-validation

**No-creds gate (required):**
```bash
export PATH="$HOME/.local/bin:$PATH"
uv run pytest tests/ -q
uvx ruff check . && uvx ruff format --check .
uv run python -c "import cloud_functions.creative_fanout.main; import cloud_functions.creative_fanout.session; print('OK')"
ls ~/.claude/skills/{subagent-driven-development,requesting-code-review,receiving-code-review,test-driven-development,finishing-a-development-branch}
```

**With-creds (optional but recommended — proves the renamed source still deploys):**
- Redeploy worker + orchestrator from the new path (service names unchanged):
  `gcloud run deploy creative-worker-crf --source cloud_functions/creative_fanout --function agent_worker_entrypoint …`
  (README flags: `--max-instances 1 --timeout 1800s --concurrency=1 --memory 8Gi --cpu 4 --region us-central1 --no-allow-unauthenticated`).
- Trigger a **1-row** batch on `target_trends_crf_p95` against v6 engine `4308043238033326080`;
  expect `1/1 PROCESSED`. (Engine + test table already exist.)

**Sequencing & PRs:** Tasks 1–2 (skills, user-level, no commits) → Tasks 3→4→5 (repo, one commit
each) → Task 6 (verify). Likely a single PR "Tier 1: cloud_functions naming cleanup" for Tasks 3–5 —
**opened only when the user asks.** Execute Tasks 3–6 with `executing-plans`, or with the
`subagent-driven-development` skill created in Tasks 1–2.
