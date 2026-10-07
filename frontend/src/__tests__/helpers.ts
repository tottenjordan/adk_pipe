// Shared test utilities. Not a test file: vitest's default include only picks
// up *.test.* / *.spec.*, so this module is imported, never collected.

/** A real JSON `Response` with the given body and status (default 200). */
export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** A `creative_brief` state value in the backend's snake_case shape (PRS × Powerball). */
export const SAMPLE_CREATIVE_BRIEF = {
  objective: "Make the SE CE24 the guitar jam-band millennials daydream about owning.",
  audience: "Millennial jam-band fans who trade surreal memes and miss their first guitar.",
  insight: "They joke about what they'd buy with a jackpot, but the dream is always a better tone.",
  single_minded_proposition: "The only jackpot worth chasing is your own tone.",
  reasons_to_believe: [
    { claim: "85/15 S humbuckers cover thick humbucker to clear single-coil tones.", source_id: "brief" },
    { claim: "The Powerball jackpot passed $1.8B this week.", source_id: "src-2" },
  ],
  brand: {
    tone_of_voice: "warm, witty, never smug",
    distinctive_assets: ["bird inlays", "PRS headstock"],
    do_not: ["mock other guitar brands"],
  },
  trend_bridge: {
    fit_score: 3,
    fit_mode: "cultural",
    bridge: "The jackpot daydream maps onto the dream rig every player keeps.",
    motifs: ["lottery ball draw", "golden ticket"],
    risks: ["gambling glamorisation"],
  },
  mandatories: ["SE CE24", "85/15 S pickups"],
  avoid: ["real lottery winners' likenesses"],
  desired_response: "Think: that's my dream rig. Do: try an SE CE24.",
  angles: [
    { angle_id: "A1", name: "Jackpot rig", tension: "Dreaming big vs. playing small rooms", route: "A lottery ball draw where every ball is a pickup setting." },
    { angle_id: "A2", name: "Signs", tension: "Flexing vs. authenticity", route: "Parody the 'there would be signs' meme with tone as the only flex." },
    { angle_id: "A3", name: "First guitar", tension: "Nostalgia vs. upgrade guilt", route: "A golden ticket tucked in an old gig bag." },
  ],
};
