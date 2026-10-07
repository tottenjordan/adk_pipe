/**
 * The structured creative brief (`creative_brief` session state), written by
 * creative_agent's brief writer (Python `CreativeBrief` in
 * creative_agent/schemas.py) and used as the contract for the ad copy and
 * visual agents.
 *
 * ADK sometimes stores `output_schema` values as JSON strings, and sessions
 * from before the brief existed have no key at all, so parsing is tolerant:
 * string or object in, missing lists become `[]`, and anything without a
 * single-minded proposition is treated as "no brief" (`null`).
 */

export type FitMode = "direct" | "cultural" | "light_touch";

export interface ReasonToBelieve {
  claim: string;
  /** A research source id (`src-N`), `"brief"` for the user's selling points, or null. */
  sourceId: string | null;
}

export interface BrandCues {
  toneOfVoice: string;
  distinctiveAssets: string[];
  doNot: string[];
}

export interface TrendBridge {
  /** Brand-trend fit, 1–5 (null when missing or out of range). */
  fitScore: number | null;
  fitMode: FitMode | null;
  bridge: string;
  motifs: string[];
  risks: string[];
}

export interface CreativeAngle {
  angleId: string;
  name: string;
  tension: string;
  route: string;
}

export interface CreativeBrief {
  objective: string;
  audience: string;
  insight: string;
  singleMindedProposition: string;
  reasonsToBelieve: ReasonToBelieve[];
  brand: BrandCues;
  trendBridge: TrendBridge;
  mandatories: string[];
  avoid: string[];
  desiredResponse: string;
  angles: CreativeAngle[];
}

type Obj = Record<string, unknown>;

const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);

const str = (v: unknown): string => (typeof v === "string" ? v.trim() : "");

const obj = (v: unknown): Obj => (isObj(v) ? v : {});

/** A list of non-empty strings (non-strings and blanks dropped). */
const strList = (v: unknown): string[] =>
  Array.isArray(v) ? v.map(str).filter((s) => s !== "") : [];

/** A list of objects mapped through `fn`, keeping only the entries it accepts. */
function objList<T>(v: unknown, fn: (o: Obj) => T | null): T[] {
  if (!Array.isArray(v)) return [];
  return v.flatMap((item) => {
    if (!isObj(item)) return [];
    const mapped = fn(item);
    return mapped === null ? [] : [mapped];
  });
}

const FIT_MODES: readonly FitMode[] = ["direct", "cultural", "light_touch"];

function parseFitScore(v: unknown): number | null {
  const n = typeof v === "string" && v.trim() !== "" ? Number(v) : v;
  if (typeof n !== "number" || !Number.isFinite(n)) return null;
  const rounded = Math.round(n);
  return rounded >= 1 && rounded <= 5 ? rounded : null;
}

function parseFitMode(v: unknown): FitMode | null {
  const mode = str(v).toLowerCase().replace(/[\s-]+/g, "_");
  return (FIT_MODES as readonly string[]).includes(mode) ? (mode as FitMode) : null;
}

/** Parse `creative_brief` from session state; null when absent or unusable. */
export function parseCreativeBrief(value: unknown): CreativeBrief | null {
  let raw: unknown = value;
  if (typeof raw === "string") {
    if (raw.trim() === "") return null;
    try {
      raw = JSON.parse(raw);
    } catch {
      return null;
    }
  }
  if (!isObj(raw)) return null;

  const singleMindedProposition = str(raw.single_minded_proposition);
  if (!singleMindedProposition) return null;

  const brand = obj(raw.brand);
  const bridge = obj(raw.trend_bridge);

  return {
    objective: str(raw.objective),
    audience: str(raw.audience),
    insight: str(raw.insight),
    singleMindedProposition,
    reasonsToBelieve: objList(raw.reasons_to_believe, (o) => {
      const claim = str(o.claim);
      return claim ? { claim, sourceId: str(o.source_id) || null } : null;
    }),
    brand: {
      toneOfVoice: str(brand.tone_of_voice),
      distinctiveAssets: strList(brand.distinctive_assets),
      doNot: strList(brand.do_not),
    },
    trendBridge: {
      fitScore: parseFitScore(bridge.fit_score),
      fitMode: parseFitMode(bridge.fit_mode),
      bridge: str(bridge.bridge),
      motifs: strList(bridge.motifs),
      risks: strList(bridge.risks),
    },
    mandatories: strList(raw.mandatories),
    avoid: strList(raw.avoid),
    desiredResponse: str(raw.desired_response),
    angles: objList(raw.angles, (o) => {
      const name = str(o.name);
      const route = str(o.route);
      const tension = str(o.tension);
      if (!name && !route && !tension) return null;
      return { angleId: str(o.angle_id), name, tension, route };
    }),
  };
}

const FIT_MODE_LABELS: Record<FitMode, string> = {
  direct: "Direct fit",
  cultural: "Cultural fit",
  light_touch: "Light touch",
};

/** Plain label for a trend fit mode ("" when unknown). */
export function fitModeLabel(mode: FitMode | null | undefined): string {
  return mode ? FIT_MODE_LABELS[mode] : "";
}
