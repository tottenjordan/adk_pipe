/**
 * Grouping of the eval report's run notes (`warnings`) for the results page.
 *
 * The notes come from `agent_common.observability.collect_degradation_warnings`
 * plus the creative_eval judge, and mix three very different things:
 *
 * - research gaps — `Step '<key>' exhausted retries and produced no output.` for
 *   a research or brief producer: the creative stands on incomplete research;
 * - failed saves — `<X> save has unresolved issues: …` from the finalize step's
 *   persistence tools (plus the pre-label `… gcs uri` / `… row uuid` forms);
 * - quality flags — everything else: image/copy/concept check residuals, brief
 *   issues, judge notes.
 *
 * The image-step exhaustion (`_images_generated`) is dropped: the dedicated
 * zero-image banner (issue #116) already covers it.
 */

export interface ClassifiedWarnings {
  research: string[]
  saves: string[]
  quality: string[]
}

const EXHAUSTED = /^Step '([^']+)' exhausted retries/
const IMAGE_STEP = "_images_generated"
// Research and brief producers (creative_agent + trend_scout).
const RESEARCH_STEPS = new Set([
  "gs_web_search_insights",
  "campaign_web_search_insights",
  "refined_web_search_insights",
  "creative_brief",
  "info_gtrends",
  "info_gtrends_raw",
])
// "<X> save has unresolved issues", or the sentence-cased persistence key a
// report written before the labels were humanized carries.
const SAVE_ISSUE = /^(?:.+ save|.+ gcs uri|.+ row uuid) has unresolved issues\b/

export function classifyWarnings(warnings: string[] | undefined): ClassifiedWarnings {
  const out: ClassifiedWarnings = { research: [], saves: [], quality: [] }
  for (const note of warnings ?? []) {
    const step = EXHAUSTED.exec(note)?.[1]
    if (step === IMAGE_STEP) continue
    if (step && (RESEARCH_STEPS.has(step) || step.endsWith("_web_search_insights"))) {
      out.research.push(note)
    } else if (SAVE_ISSUE.test(note)) {
      out.saves.push(note)
    } else {
      out.quality.push(note)
    }
  }
  return out
}
