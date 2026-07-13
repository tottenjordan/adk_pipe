# Agent Architecture Diagrams

Per-agent ADK architecture diagrams generated with the PaperBanana MCP pipeline
(`gemini-3.1-flash-image`), in the style of official Google Cloud documentation.
Each shows the multi-agent tree (Sequential/Parallel composition, `AgentTool`
wrapping) and the per-agent tooling.

| Diagram | Agent | Highlights |
|---|---|---|
| ![trend_scout](trend_scout_architecture.png) | `trend_scout/` | Root orchestrator → 3 `AgentTool` sub-agents (gather → understand → pick) → flat persistence tools; shared session state; BigQuery + GCS sinks |
| ![creative_agent](creative_agent_architecture.png) | `creative_agent/` | Nested `SequentialAgent` research pipeline with a `ParallelAgent` fan-out (trend + campaign branches → merge); ad-copy pipeline; visual pipeline (drafter → critic → finalizer → image gen); LLM-judge; all sinks |
| ![creative_eval](creative_eval_architecture.png) | `creative_eval/` | LLM-as-judge: `evaluate_all_creatives` → `ThreadPoolExecutor` concurrent fan-out to N Gemini judges → 6 ad-copy + 6 visual dims → `EvaluationSummary` → `CreativeEvaluationReport` → GCS JSON + BigQuery `creative_evals` (join on `creative_uuid`) |

## Infrastructure Diagrams

Event-driven orchestration diagrams for the Cloud Run functions + Eventarc
triggers (`cloud_functions/creative_fanout/`) and how they interact with the
Vertex AI Agent Engine.

| Diagram | Scope | Highlights |
|---|---|---|
| ![crf fan-out](crf_fanout_system_architecture.png) | System (breadth) | Pub/Sub trigger → **Eventarc** → Orchestrator (`crf_entrypoint`, concurrency=100) queries BigQuery + marks `QUEUED` → **fans out** one worker message per trend → **Eventarc** → serialized Worker (`agent_worker_entrypoint`, concurrency=1 / max-instances=1 for project-wide Gemini quota) → **Vertex AI Agent Engine** (`creative_agent`) → BigQuery + GCS |
| ![crf worker](crf_worker_reliability_deepdive.png) | Worker (depth) | How one worker turns Pub/Sub **at-least-once** delivery into **exactly-once** processing: atomic BigQuery lock (`NULL→QUEUED→PROCESSING→PROCESSED/FAILED`), duplicate-redelivery short-circuit (return + ACK), the `agent_session` create→stream→delete triad (same `user_id`, delete always in `finally`), and the ACK-success / NACK-retry semantics |

## Regenerating

Diagrams are generated one at a time (respecting the shared 2 RPM
`gemini-3.1-flash-image` cap) via the `paperbanana-figures` skill. To tweak a
label without a full regenerate, use `continue_diagram(run_id=..., feedback=...)`.
