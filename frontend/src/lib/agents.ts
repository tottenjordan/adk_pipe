import type { CampaignInput } from "@/lib/types";

export type AgentId = CampaignInput["agent"];

export interface AgentInfo {
  id: AgentId;
  /** Short sentence-case name shown on tiles and in run history. */
  label: string;
  /** What the agent does, from the user's point of view. */
  description: string;
  /** Typical wall-clock duration. */
  duration: string;
  /** Where (if anywhere) the run stops for the user. */
  pauses: string;
  /** Home-form submit button label. */
  submitLabel: string;
  /** True when a run can stop and wait for the user's review. */
  canPause: boolean;
}

/** The three agents, in the order the home form offers them. */
export const AGENTS: readonly AgentInfo[] = [
  {
    id: "trend_scout",
    label: "Trend scout",
    description:
      "Finds today's top Google Search trends and picks the ones that fit your campaign",
    duration: "~2 min",
    pauses: "Optional trend pick",
    submitLabel: "Find trends",
    canPause: true,
  },
  {
    id: "creative_agent",
    label: "Creative run",
    description:
      "Researches one trend and your brand, then writes ad copy, renders images and scores them",
    duration: "~10 min",
    pauses: "No pauses",
    submitLabel: "Generate creatives",
    canPause: false,
  },
  {
    id: "interactive_creative",
    label: "Creative run with reviews",
    description:
      "Same as a creative run, but pauses for your review after research, ad copy and visual concepts",
    duration: "~10 min + review time",
    pauses: "3 review pauses",
    submitLabel: "Generate creatives",
    canPause: true,
  },
] as const;

export function isAgentId(value: unknown): value is AgentId {
  return AGENTS.some((a) => a.id === value);
}

export function agentInfo(appName: string): AgentInfo | undefined {
  return AGENTS.find((a) => a.id === appName);
}

/** Display label for an app name; unknown apps read as a generic "Run". */
export function agentLabel(appName: string | undefined | null): string {
  return (appName && agentInfo(appName)?.label) || "Run";
}

/** Submit-button label for the home form. */
export function submitLabel(appName: AgentId): string {
  return agentInfo(appName)?.submitLabel ?? "Start run";
}

/** True for the two agents that take a trend and produce creatives. */
export function isCreativeAgent(appName: string | undefined | null): boolean {
  return appName === "creative_agent" || appName === "interactive_creative";
}
