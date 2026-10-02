# README Review: Findings and Improvement Plan

> On approval, the first commit copies this file to `docs/plans/2026-10-02-readme-overhaul.md`.

## Context
The user asked for a thorough README review, including whether "Example Outputs" should move to its own folder, with no fixes applied yet. Two read-only audits (content accuracy, and links/assets) found:
- the README (601 lines) is accurate on architecture and has **no broken links**;
- it is heavy (about **70 MB of embedded media**, including two 20–21 MB GIFs from Oct 2025 that no longer match the current HTML gallery and PDF);
- several facts are wrong or stale, the repo tree is about 20% of the file and out of date, and much of the content duplicates CLAUDE.md and `deployment/README.md`;
- a first-time visitor gets no prerequisites, and the best demo (the annotated user-journey GIF) is buried at line 360.

**User decisions (2026-10-02):**
- (1) **Move Example Outputs to `docs/examples/` and refresh the media** from a current real run.
- (2) **Lean front-page restructure.**
- (3) **Rewrite `frontend/README.md`**, which is still create-next-app boilerplate.

**Constraint (user, 2026-10-02):** don't delete any of the example outputs the README displays. They may only be moved to the new location, and must keep being displayed there.

**Outcome:** a scannable front page (what it is, a demo, prerequisites, a quickstart, links). Detail lives where it is owned: CLAUDE.md for internals, `deployment/README.md` for operations, `docs/examples/` for sample outputs. The README carries much less media weight.

## Findings (prioritized; README line numbers)
**Wrong**
- L227–230: the sample campaign doesn't match `.env.example` (extra "and love surreal memes"; "SE CE24" instead of "PRS SE CE24").
- L285: `target_search_trend` should be the state key `target_search_trends`.
- L309: the eval report is described as saved only to GCS. It also writes a BigQuery `creative_evals` row (with `weakest_dimension_labels`).
- L408: promises "the full component tree" in `frontend/README.md`, which is boilerplate (Geist, "Deploy on Vercel").
- L477: the tree says `callbacks.py` handles rate limiting and citations. Those live in `agent_common/rate_limit.py` and `creative_agent/citations.py`.
- L540–545: the repo tree is malformed (the `creative_fanout/` branch is never closed).
- Typos: "YOU KEY SELLING POINT(S)" (L273/L284), "`creative agent`" (L278), "can hyper-focused" (L251), "agents final step" (L321), broken markup "~*live editor tool*" (L601).

**Stale**
- Example Outputs (L312–354):
  - The media is from 2025-10-01. The current gallery (`creative_agent/gallery_template.py`, `creative_agent/tools.py`) has separate Visual concepts / Ad copy sections, a lightbox, a degradation banner, and 4 hover facts (the README lists 3).
  - The two GIFs are 21 MB and 20 MB, and have no alt text.
- `imgs/macho_man_prs.gif` (17 MB) is orphaned: nothing references it.
- The env block (L117–172) is a partial copy of `.env.example`. It is missing `MODEL_REQUEST_TIMEOUT_SECONDS`, `CRITIC_FALLBACK_MODEL`, `CAMPAIGN_RESEARCH_PLACEMENT`, `MODEL_ARMOR_*`, OTEL, the CRF knobs and others.
- Evaluation section: missing the `EVAL_MODEL` / `EVAL_MODEL_LOCATION` overrides, `judge_model` and the readable labels.
- Testing (L462): lists 3 CI workflows. It is missing `adk-eval.yml` (nightly eval + efficiency gate) and `dependabot.yml`.
- Repo tree: missing `creative_agent/citations.py`, `creative_eval/dimensions.py`, `agent_common/safety.py`, `runserver/otel.py`, `deployment/backfill_eval_dimension_labels.py`, `tests/eval/`, `frontend/scripts/`, `frontend/Dockerfile`, `docs/plans/archive`, the workflows, and more.
- TODO (L593–601): four completed items are still listed (struck through).
- External links:
  - the ADK docs URL is duplicated (L64, L89);
  - the Vertex prompt-guide path is old;
  - the "Vertex AI · Agent Engine" badge and the copy (L16, L69, L417) predate the Agent Platform rename that L9 mentions.

**Structure, duplication, style**
- The first thing a reader sees is the rebrand note (L9), not a demo. The user-journey GIF sits at L360.
- There are no prerequisites (Python ≥3.13, uv, Node ≥22.13, gcloud, APIs to enable, `global`-only Gemini 3.x). Nothing says which path to try first (`adk web` vs the full stack) or that BigQuery tables must exist before agents write.
- The BigQuery `bq mk` DDL (L188–209) lives only in the README, while the migrations live in `deployment/README.md` (240–280), and `deployment/README.md:20` points back to the README.
- Content duplicated from CLAUDE.md:
  - the pipeline summary (said three times: L24–44, L66–67, and again in CLAUDE.md);
  - the design system, the authz summary, the local-dev command, and the CI list.
- Mixed tone: all-lowercase headings and prose (L32, L118, L189, L234, L247–251, L267–290, L314–350) vs sentence case elsewhere. "PubSub" vs "Pub/Sub". Emoji headings.
- "Helpful references" sits in the middle of Architecture.
- No license or contributing pointer (CODE_STANDARDS.md exists).

## Recommended plan (2 PRs, built in parallel, each with one implementer)

### PR 1: `docs/readme-overhaul` (README + docs/examples + deployment docs)
1. **New `docs/examples/README.md`**, built from fresh media captured from today's real run (session `1638190721007616000`, GCS folder `gs://trend-trawler-deploy-ae/2026_10_02_16_07_b133/creative_output/`, the Paul Reed Smith (PRS) interactive run with a 100% pass rate; read the brief from its saved session state JSON):
   - **HTML gallery:**
     - download `creative_portfolio_gallery.html` and the images to a temp dir (rewrite image URLs to local paths if they point at GCS);
     - render with the existing Playwright setup (`frontend/node_modules`, Chromium; this machine needs `LD_LIBRARY_PATH=/tmp/xkb/...` per `docs/screenshots/README.md`);
     - capture: the gallery overview, a hovered creative showing the 4 facts, and the lightbox.
   - **Research PDF:** render 2–3 pages to PNG with `uv run --no-project --with pypdfium2` (the cover / summary page, a page with citations).
   - **Eval report:** a short JSON excerpt in a code block (summary + one creative's verdicts) plus a link to the full `frontend/scripts/screenshot-fixtures/creative-eval-report.json` sample.
   - **Budget:** each *new* image ≤ ~600 KB (1200 px wide, optimized PNG or JPEG via Pillow). The moved legacy files keep their original bytes; no re-encoding or deletion. Alt text on every image. Sentence-case prose describing what each output is and where it's saved (GCS paths, the BigQuery tables).
   - Include a script `docs/examples/capture_examples.md` (or a `.py`) documenting how the media was produced, so it can be refreshed.
2. **Move, don't delete, the existing example outputs** (user requirement, 2026-10-02). `git mv` all four files the README currently displays into `docs/examples/`:
   - `tt_prs_research_overview_p050_15fps.gif`
   - `tt_prs_html_overview_p050_15fps.gif`
   - `gallery_sample_prs.png`
   - `its_complicated.png`

   `docs/examples/README.md` keeps displaying all of them, with their original captions and the "details on the HTML report" content, reworded to sentence case. They go in an "Earlier outputs (Oct 2025)" section alongside the fresh captures, which are added, not substituted.

   Also `git mv imgs/macho_man_prs.gif` into `docs/examples/`, and show it there too (it was never displayed, so it gets a short caption). No media file is deleted. `imgs/trend_trawler_banner.png` stays where it is.

   The size win comes from the main README no longer embedding the two 20 MB GIFs (only a small teaser image plus a link). The files themselves remain in the repo.
3. **Restructure README.md** (target about 250–300 lines). Order:
   - **Banner and one-paragraph pitch.** Badges with the Agent Platform naming and no hard-coded minor versions, or keep them but update.
   - **Demo:** the annotated `docs/screenshots/user-journey.gif` above the fold, with a one-line caption.
   - **What it does:** a 3-step pipeline summary (scout → creative → evaluate). Said once. Link the architecture diagrams.
   - **Example outputs:** one teaser image (the new gallery overview) plus a link to `docs/examples/`.
   - **Prerequisites:** Python ≥3.13, uv, Node ≥22.13 (UI only), gcloud auth, the GCP APIs, and a `global` Gemini 3.x note.
   - **Quickstart:**
     - clone, then `uv sync`;
     - `cp .env.example .env` and set the 4 required vars (link `.env.example` as the full reference; no copied block);
     - BigQuery: one command or a link (see step 4);
     - `uv run adk web .`, with a note that the trend scout needs BigQuery to persist.
   - **Usage:** fixed sample brief, agent choices including the trend-scout trend pick and the interactive checkpoints; typos fixed.
   - **Frontend UI:** keep the GIF/gallery from #215/#216, but put the GIF up top in Demo and keep the gallery here. Local-run commands.
   - **Evaluation:** short and corrected (GCS + BigQuery row, readable labels, judge override env vars).
   - **Deployment:** 3–4 lines plus a link to `deployment/README.md`. All three agents, or `--list`. The Agent Platform naming note moves here from L9.
   - **Testing:** commands plus all 4 CI workflows (link CLAUDE.md for detail; no test counts).
   - **Repo structure:** top-level directories only, with one-line roles (about 20 lines). Per-file detail stays in CLAUDE.md "Key Files".
   - **Contributing:** a link to CODE_STANDARDS.md plus "uv/ruff/ty/pytest; PRs".
   - **Roadmap:** only the open TODOs.
   - **References:** the helpful links (deduplicated, current URLs).

   Sentence case throughout, consistent "Pub/Sub", no emoji headings, Table of Contents regenerated.
4. **BigQuery DDL moves to `deployment/README.md`** as a "Create BigQuery tables" section next to the migrations, with the same `bq mk` commands verified against `creative_agent/bq_tools.py` `EVAL_COLUMN_TYPES` (including `weakest_dimension_labels`), `trend_scout/tools.py`, and `cloud_functions/creative_fanout/main.py`. Also update `deployment/README.md:20`, which currently points back to the README. Optional and small: `deployment/create_bq_tables.sh` wrapping the same commands (idempotent `bq mk || true`), referenced from both READMEs.
5. **Cross-links:** `docs/screenshots/README.md` and `docs/diagrams/README.md` stay. Check every relative link and anchor after the restructure.

### PR 2: `docs/frontend-readme` (`frontend/README.md` rewrite, independent)
Replace the boilerplate with a concise guide:
- **What it is:** Next.js 16 App Router, Tailwind 4, shadcn/base-ui, the proof-room design note (link the plan).
- **Pages:** `/`, `/runs`, `/run/[id]`, `/results/[id]`.
- **Key modules:** `lib/api.ts`, `run-history.ts`, `run-stages.ts`, `run-completion.ts`, `eval-matching.ts`, `research-report.ts`, `agents.ts`, `pause-detection.ts`; the `/api/adk` proxy and `/api/gcs`.
- **Local dev:** backend command (link the root README), `npm run dev`.
- **Tests:** `npm test`, lint, build.
- **Screenshots and GIF:** `npm run screenshots`, `JOURNEY=1`, `build-journey-gif.py`, the libxkbcommon/emoji note.
- **Deployment:** a link to the `deployment/README` Cloud Run section.

Remove the "Deploy on Vercel" and Geist text and the stale tree.

## Conventions
- Branch + PR each. Squash-merge after CI (PR 2 touches `frontend/**`, so frontend CI runs).
- **Never add `Co-Authored-By` or any AI attribution.** Don't stage the untracked repo-root `.agents/`, `.claude/`, `skills-lock.json`.
- Docs-only. No app code changes. Downloading run artifacts from GCS is read-only. Temp files go in /tmp.
- Keep facts sourced from code (`.env.example`, `bq_tools.py`, CLAUDE.md). Don't invent numbers (no test counts).

## Verification
- **Link check** (a script in the PR description or run ad hoc): every relative `](path)` / `src="path"` in README.md, `docs/examples/README.md`, `deployment/README.md` and `frontend/README.md` exists; every `#anchor` matches a heading (GitHub slug rules). `git grep` for references to the old `imgs/` paths of the moved files returns nothing (all references updated to `docs/examples/`).
- **Nothing deleted:** `git diff --stat main` shows the five example media files as renames (`R100`), not deletions; `git diff --diff-filter=D --name-only main` lists no media files.
- **Size:** `du -h` of the media embedded by README.md itself, about ≤ 15 MB in total (was about 70 MB, mostly the two GIFs now shown on `docs/examples/`). New example images ≤ 600 KB each.
- **Render:** view the new example PNGs with the Read tool. Spot-check the README rendering with `gh` markdown preview, or view the PR's rendered README on GitHub.
- **Facts:**
  - the sample brief matches `.env.example`;
  - the DDL matches `EVAL_COLUMN_TYPES`;
  - the CI list matches `.github/workflows/*`;
  - the repo tree top-level entries match `git ls-files` top-level directories.
- **CI:** frontend tests stay green on PR 2.
