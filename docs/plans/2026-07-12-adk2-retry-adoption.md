# ADK 2.0 Retry Adoption Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Adopt ADK 2.0's automatic node retries by attaching a scoped `RetryConfig` to the agents that call transient infrastructure, and stop the broad `except Exception` blocks in `tools.py` from swallowing retryable errors.

**Architecture:** ADK 2.0 runs agents/tools as nodes in a workflow graph. A node with a `retry_config` is retried by the framework when its execution raises an exception whose class name is listed in `RetryConfig.exceptions`. Today every infra tool wraps its body in `except Exception` and returns a status dict, so the framework never sees the failure and cannot retry. We (1) define a scoped `INFRA_RETRY` config, (2) attach it to the agents whose tools do network I/O, and (3) change those tools to log-and-`raise` instead of returning an error dict. One deliberate fallback is preserved.

**Tech Stack:** Python 3.13, `google-adk>=2.4.0`, `google-genai>=2.11.0`, `google-api-core`, `pytest`, `uv`, `ruff`.

---

## Critical background (read before starting)

1. **ADK matches retry exceptions by EXACT class name, not `isinstance`.** Source: `.venv/.../google/adk/workflow/utils/_retry_utils.py::_should_retry_node` does `type(exception).__name__ not in retry_config.exceptions`. Therefore base classes like `google.api_core.exceptions.GoogleAPICallError` will NEVER match — Google clients raise concrete subclasses (`ServiceUnavailable`, `InternalServerError`, …). We must enumerate concrete class names. Passing the exception *classes* is fine — `RetryConfig`'s validator converts them to `__name__` strings — and gives import-time typo safety, so prefer passing classes.
2. **`RetryConfig` is imported from `google.adk.workflow`** (`from google.adk.workflow import RetryConfig`). Fields: `max_attempts` (default 5; we use 3), `initial_delay`, `max_delay`, `backoff_factor`, `jitter`, `exceptions`.
3. **`retry_config` is a field on `BaseAgent`**, so pass it to any `Agent(...)` constructor. Verified: `Agent(name=..., model=..., retry_config=RetryConfig(...))` round-trips.
4. **HITL is safe:** `NodeInterruptedError` subclasses `BaseException` (not `Exception`), so `except Exception` never traps it. We are not changing that.
5. **Nothing is broken today** — there is no `RetryConfig` configured yet, so this is a net-new behavior, not a regression fix. But changing tools from "return error dict" to "raise" IS a behavior change that the LLM/agent flow will see, hence the tests and the live-eval gate.
6. **Environment:** always `export PATH="$HOME/.local/bin:$PATH"` before `uv`. Use `uvx ruff` (ruff is not in the venv). Never add `Co-Authored-By` trailers. Branch is `chore/adk2-retry-adoption` (already created off `main`).

## Scoped exception set (the concrete transient class names)

- `creative_agent` (uses genai + GCS + BigQuery):
  `google.api_core.exceptions`: `ServiceUnavailable`, `InternalServerError`, `GatewayTimeout`, `TooManyRequests`, `DeadlineExceeded`;
  `google.genai.errors.ServerError`; builtins `ConnectionError`, `TimeoutError`.
- `trend_trawler` (uses BigQuery + GCS, no direct genai):
  same as above **minus** `google.genai.errors.ServerError`.

`max_attempts=3` (original + 2 retries) for both.

## Tool → agent ownership (where retry_config goes)

| Infra tool | Owning agent | File |
|---|---|---|
| `get_daily_gtrends` | `gather_trends_agent` | `trend_trawler/agent.py` |
| `write_trends_to_bq`, `save_session_state_to_gcs` | `trend_trawler` (root) | `trend_trawler/agent.py` |
| `generate_image` | `visual_generator` | `creative_agent/agent.py` |
| `save_draft_report_artifact`, `save_eval_report_to_gcs`, `save_creative_gallery_html`, `write_trends_to_bq` | `root_agent` | `creative_agent/agent.py` |

## Except blocks to change (log + `raise`, keep the log line)

| File | Function | Current failure return | Action |
|---|---|---|---|
| `trend_trawler/tools.py` | `get_daily_gtrends` | `return str(e)` | `raise` |
| `trend_trawler/tools.py` | `write_trends_to_bq` | `return {"status":"error",...}` | `raise` |
| `creative_agent/tools.py` | `generate_image` | `return {"status":"error",...}` (also missing `f` prefix bug) | `raise` |
| `creative_agent/tools.py` | `save_draft_report_artifact` | `return {"status":"failed",...}` | `raise` |
| `creative_agent/tools.py` | `save_creative_gallery_html` (the `:1026` block) | `return {"status":"failed",...}` | `raise` |
| `creative_agent/tools.py` | `save_eval_report_to_gcs` | `return {"status":"error",...}` | `raise` |
| `creative_agent/tools.py` | `write_trends_to_bq` | `return {"status":"error",...}` | `raise` |
| `creative_agent/tools.py` | `_upload_blob_to_gcs` | `return {"status":"error",...}` (type bug: returns dict where str expected) | `raise` |
| `creative_agent/tools.py` | `_get_high_res_img` fallback (`:781`) | falls back to standard-res | **KEEP AS-IS** |

> Verify function/line locations at implementation time with `grep -n "except Exception" <file>` — line numbers below are indicative and may drift.

---

### Task 1: Define `INFRA_RETRY` for trend_trawler

**Files:**
- Modify: `trend_trawler/config.py` (add constant + imports)
- Test: `tests/test_retry_config.py` (create)

**Step 1: Write the failing test**

```python
# tests/test_retry_config.py
"""Tests for the scoped RetryConfig constants attached to infra-calling agents."""


class TestTrendTrawlerRetryConfig:
    def test_infra_retry_scoped_and_bounded(self):
        from trend_trawler.config import INFRA_RETRY

        assert INFRA_RETRY.max_attempts == 3
        # exceptions are stored as class-name strings by RetryConfig's validator
        names = set(INFRA_RETRY.exceptions)
        assert {
            "ServiceUnavailable",
            "InternalServerError",
            "GatewayTimeout",
            "TooManyRequests",
            "DeadlineExceeded",
            "ConnectionError",
            "TimeoutError",
        } <= names
        # trend_trawler has no direct genai calls
        assert "ServerError" not in names
```

**Step 2: Run test to verify it fails**

Run: `export PATH="$HOME/.local/bin:$PATH" && uv run pytest tests/test_retry_config.py::TestTrendTrawlerRetryConfig -v`
Expected: FAIL with `ImportError: cannot import name 'INFRA_RETRY'`.

**Step 3: Write minimal implementation**

Add to the top of `trend_trawler/config.py` (after existing imports):

```python
from google.adk.workflow import RetryConfig
from google.api_core import exceptions as api_exceptions

# ADK 2.0 retries a node when the raised exception's EXACT class name is in this
# list (it does NOT use isinstance), so enumerate the concrete transient classes
# Google clients actually raise — base classes like GoogleAPICallError never match.
INFRA_RETRY = RetryConfig(
    max_attempts=3,
    exceptions=[
        api_exceptions.ServiceUnavailable,   # 503
        api_exceptions.InternalServerError,  # 500
        api_exceptions.GatewayTimeout,       # 504
        api_exceptions.TooManyRequests,      # 429
        api_exceptions.DeadlineExceeded,
        ConnectionError,
        TimeoutError,
    ],
)
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_retry_config.py::TestTrendTrawlerRetryConfig -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add trend_trawler/config.py tests/test_retry_config.py
git commit -m "feat(trend_trawler): add scoped INFRA_RETRY config"
```

---

### Task 2: Define `INFRA_RETRY` for creative_agent

**Files:**
- Modify: `creative_agent/config.py`
- Test: `tests/test_retry_config.py` (extend)

**Step 1: Write the failing test**

Append to `tests/test_retry_config.py`:

```python
class TestCreativeAgentRetryConfig:
    def test_infra_retry_includes_genai_server_error(self):
        from creative_agent.config import INFRA_RETRY

        assert INFRA_RETRY.max_attempts == 3
        names = set(INFRA_RETRY.exceptions)
        assert "ServerError" in names           # genai 5xx
        assert "ServiceUnavailable" in names     # api_core
        assert "TimeoutError" in names
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_retry_config.py::TestCreativeAgentRetryConfig -v`
Expected: FAIL with `ImportError`.

**Step 3: Write minimal implementation**

Add to the top of `creative_agent/config.py` (after existing imports):

```python
from google.adk.workflow import RetryConfig
from google.api_core import exceptions as api_exceptions
from google.genai import errors as genai_errors

# See note in trend_trawler/config.py: ADK matches retry exceptions by exact class
# name, so we list the concrete transient classes (genai 5xx + Google API 5xx/429 +
# transport). creative_agent calls genai directly (image gen), hence ServerError.
INFRA_RETRY = RetryConfig(
    max_attempts=3,
    exceptions=[
        genai_errors.ServerError,            # genai 5xx
        api_exceptions.ServiceUnavailable,   # 503
        api_exceptions.InternalServerError,  # 500
        api_exceptions.GatewayTimeout,       # 504
        api_exceptions.TooManyRequests,      # 429
        api_exceptions.DeadlineExceeded,
        ConnectionError,
        TimeoutError,
    ],
)
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_retry_config.py::TestCreativeAgentRetryConfig -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add creative_agent/config.py tests/test_retry_config.py
git commit -m "feat(creative_agent): add scoped INFRA_RETRY config"
```

---

### Task 3: Make trend_trawler infra tools propagate (TDD)

**Files:**
- Modify: `trend_trawler/tools.py` (`get_daily_gtrends`, `write_trends_to_bq`)
- Test: `tests/test_tools_retry.py` (create)

**Step 1: Write the failing test**

```python
# tests/test_tools_retry.py
"""Infra tools must propagate exceptions (not swallow into status dicts) so ADK
2.0's RetryConfig can retry transient failures."""

import pytest
from google.api_core import exceptions as api_exceptions


class MockState(dict):
    pass


class MockToolContext:
    def __init__(self):
        self.state = MockState()
        self.state["gcs_folder"] = "f"
        self.state["agent_output_dir"] = "d"
        self.state["target_search_trends"] = {"target_search_trends": ["t1"]}


class TestTrendTrawlerToolsPropagate:
    def test_write_trends_to_bq_raises_on_transient(self, monkeypatch):
        from trend_trawler import tools

        def boom():
            raise api_exceptions.ServiceUnavailable("503")

        # _get_gtrends_max_date runs before the try; stub it so we reach the client call
        monkeypatch.setattr(tools, "_get_gtrends_max_date", lambda: "2026-07-01")
        monkeypatch.setattr(tools, "_get_bigquery_client", boom)

        with pytest.raises(api_exceptions.ServiceUnavailable):
            tools.write_trends_to_bq(MockToolContext())

    def test_get_daily_gtrends_raises_on_transient(self, monkeypatch):
        from trend_trawler import tools

        def boom(*a, **k):
            raise api_exceptions.InternalServerError("500")

        monkeypatch.setattr(tools, "_get_bigquery_client", boom)

        with pytest.raises(api_exceptions.InternalServerError):
            tools.get_daily_gtrends(MockToolContext())
```

> NOTE at implementation time: open `trend_trawler/tools.py` and confirm where the
> client is acquired relative to the `try`. If `_get_bigquery_client()` is called
> *before* the `try`, the raise happens naturally (good). If it is called *inside*
> the `try`, the current code would still swallow it — that is exactly the block we
> are fixing in Step 3, so the test will pass once the block is converted.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tools_retry.py::TestTrendTrawlerToolsPropagate -v`
Expected: FAIL — current code catches the exception and returns `str(e)` / an error dict, so `pytest.raises` sees no exception.

**Step 3: Convert the except blocks**

In `trend_trawler/tools.py`, `get_daily_gtrends` — replace:

```python
    except Exception as e:
        logging.exception(f"Failed to gather daily trends: {e}")
        return str(e)
```
with:
```python
    except Exception as e:
        # Let transient failures propagate so ADK 2.0 RetryConfig can retry.
        logging.exception(f"Failed to gather daily trends: {e}")
        raise
```

In `write_trends_to_bq` — replace:

```python
    except Exception as e:
        logging.exception(f"Failed to insert rows to bq: {e}")
        return {
            "status": "error",
            "error_message": f"Error inserting rows to bq: {e}",
        }
```
with:
```python
    except Exception as e:
        logging.exception(f"Failed to insert rows to bq: {e}")
        raise
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tools_retry.py::TestTrendTrawlerToolsPropagate -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add trend_trawler/tools.py tests/test_tools_retry.py
git commit -m "fix(trend_trawler): propagate transient errors from infra tools"
```

---

### Task 4: Attach retry_config to trend_trawler agents

**Files:**
- Modify: `trend_trawler/agent.py` (`gather_trends_agent` ~line 30, `trend_trawler` root ~line 168)
- Test: `tests/test_retry_config.py` (extend)

**Step 1: Write the failing test**

Append to `tests/test_retry_config.py`:

```python
class TestAgentsHaveRetryConfig:
    def test_trend_trawler_agents_have_retry(self):
        from trend_trawler.agent import gather_trends_agent, trend_trawler
        from trend_trawler.config import INFRA_RETRY

        assert gather_trends_agent.retry_config is INFRA_RETRY
        assert trend_trawler.retry_config is INFRA_RETRY
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_retry_config.py::TestAgentsHaveRetryConfig::test_trend_trawler_agents_have_retry -v`
Expected: FAIL — `retry_config` is `None`.

**Step 3: Implement**

In `trend_trawler/agent.py`, ensure `INFRA_RETRY` is imported:
```python
from .config import config, INFRA_RETRY
```
Add `retry_config=INFRA_RETRY,` to the `gather_trends_agent = Agent(...)` constructor and the `trend_trawler = Agent(...)` root constructor (any position among the kwargs).

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_retry_config.py::TestAgentsHaveRetryConfig::test_trend_trawler_agents_have_retry -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add trend_trawler/agent.py tests/test_retry_config.py
git commit -m "feat(trend_trawler): attach INFRA_RETRY to infra-calling agents"
```

---

### Task 5: Make creative_agent infra tools propagate + fix bugs (TDD)

**Files:**
- Modify: `creative_agent/tools.py` (`generate_image`, `save_draft_report_artifact`, `save_creative_gallery_html`, `save_eval_report_to_gcs`, `write_trends_to_bq`, `_upload_blob_to_gcs`)
- Test: `tests/test_tools_retry.py` (extend)

**Step 1: Write the failing test**

Append to `tests/test_tools_retry.py`:

```python
class TestCreativeAgentToolsPropagate:
    def test_write_trends_to_bq_raises_on_transient(self, monkeypatch):
        from creative_agent import tools

        def boom():
            raise api_exceptions.TooManyRequests("429")

        monkeypatch.setattr(tools, "_get_bigquery_client", boom)
        with pytest.raises(api_exceptions.TooManyRequests):
            tools.write_trends_to_bq(MockToolContext())

    def test_upload_blob_to_gcs_raises_on_transient(self, monkeypatch):
        from creative_agent import tools

        class FakeBlob:
            def upload_from_string(self, *a, **k):
                raise api_exceptions.ServiceUnavailable("503")

        class FakeBucket:
            def blob(self, *a, **k):
                return FakeBlob()

        class FakeClient:
            def bucket(self, *a, **k):
                return FakeBucket()

        monkeypatch.setattr(tools, "_get_gcs_client", lambda: FakeClient())
        with pytest.raises(api_exceptions.ServiceUnavailable):
            # signature: verify args at implementation time
            tools._upload_blob_to_gcs(
                image_bytes=b"x", filename="a.png", gcs_folder="f", gcs_subdir="d"
            )
```

> At implementation time, confirm `_upload_blob_to_gcs`'s exact signature
> (`creative_agent/tools.py:1264`) and adjust the call kwargs accordingly.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tools_retry.py::TestCreativeAgentToolsPropagate -v`
Expected: FAIL — tools currently return error dicts.

**Step 3: Convert every listed creative_agent except block**

For each function in the "Except blocks to change" table (except `_get_high_res_img`), replace the `return {"status": ...}` inside the `except Exception as e:` with `raise`, keeping the `logging.exception(...)` line and adding the comment `# Propagate so ADK 2.0 RetryConfig can retry transient infra failures.`

Also delete the now-dead incidental bug at `generate_image` (`return {"error_message": "No images generated. {e}"}` had a missing `f` prefix — it disappears with the conversion).

Do NOT touch `_get_high_res_img` (`:781`) — that catch is a deliberate high-res→standard-res fallback.

**Step 4: Run tests**

Run: `uv run pytest tests/test_tools_retry.py -v`
Expected: PASS (both trend_trawler and creative_agent classes).

**Step 5: Commit**

```bash
git add creative_agent/tools.py tests/test_tools_retry.py
git commit -m "fix(creative_agent): propagate transient errors from infra tools"
```

---

### Task 6: Attach retry_config to creative_agent agents

**Files:**
- Modify: `creative_agent/agent.py` (`visual_generator` ~line 739, `root_agent` ~line 800)
- Test: `tests/test_retry_config.py` (extend)

**Step 1: Write the failing test**

Append to `TestAgentsHaveRetryConfig`:

```python
    def test_creative_agent_agents_have_retry(self):
        from creative_agent.agent import visual_generator, root_agent
        from creative_agent.config import INFRA_RETRY

        assert visual_generator.retry_config is INFRA_RETRY
        assert root_agent.retry_config is INFRA_RETRY
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_retry_config.py::TestAgentsHaveRetryConfig::test_creative_agent_agents_have_retry -v`
Expected: FAIL.

**Step 3: Implement**

In `creative_agent/agent.py`, import `INFRA_RETRY` from `.config` and add `retry_config=INFRA_RETRY,` to the `visual_generator` and `root_agent` constructors.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_retry_config.py -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add creative_agent/agent.py tests/test_retry_config.py
git commit -m "feat(creative_agent): attach INFRA_RETRY to infra-calling agents"
```

---

### Task 7: Handle interactive_creative

**Files:**
- Inspect: `interactive_creative/agent.py`
- Modify: same (only if it defines its own infra-calling agents rather than importing creative_agent's)

**Step 1: Investigate**

Run: `grep -n "generate_image\|write_trends_to_bq\|save_.*gcs\|save_.*artifact\|from creative_agent\|retry_config\|Agent(" interactive_creative/agent.py`

Decision:
- If `interactive_creative` **imports the same agent objects** from `creative_agent`, they already carry `retry_config` — no change; add a comment noting the inheritance.
- If it **defines its own** `visual_generator`/root that call infra tools, attach `retry_config=INFRA_RETRY` (import from `creative_agent.config`) the same way, and add a test mirroring Task 6.

**Step 2: (If change needed) write test, run-fail, implement, run-pass** — same pattern as Task 6.

**Step 3: Commit (only if changed)**

```bash
git add interactive_creative/agent.py tests/test_retry_config.py
git commit -m "feat(interactive_creative): attach INFRA_RETRY to infra-calling agents"
```

---

### Task 8: Full validation + open PR

**Step 1: Lint + format**

Run:
```bash
export PATH="$HOME/.local/bin:$PATH"
uvx ruff check .
uvx ruff format --check .
```
Expected: `All checks passed!` and `N files already formatted`. If format flags files, run `uvx ruff format .` and re-commit.

**Step 2: Full test suite**

Run: `uv run pytest tests/ -q`
Expected: all tests pass (130 existing + the new retry/propagation tests).

**Step 3: Import smoke under adk 2.4**

Run:
```bash
for m in trend_trawler.agent creative_agent.agent interactive_creative.agent; do
  uv run python -c "import $m" && echo "OK $m"
done
```
Expected: `OK` for each.

**Step 4: Push + open PR**

```bash
git push -u origin chore/adk2-retry-adoption
gh pr create --base main --head chore/adk2-retry-adoption \
  --title "feat: adopt ADK 2.0 RetryConfig for transient infra failures" \
  --body "<summary: scoped RetryConfig(max_attempts=3) on infra-calling agents; infra tools now log+raise instead of swallowing; kept the high-res fallback; fixed _upload_blob_to_gcs return-type bug and generate_image f-string bug. NEEDS LIVE adk-eval before merge — behavior change (tools raise) only fully exercised against real APIs.>"
```

**Step 5: Update memory**

Mark `adk2-retry-adoption-followup.md` and `adk-pipe-work-status.md` as "PR open" and record the live-eval gate.

---

## Live-validation gate (cannot be done in-session — needs GCP creds)

Before merging, run against real APIs and confirm: (a) a forced transient error on an infra tool triggers a retry (check logs for repeated attempts with backoff), (b) the agent flow still completes end-to-end, (c) HITL pause/resume in `interactive_creative` is unaffected.

```bash
uv run adk eval creative_agent tests/eval/evalsets/creative_agent_evalset.json \
  --config_file_path=tests/eval/eval_config.json --print_detailed_results
```

## Risks / notes

- **Behavior change:** tools that returned friendly error dicts now raise. If ADK does not retry (e.g. a non-transient error) the invocation surfaces the error instead of the agent continuing with a status dict. This is intended, but is the reason for the live gate.
- **Exact-name matching is brittle:** if a new transient class name appears (SDK change), it won't be retried until added to `INFRA_RETRY.exceptions`. Documented in the config comment.
- **DRY vs per-package:** `INFRA_RETRY` is defined twice (once per package) intentionally — the two packages share no module and the lists differ (genai `ServerError`). Do not create a shared module just for this (YAGNI).
