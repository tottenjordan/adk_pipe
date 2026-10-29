// @vitest-environment jsdom
import React, { Suspense, act } from "react";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { VisualConcept } from "@/lib/eval-matching";

const pushMock = vi.fn();
let currentSearchParams = new URLSearchParams();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: vi.fn() }),
  useSearchParams: () => currentSearchParams,
}));

const listSessionsMock = vi.fn();
const listPersonRefsMock = vi.fn();
const listSharesMock = vi.fn();
const createSessionMock = vi.fn();
const fetchBrandAssistMock = vi.fn();
const getSessionMock = vi.fn();
const listArtifactsMock = vi.fn();
const getArtifactMock = vi.fn();
const getRatingsMock = vi.fn();
const fetchEvalReportMock = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    listSessions: (...a: unknown[]) => listSessionsMock(...a),
    listPersonRefs: (...a: unknown[]) => listPersonRefsMock(...a),
    listShares: (...a: unknown[]) => listSharesMock(...a),
    createSession: (...a: unknown[]) => createSessionMock(...a),
    fetchBrandAssist: (...a: unknown[]) => fetchBrandAssistMock(...a),
    getSession: (...a: unknown[]) => getSessionMock(...a),
    listArtifacts: (...a: unknown[]) => listArtifactsMock(...a),
    getArtifact: (...a: unknown[]) => getArtifactMock(...a),
    getRatings: (...a: unknown[]) => getRatingsMock(...a),
  };
});

vi.mock("@/lib/eval-report", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/eval-report")>()),
  fetchEvalReport: (...a: unknown[]) => fetchEvalReportMock(...a),
}));

import Home from "@/app/page";
import ResultsPage from "@/app/results/[sessionId]/page";
import { RunChangelogPanel } from "@/app/results/[sessionId]/run-changelog";
import { VideoReelPanel } from "@/app/results/[sessionId]/video-reel-panel";
import { ReviewPanel } from "@/app/run/[sessionId]/ReviewPanel";
import {
  PipelineInspector,
  extractResiduals,
} from "@/app/run/[sessionId]/pipeline-inspector";
import {
  clearDraftsFor,
  clearReviewDrafts,
} from "@/app/run/[sessionId]/review-drafts";
import { ResumeRejectedError, resumeRun } from "@/lib/api";
import { EMPTY_CAMPAIGN_FORM, buildInitialState } from "@/lib/initial-state";
import { briefFromState } from "@/lib/run-history";
import { deriveStages } from "@/lib/run-stages";
import type { VideoStoryboard } from "@/lib/video-storyboard";

const SAMPLE_CONCEPTS: VisualConcept[] = [
  {
    ad_copy_id: 1,
    trend: "stadium guitar encore",
    aspect_ratio: "16:9",
    concept_name: "Encore Riser",
    visual_style: "Editorial stage photography",
    composition_mode: "product_in_scene",
    headline: "Built For Encore Sustain",
    social_caption: "PRS SE CE24 on tour.",
    call_to_action: "Play PRS SE CE24",
    concept_summary: "PRS SE CE24 under warm stadium beams.",
    image_generation_prompt: "Editorial stage photo of PRS SE CE24.",
    trend_reference: "Live stadium encore energy.",
    markets_product: "Figured maple top.",
    audience_appeal: "Gigging guitarists.",
    selection_rationale: "Hero stage keyframe.",
  },
  {
    ad_copy_id: 2,
    trend: "stadium guitar encore",
    aspect_ratio: "9:16",
    concept_name: "Backstage Fretboard",
    visual_style: "Macro studio detail",
    composition_mode: "product_focus",
    headline: "Snap Into Every Chorus",
    social_caption: "Bolt-on maple neck.",
    call_to_action: "Hear The Tone",
    concept_summary: "Close-up of bird inlays and maple neck.",
    image_generation_prompt: "Close-up macro photo of PRS fretboard.",
    trend_reference: "Encore soundcheck detail.",
    markets_product: "Bolt-on snap.",
    audience_appeal: "Studio players.",
    selection_rationale: "Detail cutdown anchor.",
  },
];

function makeValidStoryboard(): VideoStoryboard {
  return {
    title: "PRS SE CE24 — Encore Sustain",
    creative_direction: "From backstage quiet to full stadium sustain.",
    music_direction: "Warm instrumental post-rock guitar swell, no vocals.",
    voiceover_style: "Warm, grounded studio narrator",
    shots: [
      {
        shot_number: 1,
        duration_seconds: 6,
        camera_motion: "Slow push-in dolly on illuminated stage riser",
        visual_action_prompt:
          "Stage lights bloom behind the PRS SE CE24 resting on its stand.",
        voiceover_line: "Every encore starts before the crowd hears a note.",
        on_screen_super: "PRS Guitars",
        audio_cue: "Warm amp hum and cymbal shimmer",
        anchor_concept_name: "Encore Riser",
        include_in_15s: true,
        include_in_6s: true,
      },
      {
        shot_number: 2,
        duration_seconds: 8,
        camera_motion: "Smooth macro slider along maple neck",
        visual_action_prompt:
          "Fingers fret a chord across the bird inlays under amber spotlights.",
        voiceover_line:
          "Bolt-on maple snap meets effortless coil-split versatility.",
        on_screen_super: "",
        audio_cue: "Clean arpeggio chord bloom",
        anchor_concept_name: "Backstage Fretboard",
        include_in_15s: false,
        include_in_6s: false,
      },
      {
        shot_number: 3,
        duration_seconds: 4,
        camera_motion: "Low-angle tracking shot",
        visual_action_prompt:
          "Guitarist steps toward the mic as stage haze catches golden beams.",
        voiceover_line: "Dialed for the spotlight.",
        on_screen_super: "",
        audio_cue: "Tight drum fill",
        anchor_concept_name: "Encore Riser",
        include_in_15s: true,
        include_in_6s: false,
      },
      {
        shot_number: 4,
        duration_seconds: 6,
        camera_motion: "Orbiting medium close-up",
        visual_action_prompt:
          "Carved maple top reflects stadium lights on the final sustained chord.",
        voiceover_line: "Sustain that carries to the back row.",
        on_screen_super: "Built For Encore Sustain",
        audio_cue: "Full instrumental crescendo",
        anchor_concept_name: "Backstage Fretboard",
        include_in_15s: true,
        include_in_6s: false,
      },
      {
        shot_number: 5,
        duration_seconds: 6,
        camera_motion: "Locked hero end-card frame",
        visual_action_prompt:
          "PRS SE CE24 centered in crisp studio-stage lighting.",
        voiceover_line: "Play the PRS SE CE24 today.",
        on_screen_super: "Play PRS SE CE24",
        audio_cue: "Clean harmonic ring-out",
        anchor_concept_name: "Encore Riser",
        include_in_15s: false,
        include_in_6s: false,
      },
    ],
  };
}

const SAMPLE_GENERATED_VIDEOS = {
  hero_30s_16x9: {
    cut: "hero_30s_16x9",
    label: "30s Hero (16:9)",
    gcs_uri: "gs://tt-bucket/campaigns/run-42/videos/hero_30s_16x9.mp4",
    artifact_key: "video_hero_30s_16x9.mp4",
    duration_seconds: 30,
    aspect_ratio: "16:9",
    backend: "omniflash",
    shot_numbers: [1, 2, 3, 4, 5],
    voiceover_transcript:
      "Every encore starts before the crowd hears a note. Play the PRS SE CE24 today.",
    qa_passed: true,
    qa_summary: "5/5 shots passed multimodal QA",
  },
  social_15s_9x16: {
    cut: "social_15s_9x16",
    label: "15s Social (9:16)",
    gcs_uri: "gs://tt-bucket/campaigns/run-42/videos/social_15s_9x16.mp4",
    artifact_key: "video_social_15s_9x16.mp4",
    duration_seconds: 16,
    aspect_ratio: "9:16",
    backend: "veo",
    shot_numbers: [1, 3, 4],
    voiceover_transcript:
      "Every encore starts before the crowd hears a note. Dialed for the spotlight.",
    qa_passed: false,
    qa_summary: "2/3 shots passed multimodal QA",
  },
  bumper_6s_9x16: {
    cut: "bumper_6s_9x16",
    label: "6s Bumper (9:16)",
    gcs_uri: "",
    artifact_key: "video_bumper_6s_9x16.mp4",
    duration_seconds: 6,
    aspect_ratio: "9:16",
    backend: "omniflash",
    shot_numbers: [1],
    voiceover_transcript: "",
    qa_passed: true,
    qa_summary: "1/1 shots passed multimodal QA",
  },
};

beforeEach(() => {
  currentSearchParams = new URLSearchParams();
  sessionStorage.clear();
  pushMock.mockReset();
  listSessionsMock.mockReset();
  listPersonRefsMock.mockReset();
  listSharesMock.mockReset();
  createSessionMock.mockReset();
  fetchBrandAssistMock.mockReset();
  getSessionMock.mockReset();
  listArtifactsMock.mockReset();
  getArtifactMock.mockReset();
  getRatingsMock.mockReset();
  fetchEvalReportMock.mockReset();
  listSessionsMock.mockResolvedValue([]);
  listPersonRefsMock.mockResolvedValue({ personRefs: [], prefix: null });
  listSharesMock.mockResolvedValue([]);
  listArtifactsMock.mockResolvedValue([]);
  getRatingsMock.mockResolvedValue([]);
  fetchEvalReportMock.mockResolvedValue({ status: "missing" });
});

afterEach(() => {
  clearReviewDrafts();
  vi.unstubAllGlobals();
});

describe("Phase 3 intake toggle & session state round-trip", () => {
  it("defaults videoProductionEnabled to false and maps video_production_enabled for creative agents only", () => {
    expect(EMPTY_CAMPAIGN_FORM.videoProductionEnabled).toBe(false);

    const creativeOff = buildInitialState({
      ...EMPTY_CAMPAIGN_FORM,
      agent: "creative_agent",
    });
    expect(creativeOff.video_production_enabled).toBe(false);

    const creativeOn = buildInitialState({
      ...EMPTY_CAMPAIGN_FORM,
      agent: "creative_agent",
      videoProductionEnabled: true,
    });
    expect(creativeOn.video_production_enabled).toBe(true);

    const interactiveOn = buildInitialState({
      ...EMPTY_CAMPAIGN_FORM,
      agent: "interactive_creative",
      videoProductionEnabled: true,
    });
    expect(interactiveOn.video_production_enabled).toBe(true);

    const scout = buildInitialState({
      ...EMPTY_CAMPAIGN_FORM,
      agent: "trend_scout",
      videoProductionEnabled: true,
    });
    expect(scout).not.toHaveProperty("video_production_enabled");
  });

  it("restores videoProductionEnabled in briefFromState", () => {
    const enabledBrief = briefFromState({
      brand: "PRS Guitars",
      video_production_enabled: true,
    });
    expect(enabledBrief?.videoProductionEnabled).toBe(true);

    const disabledBrief = briefFromState({
      brand: "PRS Guitars",
      video_production_enabled: false,
    });
    expect(disabledBrief?.videoProductionEnabled).toBe(false);
  });

  it("renders unchecked #video-production-enabled on Quick Form for creative agents and syncs with Guided Brief", async () => {
    currentSearchParams = new URLSearchParams("agent=creative_agent");
    render(<Home />);
    await waitFor(() => expect(listSessionsMock).toHaveBeenCalled());

    const quickCheckbox = document.getElementById(
      "video-production-enabled"
    ) as HTMLInputElement | null;
    expect(quickCheckbox).not.toBeNull();
    expect(quickCheckbox?.checked).toBe(false);

    fireEvent.click(quickCheckbox!);
    expect(quickCheckbox?.checked).toBe(true);

    // Switch to Guided brief -> Step 3 ("3. Audience & Launch") reflects checked state
    fireEvent.click(screen.getByRole("tab", { name: "Guided brief" }));
    fireEvent.click(
      screen.getByRole("button", { name: "3. Audience & Launch" })
    );

    const guidedCheckbox = document.getElementById(
      "video-production-enabled"
    ) as HTMLInputElement | null;
    expect(guidedCheckbox).not.toBeNull();
    expect(guidedCheckbox?.checked).toBe(true);

    // Unchecking in Guided brief syncs back to Quick form
    fireEvent.click(guidedCheckbox!);
    fireEvent.click(screen.getByRole("tab", { name: "Quick form" }));
    const syncedQuick = document.getElementById(
      "video-production-enabled"
    ) as HTMLInputElement | null;
    expect(syncedQuick?.checked).toBe(false);
  });

  it("hides #video-production-enabled when trend_scout is selected", async () => {
    currentSearchParams = new URLSearchParams("agent=trend_scout");
    render(<Home />);
    await waitFor(() => expect(listSessionsMock).toHaveBeenCalled());

    expect(document.getElementById("video-production-enabled")).toBeNull();
  });
});

describe("Conditional run stages & PipelineInspector", () => {
  it("keeps exact 7 creative_agent stages and 10 interactive_creative stages when video_production_enabled is false or unset", () => {
    expect(deriveStages("creative_agent", {}, null, "running")).toHaveLength(7);
    expect(
      deriveStages(
        "creative_agent",
        { video_production_enabled: false },
        null,
        "running"
      )
    ).toHaveLength(7);
    expect(
      deriveStages(
        "creative_agent",
        { video_production_enabled: "false" },
        null,
        "running"
      )
    ).toHaveLength(7);
    expect(
      deriveStages("interactive_creative", {}, null, "running")
    ).toHaveLength(10);
  });

  it("inserts Phase 3 video stages after images and before eval_save when video_production_enabled is true", () => {
    const creativeStages = deriveStages(
      "creative_agent",
      {
        video_production_enabled: true,
        _images_generated: true,
      },
      null,
      "running"
    );
    expect(creativeStages).toHaveLength(9);
    expect(creativeStages.map((s) => s.id).slice(-4)).toEqual([
      "images",
      "video_storyboard",
      "video_reel",
      "eval_save",
    ]);
    expect(
      creativeStages.find((s) => s.id === "video_storyboard")?.state
    ).toBe("active");

    const interactivePaused = deriveStages(
      "interactive_creative",
      {
        video_production_enabled: true,
        _images_generated: true,
        video_storyboard: makeValidStoryboard(),
      },
      {
        eventId: "ev-sb-1",
        functionCallId: "fc-sb-1",
        functionName: "review_video_storyboard",
      },
      "paused"
    );
    expect(interactivePaused).toHaveLength(13);
    expect(interactivePaused.map((s) => s.id).slice(-5)).toEqual([
      "images",
      "video_storyboard",
      "review_video_storyboard",
      "video_reel",
      "eval_save",
    ]);
    expect(
      interactivePaused.find((s) => s.id === "review_video_storyboard")?.state
    ).toBe("needs_review");
  });

  it("marks video_reel degraded when fail-soft generated_videos__issues is set without _videos_generated", () => {
    const stages = deriveStages(
      "creative_agent",
      {
        video_production_enabled: true,
        _images_generated: true,
        video_storyboard: makeValidStoryboard(),
        _videos_generated: false,
        generated_videos__issues: ["Veo quota exhausted"],
        finalize_done: true,
      },
      null,
      "completed"
    );
    expect(stages.find((s) => s.id === "video_reel")?.state).toBe("degraded");
    expect(stages.find((s) => s.id === "eval_save")?.state).toBe("done");
  });

  it("includes video_storyboard_revision_rounds_used and video residual labels in PipelineInspector and RunChangelogPanel", () => {
    const sessionState = {
      video_production_enabled: true,
      brief_revision_rounds_used: 1,
      ad_copy_revision_rounds_used: 0,
      visual_concept_revision_rounds_used: 1,
      video_storyboard_revision_rounds_used: 2,
      video_storyboard__issues: ["Hero cut sum repaired"],
      video_qa__issues: ["Shot 2 required 1 retake"],
      generated_videos__issues: ["Bumper uploaded with fallback"],
    };

    const residuals = extractResiduals(sessionState);
    expect(residuals.map((r) => [r.key, r.stageLabel])).toEqual([
      ["generated_videos__issues", "Video reel"],
      ["video_qa__issues", "Video check"],
      ["video_storyboard__issues", "Video storyboard"],
    ]);

    render(
      <>
        <PipelineInspector sessionState={sessionState} appName="creative_agent" />
        <RunChangelogPanel
          changelog={{
            session_id: "sess-42",
            app_name: "creative_agent",
            origin: "studio",
            handoff_source_session_id: null,
            gate_rounds: {
              brief: 1,
              ad_copy: 0,
              visual_concept: 1,
              video_storyboard: 2,
            },
            source_counts: { web: 2, enterprise: 0 },
            warnings: [],
            entries: [],
          }}
          loading={false}
          error={null}
          onOpen={() => {}}
        />
      </>
    );
    expect(screen.getByText("4 gate revisions")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Pipeline inspector"));
    expect(screen.getByText("Video storyboard gate")).toBeInTheDocument();
    expect(screen.getByText("2 revisions")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Run changelog/i }));
    expect(
      screen.getByText("Brief 1 · Copy 0 · Visual 1 · Storyboard 2")
    ).toBeInTheDocument();
  });
});

describe("Checkpoint 4: ReviewPanel review_video_storyboard & 400 error joining", () => {
  function renderCheckpoint4(
    overrides: Record<string, unknown> = {},
    props: {
      functionCallId?: string;
      serverErrors?: Record<string, string>;
    } = {}
  ) {
    const onResume = vi.fn();
    const utils = render(
      <ReviewPanel
        functionName="review_video_storyboard"
        functionCallId={props.functionCallId}
        serverErrors={props.serverErrors}
        sessionState={{
          brand: "PRS Guitars",
          target_product: "PRS SE CE24",
          final_visual_concepts: { visual_concepts: SAMPLE_CONCEPTS },
          video_storyboard: makeValidStoryboard(),
          ...overrides,
        }}
        onResume={onResume}
      />
    );
    return { onResume, ...utils };
  }

  it("shows live cut-duration totals and approves an untouched storyboard without sending edits", () => {
    const { onResume } = renderCheckpoint4();

    expect(
      screen.getByRole("heading", { name: "Review video storyboard" })
    ).toBeInTheDocument();
    expect(screen.getByText(/30s Hero: 30s \/ 30s/)).toBeInTheDocument();
    expect(screen.getByText(/15s Social: 16s \(3 shots\)/)).toBeInTheDocument();
    expect(screen.getByText(/6s Bumper: 6s \(1 shot\)/)).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: /Approve & render video reel/i })
    );
    expect(onResume).toHaveBeenCalledTimes(1);
    const payload = onResume.mock.calls[0][0] as Record<string, unknown>;
    expect(payload.status).toBe("approved");
    expect(payload).not.toHaveProperty("edits");
    expect(payload).not.toHaveProperty("storyboard_edited");
  });

  it("blocks submit when shot duration breaks the 30s Hero total or VO exceeds ceil(2.3 * duration) words", () => {
    const { onResume } = renderCheckpoint4();

    // Mutate Shot 1 duration from 6s to 8s -> Hero becomes 32s and Bumper becomes 8s
    fireEvent.change(screen.getByLabelText("Shot 1 duration"), {
      target: { value: "8" },
    });
    expect(screen.getByText(/30s Hero: 32s \/ 30s/)).toBeInTheDocument();
    expect(
      screen.getByText(/30s Hero cut totals 32s \(must equal 30s\)/)
    ).toBeInTheDocument();

    const approveBtn = screen.getByRole("button", {
      name: /Approve & render video reel/i,
    });
    expect(approveBtn).toBeDisabled();

    // Restore Shot 1 duration back to 6s, then exceed VO word limit on Shot 3 (4s -> max 10 words)
    fireEvent.change(screen.getByLabelText("Shot 1 duration"), {
      target: { value: "6" },
    });
    expect(approveBtn).not.toBeDisabled();

    const shot3Vo = screen.getByLabelText("Shot 3 voiceover line");
    fireEvent.change(shot3Vo, {
      target: {
        value:
          "One two three four five six seven eight nine ten eleven words",
      },
    });
    expect(screen.getByText("12/10 words")).toBeInTheDocument();
    expect(shot3Vo).toHaveAttribute("aria-invalid", "true");
    expect(
      screen.getByText(/Voiceover has 12 words \(max 10 words for a 4s shot\)/)
    ).toBeInTheDocument();
    expect(approveBtn).toBeDisabled();

    fireEvent.click(approveBtn);
    expect(onResume).not.toHaveBeenCalled();
  });

  it("submits full edited video_storyboard with renumbered shots when valid edits are made", () => {
    const { onResume } = renderCheckpoint4();

    fireEvent.change(screen.getByLabelText("Spot title"), {
      target: { value: "PRS SE CE24 — Midnight Encore" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Move shot 2 up" }));

    fireEvent.click(
      screen.getByRole("button", { name: /Approve & render video reel/i })
    );
    expect(onResume).toHaveBeenCalledTimes(1);
    const payload = onResume.mock.calls[0][0] as {
      status: string;
      storyboard_edited?: boolean;
      edits?: { field: string; value: VideoStoryboard }[];
    };
    expect(payload.status).toBe("approved");
    expect(payload.storyboard_edited).toBe(true);
    expect(payload.edits).toHaveLength(1);
    expect(payload.edits?.[0].field).toBe("video_storyboard");
    expect(payload.edits?.[0].value.title).toBe(
      "PRS SE CE24 — Midnight Encore"
    );
    // Shot 2 (8s, Backstage Fretboard) moved to position 1 with sequential 1..5 shot_number
    expect(
      payload.edits?.[0].value.shots.map((s) => s.shot_number)
    ).toEqual([1, 2, 3, 4, 5]);
    expect(payload.edits?.[0].value.shots[0].anchor_concept_name).toBe(
      "Backstage Fretboard"
    );
  });

  it("preserves draft across remount for the same functionCallId, evicts via clearDraftsFor, gates submit on serverErrors, and wires audio_cue errors", () => {
    const first = renderCheckpoint4({}, { functionCallId: "fc-sb-400" });
    fireEvent.change(screen.getByLabelText("Spot title"), {
      target: { value: "Preserved Spot Draft" },
    });
    first.unmount();

    const second = renderCheckpoint4(
      {},
      {
        functionCallId: "fc-sb-400",
        serverErrors: {
          shots:
            "Shot 1: on_screen_super 'Unapproved' does not match approved copy.\nShot 2: visual_action_prompt contains avoided term 'cheap'.",
          "shots.0.audio_cue": "Audio cue contains disallowed vocal lyric.",
        },
      }
    );

    expect(screen.getByLabelText("Spot title")).toHaveValue(
      "Preserved Spot Draft"
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(
      "Shot 1: on_screen_super 'Unapproved' does not match approved copy."
    );
    expect(alert).toHaveTextContent(
      "Shot 2: visual_action_prompt contains avoided term 'cheap'."
    );

    // audio_cue error is wired via aria-invalid + aria-describedby FieldError
    const shot1Audio = screen.getByLabelText("Shot 1 audio cue");
    expect(shot1Audio).toHaveAttribute("aria-invalid", "true");
    expect(
      screen.getByText("Audio cue contains disallowed vocal lyric.")
    ).toBeInTheDocument();

    // Submit button and Ctrl/Cmd+Enter are gated while activeServerErrors exist
    const approveBtn = screen.getByRole("button", {
      name: /Approve & render video reel/i,
    });
    expect(approveBtn).toBeDisabled();
    fireEvent.keyDown(document, { key: "Enter", ctrlKey: true });
    expect(second.onResume).not.toHaveBeenCalled();

    // Editing the draft clears stale server errors and re-enables approval
    fireEvent.change(shot1Audio, {
      target: { value: "Clean instrumental harmonic swell" },
    });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(shot1Audio).not.toHaveAttribute("aria-invalid");
    expect(approveBtn).not.toBeDisabled();

    // Evicting via clearDraftsFor("fc-sb-400") resets back to the server storyboard on remount
    second.unmount();
    clearDraftsFor("fc-sb-400");
    renderCheckpoint4({}, { functionCallId: "fc-sb-400" });
    expect(screen.getByLabelText("Spot title")).toHaveValue(
      "PRS SE CE24 — Encore Sustain"
    );
  });

  it("joins multiple 400 validation errors sharing the same loc in resumeRun", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: {
              message: "Invalid storyboard edit",
              errors: [
                {
                  loc: "shots",
                  msg: "Hero 30s cut duration sum is 26s (must equal 30s).",
                },
                {
                  loc: "shots",
                  msg: "Bumper 6s cut duration sum is 4s (must equal 6s).",
                },
              ],
            },
          }),
          { status: 400, headers: { "Content-Type": "application/json" } }
        )
      )
    );

    let caught: unknown;
    try {
      await resumeRun(
        "interactive_creative",
        "me",
        "sess-1",
        "fc-1",
        "review_video_storyboard",
        { status: "approved" }
      );
    } catch (err) {
      caught = err;
    }

    expect(caught).toBeInstanceOf(ResumeRejectedError);
    const rejected = caught as ResumeRejectedError;
    expect(rejected.fieldErrors.shots).toBe(
      "Hero 30s cut duration sum is 26s (must equal 30s).\nBumper 6s cut duration sum is 4s (must equal 6s)."
    );
  });
});

describe("VideoReelPanel & ResultsPage Proof Room integration", () => {
  it("renders nothing on default static runs without generated_videos", () => {
    const { container } = render(
      <VideoReelPanel
        state={{ _images_generated: true }}
        artifactUrlByName={new Map()}
        outputUrl={() => null}
      />
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders 30s/15s/6s tabs, keyboard navigation, /api/gcs + inline artifact URLs, transcripts, QA badges, and download link", () => {
    const artifactMap = new Map<string, string>([
      ["video_bumper_6s_9x16.mp4", "data:video/mp4;base64,AAAA"],
    ]);

    render(
      <VideoReelPanel
        state={{
          video_production_enabled: true,
          video_storyboard: makeValidStoryboard(),
          generated_videos: SAMPLE_GENERATED_VIDEOS,
        }}
        artifactUrlByName={artifactMap}
        outputUrl={(f) => `/fallback/${f}`}
      />
    );

    const tablist = screen.getByRole("tablist", { name: "Video reel cuts" });
    const tabs = within(tablist).getAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual([
      "30s Hero (16:9)",
      "15s Social (9:16)",
      "6s Bumper (9:16)",
    ]);
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");

    // 30s Hero resolves via /api/gcs proxy, shows Omni Flash 1.1 & QA passed
    const heroVideo = screen.getByLabelText(
      "30s Hero (16:9) video player"
    ) as HTMLVideoElement;
    expect(heroVideo.getAttribute("src")).toBe(
      "/api/gcs?bucket=tt-bucket&path=campaigns%2Frun-42%2Fvideos%2Fhero_30s_16x9.mp4"
    );
    const activePanel = screen.getByRole("tabpanel");
    expect(within(activePanel).getByText("QA passed")).toBeInTheDocument();
    expect(within(activePanel).getByText("Omni Flash 1.1")).toBeInTheDocument();
    expect(
      within(activePanel).getByText(
        /Every encore starts before the crowd hears a note\./
      )
    ).toBeInTheDocument();

    const downloadLink = screen.getByRole("link", { name: /Download MP4/i });
    expect(downloadLink).toHaveAttribute("download", "hero_30s_16x9.mp4");

    // ArrowRight switches to 15s Social (9:16), Veo 3.1, QA flagged, and focuses the tab
    tabs[0].focus();
    fireEvent.keyDown(tabs[0], { key: "ArrowRight" });
    expect(tabs[1]).toHaveAttribute("aria-selected", "true");
    expect(document.activeElement).toBe(tabs[1]);
    const socialVideo = screen.getByLabelText(
      "15s Social (9:16) video player"
    ) as HTMLVideoElement;
    expect(socialVideo.getAttribute("src")).toBe(
      "/api/gcs?bucket=tt-bucket&path=campaigns%2Frun-42%2Fvideos%2Fsocial_15s_9x16.mp4"
    );
    expect(screen.getByText("QA flagged")).toBeInTheDocument();
    expect(screen.getByText("Veo 3.1")).toBeInTheDocument();
    expect(
      screen.getByText("2/3 shots passed multimodal QA")
    ).toBeInTheDocument();

    // Clicking 6s Bumper resolves inline ADK artifact data URL (gcs_uri is "") and shows instrumental fallback
    fireEvent.click(tabs[2]);
    const bumperVideo = screen.getByLabelText(
      "6s Bumper (9:16) video player"
    ) as HTMLVideoElement;
    expect(bumperVideo.getAttribute("src")).toBe(
      "data:video/mp4;base64,AAAA"
    );
    expect(
      screen.getByText("Instrumental only (no spoken voiceover)")
    ).toBeInTheDocument();
  });

  it("rejects unsafe video URLs and avoids 404 outputUrl fallback when gcs_uri is explicitly empty", () => {
    const unsafeArtifactMap = new Map<string, string>([
      ["video_hero_30s_16x9.mp4", "javascript:alert(1)"],
      ["video_social_15s_9x16.mp4", "https://evil.example/video.mp4"],
      ["video_bumper_6s_9x16.mp4", "data:text/html;base64,PHNjcmlwdD4="],
    ]);

    render(
      <VideoReelPanel
        state={{
          video_production_enabled: true,
          generated_videos: {
            hero_30s_16x9: {
              ...SAMPLE_GENERATED_VIDEOS.hero_30s_16x9,
              gcs_uri: "",
            },
            social_15s_9x16: {
              ...SAMPLE_GENERATED_VIDEOS.social_15s_9x16,
              gcs_uri: "",
            },
            bumper_6s_9x16: {
              ...SAMPLE_GENERATED_VIDEOS.bumper_6s_9x16,
              gcs_uri: "",
            },
          },
        }}
        artifactUrlByName={unsafeArtifactMap}
        outputUrl={(f) =>
          `/api/gcs?bucket=tt-bucket&path=${encodeURIComponent(`campaigns/run-42/${f}`)}`
        }
      />
    );

    const tabs = within(
      screen.getByRole("tablist", { name: "Video reel cuts" })
    ).getAllByRole("tab");

    // Hero: javascript: rejected & gcs_uri === "" prevents 404 outputUrl fallback
    expect(screen.getByText("Video file unavailable")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /Download MP4/i })
    ).not.toBeInTheDocument();

    // Social: https://evil.example rejected & gcs_uri === "" prevents fallback
    fireEvent.click(tabs[1]);
    expect(screen.getByText("Video file unavailable")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /Download MP4/i })
    ).not.toBeInTheDocument();

    // Bumper: data:text/html;base64,... rejected
    fireEvent.click(tabs[2]);
    expect(screen.getByText("Video file unavailable")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /Download MP4/i })
    ).not.toBeInTheDocument();
  });

  it("renders a calm degraded state card surfacing video_storyboard__issues and generated_videos__issues", () => {
    render(
      <VideoReelPanel
        state={{
          video_production_enabled: true,
          generated_videos: null,
          video_storyboard__issues: [
            "Storyboard director could not anchor Shot 3 concept",
          ],
          generated_videos__issues: [
            "All video cuts failed to conform: ffmpeg timeout",
          ],
        }}
        artifactUrlByName={new Map()}
        outputUrl={() => null}
      />
    );

    expect(
      screen.getByRole("heading", {
        name: "Video reel unavailable for this run",
      })
    ).toBeInTheDocument();
    expect(
      screen.getByText("Storyboard director could not anchor Shot 3 concept")
    ).toBeInTheDocument();
    expect(
      screen.getByText("All video cuts failed to conform: ffmpeg timeout")
    ).toBeInTheDocument();
  });

  it("mounts VideoReelPanel inside ResultsPage and skips getArtifact for gs://-backed MP4s & static PNGs while fetching empty-gcs_uri MP4s", async () => {
    currentSearchParams = new URLSearchParams("app=creative_agent");
    listArtifactsMock.mockResolvedValue([
      "video_hero_30s_16x9.mp4",
      "video_social_15s_9x16.mp4",
      "video_bumper_6s_9x16.mp4",
      "encore_riser.png",
    ]);
    getArtifactMock.mockImplementation(
      async (_app: string, _user: string, _sess: string, name: string) => {
        if (name === "video_bumper_6s_9x16.mp4") {
          return { inlineData: { mimeType: "video/mp4", data: "BBBB" } };
        }
        return { inlineData: { mimeType: "image/png", data: "CCCC" } };
      }
    );
    getSessionMock.mockResolvedValue({
      id: "sess-video-42",
      appName: "creative_agent",
      userId: "me",
      events: [
        { author: "creative_agent", content: { parts: [{ text: "Done" }] } },
      ],
      state: {
        brand: "PRS Guitars",
        target_product: "PRS SE CE24",
        eval_report_gcs_uri: "gs://tt-bucket/campaigns/run-42/eval.json",
        finalize_done: true,
        _images_generated: true,
        _videos_generated: true,
        video_production_enabled: true,
        final_visual_concepts: { visual_concepts: SAMPLE_CONCEPTS },
        ad_copy_critique: { ad_copies: [] },
        video_storyboard: makeValidStoryboard(),
        generated_videos: SAMPLE_GENERATED_VIDEOS,
        creative_evaluation_report: {
          summary: {
            total_creatives_evaluated: 2,
            overall_pass_rate: 1,
            average_visual_score: 0.9,
            average_ad_copy_score: 0.9,
          },
          visual_concept_evaluations: [],
          ad_copy_evaluations: [],
        },
      },
    });

    const paramsPromise = Promise.resolve({ sessionId: "sess-video-42" });
    await act(async () => {
      render(
        <Suspense fallback={<div>Loading…</div>}>
          <ResultsPage
            params={paramsPromise}
            searchParams={Promise.resolve({})}
          />
        </Suspense>
      );
    });

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Built For Encore Sustain" })
      ).toBeInTheDocument();
    });
    expect(
      screen.getByRole("heading", { name: "Multi-cut video reel" })
    ).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: "30s Hero (16:9)" })
    ).toBeInTheDocument();

    // Only the MP4 cut lacking a gs:// URI is downloaded via getArtifact; gs:// MP4s and PNGs are not
    const fetchedArtifactNames = getArtifactMock.mock.calls.map((c) => c[3]);
    expect(fetchedArtifactNames).toEqual(["video_bumper_6s_9x16.mp4"]);

    // Switching to 6s Bumper plays the fetched inline data:video/mp4;base64,BBBB payload
    fireEvent.click(screen.getByRole("tab", { name: "6s Bumper (9:16)" }));
    const bumperVideo = screen.getByLabelText(
      "6s Bumper (9:16) video player"
    ) as HTMLVideoElement;
    expect(bumperVideo.getAttribute("src")).toBe(
      "data:video/mp4;base64,BBBB"
    );
  });
});
