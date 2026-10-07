"use client";

import React, { useState } from "react";
import { Textarea } from "@/components/ui/textarea";
import { ResearchReport } from "@/components/research-report";
import {
  fromEditableReport,
  toEditableReport,
  type ReportSources,
} from "@/lib/research-report";
import { buildEditableResearchEdit, type ResearchEdit } from "./run-helpers";

/** Checkpoint-1 research-report edit state, shared by both review panels. */
export function useReportEdit(state: Record<string, unknown>) {
  const report = state.combined_final_cited_report as string | undefined;
  const sources = state.sources as ReportSources | undefined;
  // The textarea shows `[src-N]` markers instead of raw cite tags; they go
  // back to canonical `<cite source="src-N"/>` tags for preview and resume.
  const [editedReport, setEditedReport] = useState(() => toEditableReport(report ?? ""));
  const edits: ResearchEdit[] | null = buildEditableResearchEdit(report ?? "", editedReport);
  const previewSource = edits ? fromEditableReport(editedReport) : (report ?? "");
  return { report, sources, editedReport, setEditedReport, edits, previewSource };
}

/** The research report with an Edit / Preview toggle (nothing without a report). */
export function ResearchReportEditor({
  edit,
  idPrefix,
}: {
  edit: ReturnType<typeof useReportEdit>;
  idPrefix: string;
}) {
  const [editMode, setEditMode] = useState(false);
  const { report, sources, editedReport, setEditedReport, edits, previewSource } = edit;
  if (!report) return null;
  const hintId = `${idPrefix}-markers-hint`;
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-end gap-3">
        {edits && <span className="text-xs text-muted-foreground">Edited</span>}
        <button
          type="button"
          onClick={() => setEditMode((v) => !v)}
          className="rounded-sm text-xs font-medium text-primary hover:underline"
        >
          {editMode ? "Preview" : "Edit report"}
        </button>
      </div>
      {!editMode && <ResearchReport markdown={previewSource} sources={sources} scroll />}
      {editMode && (
        <div className="space-y-1.5">
          <Textarea
            value={editedReport}
            onChange={(e) => setEditedReport(e.target.value)}
            rows={16}
            aria-label="Research report"
            aria-describedby={hintId}
            className="font-mono text-xs leading-relaxed max-h-[28rem]"
          />
          <p id={hintId} className="text-xs text-muted-foreground">
            Source markers like [src-12] keep their citations. Leave them in place or delete them.
          </p>
        </div>
      )}
    </div>
  );
}
