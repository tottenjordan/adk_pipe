export interface Part {
  text?: string;
  functionCall?: {
    name: string;
    args: Record<string, unknown>;
  };
  functionResponse?: {
    name: string;
    response: Record<string, unknown>;
  };
  inlineData?: {
    mimeType: string;
    data: string;
  };
}

export interface Content {
  role: string;
  parts: Part[];
}

export interface AgentEvent {
  id: string;
  invocationId: string;
  author: string;
  content?: Content;
  actions?: {
    stateDelta?: Record<string, unknown>;
    artifactDelta?: Record<string, string>;
  };
  timestamp: number;
}

export interface Session {
  id: string;
  appName: string;
  userId: string;
  state: Record<string, unknown>;
  events: AgentEvent[];
}

export interface CampaignInput {
  agent: "trend_trawler" | "creative_agent";
  brand: string;
  targetAudience: string;
  targetProduct: string;
  keySellingPoints: string;
  targetSearchTrend?: string;
}
