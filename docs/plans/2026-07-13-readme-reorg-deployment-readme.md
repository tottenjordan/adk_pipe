# README Reorg (Thin Landing Page) + `deployment/README.md` Consolidation — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task.
> This is a documentation-only change (no code, no tests to write) — a single-writer, tightly-coupled
> edit (main README and `deployment/README.md` must stay consistent), so `executing-plans` /
> straight execution fits better than `subagent-driven-development`.

**Goal:** Restructure the 926-line `README.md` into a concise **thin landing page** (Option A) and move
all step-heavy deployment instructions into a new **`deployment/README.md`**, keeping the two
architecture diagrams and a ~40-line deployment summary in the main README with a pointer to the detail.

**Architecture:** Pure content move + reorganization. The ~410-line Deployment block (current
`README.md:361–773`) becomes `deployment/README.md`; the main README keeps a summary + both diagrams +
a link. The rest of the README is reordered quickstart-forward, with **Evaluation** and **Example
Outputs** promoted to their own top-level sections (per the user), `<details>` collapsibles retained,
and Frontend/Testing reduced to short blurbs that link to `frontend/README.md` and the Testing detail.
Plus three folded-in cleanups: ADK badge `1.31→2.4`, delete stray `docs/architecture/run_*` scratch
dirs, sync `.env.example` CRF var names/comments.

**Tech Stack:** Markdown only. No `uv`/pytest/ruff runs needed except a final link/reference sanity grep.

**Standing constraints:** never add `Co-Authored-By` trailers; commit per-task; **only push/PR when the
user asks**; do NOT stage `uv.lock`/`.python-version`. `export PATH="$HOME/.local/bin:$PATH"` only if a
tool is invoked (none required here).

---

## Context

**Why:** The README is 926 lines and the Deployment section (`README.md:361–773`, ~410 lines) dominates
it — a wall of `gcloud`/`bq` commands that buries the "what is this / how do I run it" story a landing
page should lead with. The user chose **Option A (thin landing page)**: main README = hero →
architecture (diagrams) → quickstart → usage → eval → example outputs → frontend blurb → deployment
summary → testing blurb → repo structure → TODO; all deployment depth lives in `deployment/README.md`.

**Decided with the user (this session):**
- Structure: **Option A — thin landing page.**
- Add dedicated **Evaluation** and **Example Outputs** sections (don't fold them under Usage).
- Keep using `<details>` collapsibles where they aid scanning.
- Fold in all three cleanups: ADK badge, stray `run_*` dirs, `.env` var sync.

**Verified facts:**
- ADK pin: `google-adk[eval]>=2.4.0,<3.0.0`; deployed `2.4.0` → badge should read **2.4**
  (`README.md:13` currently says `1.31`).
- `docs/architecture/run_*` dirs are **already gitignored** (`.gitignore:20: docs/architecture/run_*/`)
  and **not git-tracked** → removing them is a plain on-disk `rm -rf`, **no git/gitignore change**.
- Diagrams present: `docs/architecture/agent-engine-pipeline.png` (already used in README),
  `docs/architecture/crf-fanout-orchestration.png` (exists but currently **unreferenced** — surface it
  in the deployment summary / `deployment/README.md`).
- `deployment/README.md` does not exist yet. `frontend/README.md` already carries the full frontend
  tree — main README should link to it, not duplicate.
- Working tree clean at plan time.

**Out of scope:** No changes to `CLAUDE.md`, deploy scripts, or any code. Deployed service names and env
vars are unchanged. Historical `docs/plans/*` untouched.

---

## Task 1: Create `deployment/README.md` (move the deployment detail)

**Files:**
- Create: `deployment/README.md`
- Source of content (to move, near-verbatim): `README.md:361–773`

**Step 1:** Write `deployment/README.md` with this structure, porting the existing command blocks
verbatim (they're correct — the CRF paths already say `cloud_functions/creative_fanout`):

```markdown
# Deployment

Operational guide for deploying Trend Trawler. For what the system does and how to run it locally,
see the [main README](../README.md).

## Contents
- [Prerequisites](#prerequisites)
- [Deploying Agents to Agent Engine](#deploying-agents-to-agent-engine)
- [Cloud Run Functions Fan-out Pattern](#cloud-run-functions-fan-out-pattern)
- [Alternative: Deploy to Cloud Run](#alternative-deploy-to-cloud-run)

## Prerequisites
- Populated `.env` (see [.env.example](../.env.example)) — project, `GOOGLE_CLOUD_LOCATION=global`,
  `GCP_REGION=us-central1`, GCS bucket, PubSub topics, CRF names, BigQuery IDs.
- `gcloud` authenticated; BigQuery dataset + tables created (see main README → Installation).

## Deploying Agents to Agent Engine
<port README.md:363–446 verbatim: deploy/list/delete commands, AGENT_EXTRA_PACKAGES note,
 "Test deployment" (test_deployment.py) subsection, notebook WIP note, "View logs" query>

## Cloud Run Functions Fan-out Pattern
<port README.md:449–691 verbatim: objectives, "Why two deployments?" <details>, steps 1–5
 (IAM grants, PubSub topics, functions + eventarc triggers, confirm, invoke incl. SQL insert +
 message.json + the concurrency/quota rationale comments). Embed
 ../docs/architecture/crf-fanout-orchestration.png near the top of this section.>

## Alternative: Deploy to Cloud Run
<port README.md:694–773 verbatim: adk deploy cloud_run blocks for both agents>
```

- Fix relative paths for the new location: image `src` and file links that were repo-root-relative
  become `../`-prefixed (e.g. `docs/architecture/...` → `../docs/architecture/...`,
  `cloud_functions/creative_fanout/message.json` → `../cloud_functions/creative_fanout/message.json`).
  Intra-`deployment/` references (`deployment/test_deployment.py`) can stay as-is or become bare
  `test_deployment.py` — keep repo-root style (`deployment/test_deployment.py`) for copy-paste safety.

**Step 2:** Commit: `docs(deployment): add deployment/README.md with full deploy instructions`.

---

## Task 2: Rewrite `README.md` as the thin landing page

**Files:** Modify `README.md` (replace `361–773` with a summary; reorder + trim the rest; badge fix).

**Target section order (Option A):**
1. Hero (banner, title, tagline, badges) + stage table + "casting a wide net" `<details>` — **keep**.
   - **Cleanup:** badge line 13 `Google%20ADK-1.31` → `Google%20ADK-2.4`.
2. **Table of Contents** — rebuild to match the new order below.
3. **Architecture** — short prose on the two-phase pipeline; embed `agent-engine-pipeline.png` here
   (moved up from Deployment). Keep the agent-composition summary tight.
4. **Quickstart** — condense current Installation (`README.md:63–192`): clone → auth → `.env`
   (keep the `<details>` env dump) → `uv sync` → BigQuery `bq mk` blocks. Aim ~6 numbered steps.
5. **Usage** — campaign metadata (keep the "guidance" `<details>`) + Running an Agent (local
   `adk web`, the 3 agent choices).
6. **Evaluation** — promote current "Creative Evaluation" (`README.md:276–284`) to its own `##`
   section: the 12 dimensions, 0.7 threshold, `global` judge note.
7. **Example Outputs** — promote current "Example Output" (`README.md:286–326`) to its own `##`
   section: the two GIFs + the HTML-report `<details>`.
8. **Frontend UI** — reduce to a short blurb (what it is, `npm run dev`) + link to
   [frontend/README.md](frontend/README.md) for the full component tree/details.
9. **Deployment** — NEW ~40-line summary (see block below): the two-layer table, both diagrams, a
   one-line quick-deploy, and a prominent pointer to [deployment/README.md](deployment/README.md).
10. **Testing** — short blurb + the frontend/pytest/eval/integration command groups may stay (they're
    compact) OR trim to essentials + note `CLAUDE.md`/`CODE_STANDARDS.md`. Keep it lean.
11. **Repo Structure** — keep the current tree (`README.md:811–914`); verify it still matches.
12. **TODO** — keep.

**Deployment summary block to insert (replaces `361–773`):**

```markdown
## Deployment

Trend Trawler deploys in two layers:

| Layer | What | Where |
| --- | --- | --- |
| **Agents** | `trend_trawler`, `creative_agent`, `interactive_creative` | Vertex AI Agent Engine (one instance each) |
| **Fan-out** | orchestrator (`crf_entrypoint`) + worker (`agent_worker_entrypoint`) | Cloud Run Functions + Pub/Sub |

<p align="center">
  <img src="docs/architecture/agent-engine-pipeline.png" alt="creative_agent pipeline on Agent Engine" width="640">
</p>
<p align="center">
  <img src="docs/architecture/crf-fanout-orchestration.png" alt="Cloud Run Functions fan-out orchestration" width="640">
</p>

Deploy an agent to Agent Engine:

```bash
python deployment/deploy_agent.py --version=v1 --agent=creative_agent --create
```

**→ Full instructions** — IAM, Pub/Sub topics, eventarc triggers, invoking the fan-out, testing
deployed agents, and the Cloud Run alternative — **live in [deployment/README.md](deployment/README.md).**
```

> Note: if `agent-engine-pipeline.png` is embedded in the **Architecture** section (item 3), don't
> also embed it in the Deployment summary — keep the CRF diagram there and the Agent Engine diagram up
> top, or keep both here. Decide during execution; avoid duplicating the same image twice on the page.

**Step 2:** Update the Table of Contents to the new anchors.

**Step 3:** Commit: `docs(readme): reorganize into thin landing page; link out to deployment/README.md`.

---

## Task 3: Folded cleanups (stray dirs + `.env` sync)

**Files:** delete `docs/architecture/run_*/` (disk only); modify `.env.example` if drift found.

**Step 1 — remove scratch dirs (no git action; already gitignored):**
```bash
rm -rf docs/architecture/run_2026*/
ls docs/architecture/            # expect only: agent-engine-pipeline.png, crf-fanout-orchestration.png, README.md
git status --short docs/architecture/   # expect: no output (dirs were untracked/ignored)
```

**Step 2 — sync `.env.example` deploy vars:** diff the CRF/deploy var names + comments in
`.env.example` (`CREATIVE_CRF_NAME`, `CRF_ENTRYPOINT`, `CREATIVE_WORKER_CRF_NAME`,
`CREATIVE_WORKER_ENTRYPOINT`, topics, `BASE_IMAGE`, `GCP_REGION`, `GOOGLE_CLOUD_LOCATION`) against
what `deployment/README.md` now references. Fix any name/comment drift so the two agree. If already
consistent, no change (note it).

**Step 3:** Commit (only if files changed): `chore(docs): drop scratch run_* dirs; align .env.example deploy vars`.

---

## Task 4: Verification

**No-creds gate:**
```bash
cd /home/user/adk_pipe
# 1. New file exists and main README points at it
test -f deployment/README.md && grep -q "deployment/README.md" README.md && echo "LINK OK"
# 2. No deployment step-detail left behind in main README (should be summary only)
grep -n "gcloud eventarc triggers create\|gcloud pubsub topics create\|adk deploy cloud_run" README.md \
  && echo "WARN: deploy detail still in README" || echo "README clean of deploy detail"
# 3. Badge fixed
grep -q "Google%20ADK-2.4" README.md && echo "BADGE OK"
# 4. Scratch dirs gone
ls docs/architecture/ | grep -q "run_2026" && echo "WARN: run_* remain" || echo "run_* removed"
# 5. No broken repo-relative links in deployment/README.md (../ prefixes resolve)
grep -oE '\]\(\.\./[^)]+\)' deployment/README.md | sed -E 's/.*\((\.\.\/[^)]+)\)/\1/' \
  | while read p; do (cd deployment && test -e "$p" && echo "OK  $p" || echo "MISS $p"); done
```

- Manually skim both rendered files (or `grep '^#'` each) to confirm the heading hierarchy and that no
  section was dropped. Confirm ToC anchors match headings.

**Sequencing & PR:** Task 1 → 2 → 3 → 4, one commit each. Likely a single PR
"docs: thin-landing-page README + deployment/README.md" — **opened only when the user asks.**
Execute with `executing-plans`.
