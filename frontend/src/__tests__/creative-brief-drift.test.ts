import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { toBriefPayload } from "@/lib/brief-edit";
import { parseCreativeBrief } from "@/lib/creative-brief";
import { SAMPLE_CREATIVE_BRIEF } from "./helpers";

// Drift guard: the checkpoint-1 brief edit sends the WHOLE brief, which the
// backend validates against the Python `CreativeBrief` (merge_brief_edit) and
// re-renders into creative_brief_md. A Python field the client does not carry
// would silently fall back to its default on every edit, so every field of the
// brief models must round-trip through parseCreativeBrief -> toBriefPayload.
const SCHEMAS = path.resolve(__dirname, "../../../creative_agent/schemas.py");
const source = readFileSync(SCHEMAS, "utf8");

/** Field names of a top-level pydantic class (4-space `name:` lines in its block). */
function pythonFields(className: string): string[] {
  const lines = source.split("\n");
  const start = lines.findIndex((l) => l.startsWith(`class ${className}(`));
  if (start < 0) throw new Error(`class ${className} not found in schemas.py`);
  const fields: string[] = [];
  for (const line of lines.slice(start + 1)) {
    if (line.trim() !== "" && !/^\s/.test(line)) break; // next top-level statement
    const m = /^ {4}([a-z_][a-z0-9_]*)\s*:/.exec(line);
    if (m && m[1] !== "model_config") fields.push(m[1]);
  }
  return fields;
}

// Python fields the client intentionally does not send. Keep this empty unless
// the backend fills the field itself; anything listed here is dropped on edit.
const NOT_SENT: Record<string, readonly string[]> = {};

/** A brief with every optional field set, so nothing is missing for lack of data. */
function fullBrief(): Record<string, unknown> {
  const raw = structuredClone(SAMPLE_CREATIVE_BRIEF);
  raw.trend_bridge.timely_angle = "Texas vs Oklahoma is tomorrow: anticipation only.";
  return raw;
}

const payload = toBriefPayload(parseCreativeBrief(fullBrief())!);
const obj = (v: unknown) => v as Record<string, unknown>;

const LEVELS: { className: string; value: Record<string, unknown> }[] = [
  { className: "CreativeBrief", value: payload },
  { className: "BrandCues", value: obj(payload.brand) },
  { className: "TrendBridge", value: obj(payload.trend_bridge) },
  { className: "ReasonToBelieve", value: obj((payload.reasons_to_believe as unknown[])[0]) },
  { className: "CreativeAngle", value: obj((payload.angles as unknown[])[0]) },
];

describe("creative brief payload vs Python schema", () => {
  it.each(LEVELS)("$className: every Python field is sent, and nothing else", ({ className, value }) => {
    const fields = pythonFields(className);
    expect(fields.length).toBeGreaterThan(0);
    const expected = fields.filter((f) => !(NOT_SENT[className] ?? []).includes(f));
    expect(Object.keys(value).sort()).toEqual(expected.sort());
  });
});
