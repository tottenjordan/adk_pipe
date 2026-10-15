# Wave-2 Leftovers Implementation Plan (2 parallel tracks, time-optimised)

> **For Claude:**
> - **Two tracks in parallel:**
>   - **Track A:** one implementer subagent (`subagent-driven-development`) does L2, the only long item.
>   - **Track B:** I do L1+L3 myself as one small PR in the main checkout while Track A runs; no agent startup or worktree overhead.
> - **Docs:** **all** CLAUDE.md / `tests/README.md` edits live in Track B's PR, so Track A never conflicts and nothing has to be rebased.
> - **Plan copy:** Track B copies this plan to `docs/plans/2026-10-15-wave2-leftovers.md`.
> - **Merging:** each PR is reviewed with `code-review-and-quality` and merged only after CI is green **and** `gh pr view --json state` shows MERGED; only then is the branch deleted.
> - **Deploy:** starts the minute the second PR merges (Task 3).

**Goal:** close the small leftovers recorded at the end of wave 2 in `agents/code_review.md`, with no behaviour change, in the least wall-clock time.

**Architecture:** two behaviour-preserving PRs, split by file ownership, plus one process note saved to memory. The proof of no behaviour change is the unchanged suite (5,595 tests), the judge-prompt golden, the render-parity golden and the instruction sha pins.

**Tech stack:** Python 3.13, ADK 2.10, pytest. Gates: `uv run ruff format --check . && uv run ruff check . && uv run ty check && env -u GOOGLE_GENAI_USE_ENTERPRISE -u GOOGLE_GENAI_USE_VERTEXAI GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 16` (`-n 16` is the fastest local setting).

---

## Context

Wave 2 (#450–#457, main `c91fddf`, judge `2026-10-15.2`) left these items, recorded in `agents/code_review.md` and memory:

1. **Logging at import:** `logging.basicConfig` still runs at import in `creative_agent/callbacks.py`, `creative_agent/tools.py`, `creative_agent/sub_agents/trend_researcher/agent.py` and `creative_agent/sub_agents/campaign_researcher/agent.py`. Logging is already configured by `deployment/async_app.py` and by `agent_common.observability.ensure_default_logging` (#453).
2. **Duplicate constant:** `creative_agent/trend_tie.py` keeps its own `_MIN_TOKEN_CHARS`; `text_match.MIN_TOKEN_CHARS` is the shared one since #456.
3. **`creative_eval/evaluate.py` size:** it grew to 1,458 lines, mixing gate/verdict normalisation, the judge client and request building, and report building.
4. **`image_aspect_ratios_allowed`** is still on `agent_common.config.BaseAgentConfiguration`, a creative-only knob, because `runserver/share_snapshot.py` reads it there.
5. **Process lesson:** delete a PR branch only after confirming the PR merged (#452 was closed by an early delete and recovered as #454).

## Time optimisations applied (vs the 3-agent version)

| Change | Saves |
|---|---|
| L1 and L3 are tiny (about 10 lines each), so I do them myself as one PR while the L2 agent runs | ~15 min of agent startup and worktree setup, plus one CI cycle and one merge |
| All doc edits go in Track B | No CLAUDE.md rebase conflicts (wave 2 lost about 10 min to these) |
| One targeted mutation per change instead of broad sweeps | ~10 min |
| No separate live creative run; the Cloud Build `_SMOKE=true` run already does a full creative run on the new engine, and its report is checked | ~10 min |
| Deploy engines and the api in parallel, and build the api image as soon as the last merge lands | ~5 min |
| Run the suite with `-n 16` locally | ~1 min per run |

**Expected wall clock: about 75 min** (Track A about 45 min is the critical path, about 10 min of review and merges, about 20 min to deploy). The 3-agent version was about 105.

## File ownership

| Track / PR | Branch | Owns (exclusive) |
|---|---|---|
| **A: L2, split `evaluate.py`** | `refactor/eval-modules` | `creative_eval/*`, eval tests, the judge-prompt golden (read-only). **No CLAUDE.md or tests/README edits.** |
| **B: L1 + L3 + docs** | `chore/wave2-leftovers` | `creative_agent/{callbacks,tools,trend_tie}.py`, `creative_agent/sub_agents/{trend_researcher,campaign_researcher}/agent.py`, `agent_common/config.py`, `creative_agent/config.py`, `runserver/share_snapshot.py`, `tests/test_logging_config.py`, trend_tie / share_snapshot / config tests, CLAUDE.md, `tests/README.md`, the plan copy |

## Track A: L2, split `creative_eval/evaluate.py` (~45 min, subagent)

1. **Before any change:** confirm the judge-prompt golden (`tests/test_judge_prompt_golden.py`) and the eval suite pass on main.
2. **Move code mechanically, by concept** (no logic edits):
   - **`creative_eval/gates.py`:** `normalize_gates`, `normalize_verdicts`, the code-decided gate rules, `unreported_gates_warning`, gate-set helpers.
   - **`creative_eval/judge.py`:** `_get_client`, `env_workers`, retry and timeout options, `fence`, `_call_judge`, the request builders, the image fallbacks.
   - **`creative_eval/report.py`:** `build_report`, the summary, `gates_pass_rate`, warnings, `_failed_score`, `_is_failed`.
   - **`evaluate.py`** keeps the public entry points.
3. **Imports:** update the ones in `creative_eval/*` and tests. Keep re-exports in `evaluate.py` **only** for importers outside Track A's files (`creative_agent/finalize.py`, `runserver/*`; grep), and list them in a comment. Don't edit those files.
4. **Sizes:** `evaluate.py` under 500 lines, each new module under 600. `JUDGE_VERSION` unchanged.
5. **Proof:**
   - the judge golden is byte-identical;
   - the full suite passes;
   - two mutations in the moved code, the CE-3 gates-reported rule and CE-9 `judge_failed`, each fail a test; `git diff --exit-code` is clean after reverting.
6. **Wrap-up:** run the gates and open a PR (`refactor(eval): split evaluate.py into gates, judge and report modules`). No attribution; don't merge or deploy. Report line counts and the re-export list, which I add to CLAUDE.md in Track B or a follow-up docs commit.

## Track B: L1 + L3 + docs (~20 min, me)

1. **Plan copy:** copy this plan to `docs/plans/2026-10-15-wave2-leftovers.md`.
2. **L1 logging:** remove the module-level `logging.basicConfig(...)` in the four files, keeping `logger = logging.getLogger(__name__)`. Extend `tests/test_logging_config.py` with a subprocess import of `creative_agent`, `interactive_creative` and `trend_scout` that asserts no root handlers are added. Mutation: re-add the call to `tools.py`, and the test fails.
3. **L1 constant:** `trend_tie` uses `text_match.MIN_TOKEN_CHARS`, and the local constant is deleted. The existing trend-tie tests are unchanged; add an assertion that the module no longer defines `_MIN_TOKEN_CHARS`.
4. **L3 config:** move `image_aspect_ratios_allowed` (field, parser, default) from `BaseAgentConfiguration` to `creative_agent.config.ResearchConfiguration`, and have `runserver/share_snapshot.py` read `creative_agent.config`.
   - Grep the other readers; attribute access is unchanged.
   - The deploy env contract and shipped-defaults tests must pass.
   - Test that `share_snapshot` uses the creative config value.
5. **Docs:** update the CLAUDE.md phrases (logging in `agent_common/observability.py`, config bullet, `creative_eval` module list for Track A's split) and `tests/README.md`.
6. **Wrap-up:** run the gates and open a PR (`chore: no logging setup at import, shared token length, aspect ratios in creative config`).

## Task 3: merge, deploy, verify (~20 min after both are ready)

1. **Merge:** merge whichever PR is green first, then the other; confirm `state=MERGED` each time before deleting its branch. Run the full suite on combined `main` with `-n 16`.
2. **Memory:** save a `feedback` memory on deleting branches only after a confirmed merge, while the builds run. Link it from the runbook and the MEMORY.md index.
3. **Deploy, in parallel:**
   - Cloud Build for creative + interactive (`_AGENTS=creative_agent,interactive_creative`; trend_scout is unchanged), `_SMOKE=true`.
   - The root image build, then the api (idle check, `--no-traffic`, pin). Web is unchanged.
4. **Handoff:** run `engine_handoff apply --update-worker` and delete the old creative and interactive engines.
5. **Verify without a separate run:**
   - read the smoke run's report from the build log or engine session: judge `2026-10-15.2`, eval pairing intact;
   - read the api's startup logs: no duplicate log lines and no errors.
6. **Wrap-up:** update memory and the status note in `agents/code_review.md`.

## Verification

- **Per PR:** the suite is unchanged (only import paths); the judge golden, render-parity golden and sha pins are byte-identical; the targeted mutations are still killed; CI is green and the PR is confirmed MERGED before its branch is deleted.
- **Deploy:** the smoke run passes on the new creative engine, and the api starts clean.
