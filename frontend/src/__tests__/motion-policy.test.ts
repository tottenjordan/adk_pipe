import { describe, it, expect } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";

/**
 * Motion policy (proof-room + Emil Kowalski's rules), enforced over all of src/:
 * - no transition-all and no bare `transition` (name the properties);
 * - no ease-in (ease-in-out is fine; the curves live in globals.css tokens);
 * - no duration above 300ms;
 * - no tw-animate enter/exit classes (no page-entrance animations; shadcn's
 *   templates ship them, this keeps them from coming back on a re-add);
 * - only allowlisted animate-* utilities and @keyframes;
 * - every file with a transform transition also has a motion-reduce: fallback.
 * Durations and curves come from the tokens: duration-(--dur-pop) ease-(--ease-out).
 */
const SRC = path.resolve(__dirname, "..");

const MAX_DURATION_MS = 300;
const ALLOWED_ANIMATE = new Set(["animate-pulse", "animate-spin", "animate-none"]);
const ALLOWED_KEYFRAMES = new Set(["bounce-dot"]);
const ENTER_EXIT_CLASS =
  /^(?:animate-(?:in|out)|fade-(?:in|out)(?:-\d+)?|zoom-(?:in|out)(?:-\d+)?|slide-(?:in|out)-from-[a-z-]+\d*|spin-(?:in|out)(?:-\d+)?)$/;
const TRANSFORM_PROP = /\b(?:transform|scale|translate|rotate)\b/;

/**
 * Temporary: violations that predate the policy, each owned by a follow-up PR of
 * the frontend-polish plan. The owning PR deletes its entries; a stale entry
 * (no longer violating) fails the test, so the list only shrinks.
 */
const ALLOWLIST: Record<string, string[]> = {
  // PR-4 (page states and the home form)
  "components/new-run/visual-direction-fieldset.tsx": ["transform-without-motion-reduce"],
};

type Violation = { file: string; rule: string };

function sourceFiles(): string[] {
  return (readdirSync(SRC, { recursive: true }) as string[])
    .map((f) => f.split(path.sep).join("/"))
    .filter((f) => /\.(tsx?|css)$/.test(f))
    .filter((f) => !f.startsWith("__tests__/") && !/\.test\.tsx?$/.test(f))
    .sort();
}

/** The utility part of a class token: strips variants (`md:hover:`) and `!`. */
function utilityOf(token: string): string {
  let depth = 0;
  let start = 0;
  for (let i = 0; i < token.length; i++) {
    const c = token[i];
    if (c === "[" || c === "(") depth++;
    else if (c === "]" || c === ")") depth--;
    else if (c === ":" && depth === 0) start = i + 1;
  }
  return token.slice(start).replace(/^!|!$/g, "");
}

function durationMs(value: string, unit: string): number {
  return unit === "s" ? parseFloat(value) * 1000 : parseFloat(value);
}

function classTokens(src: string): string[] {
  const code = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  const tokens: string[] = [];
  for (const m of code.matchAll(/"((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)'|`((?:[^`\\]|\\.)*)`/g)) {
    tokens.push(...(m[1] ?? m[2] ?? m[3] ?? "").split(/\s+/).filter(Boolean));
  }
  return tokens;
}

function checkClasses(file: string, src: string): Violation[] {
  const rules = new Set<string>();
  let transformTransition = false;
  let motionReduce = false;
  for (const token of classTokens(src)) {
    if (token.startsWith("motion-reduce:")) motionReduce = true;
    const u = utilityOf(token);
    if (u === "transition-all") rules.add("transition-all");
    if (u === "transition") rules.add("bare-transition");
    if (u === "ease-in") rules.add("ease-in");
    const d = u.match(/^duration-(\d+)$/) ?? u.match(/^duration-\[(\d+(?:\.\d+)?)(ms|s)\]$/);
    if (d && durationMs(d[1], d[2] ?? "ms") > MAX_DURATION_MS) rules.add("duration>300");
    if (ENTER_EXIT_CLASS.test(u)) rules.add("enter-exit-class");
    else if (/^animate-/.test(u) && !ALLOWED_ANIMATE.has(u)) rules.add(`animate:${u}`);
    const arb = u.match(/^transition-\[(.*)\]$/);
    if (u === "transition-transform" || (arb && TRANSFORM_PROP.test(arb[1]))) transformTransition = true;
  }
  if (transformTransition && !motionReduce) rules.add("transform-without-motion-reduce");
  return [...rules].map((rule) => ({ file, rule }));
}

function checkCss(file: string, src: string): Violation[] {
  const css = src.replace(/\/\*[\s\S]*?\*\//g, "");
  const rules = new Set<string>();
  let transformTransition = false;
  for (const [, prop, value] of css.matchAll(/(transition(?:-property|-duration|-timing-function)?)\s*:\s*([^;]+);/g)) {
    if (/\ball\b/.test(value) && prop !== "transition-duration") rules.add("transition-all");
    if (prop !== "transition-duration" && TRANSFORM_PROP.test(value)) transformTransition = true;
    for (const [, n, unit] of value.matchAll(/(\d+(?:\.\d+)?)(ms|s)\b/g)) {
      if (durationMs(n, unit) > MAX_DURATION_MS) rules.add("duration>300");
    }
  }
  if (/\bease-in\b(?!-out)/.test(css)) rules.add("ease-in");
  for (const [, name] of css.matchAll(/@keyframes\s+([\w-]+)/g)) {
    if (!ALLOWED_KEYFRAMES.has(name)) rules.add(`keyframes:${name}`);
  }
  if (transformTransition && !/prefers-reduced-motion/.test(css)) rules.add("transform-without-motion-reduce");
  return [...rules].map((rule) => ({ file, rule }));
}

function violations(): Violation[] {
  return sourceFiles().flatMap((file) => {
    const src = readFileSync(path.join(SRC, file), "utf8");
    return file.endsWith(".css") ? checkCss(file, src) : checkClasses(file, src);
  });
}

const allowed = (v: Violation) => ALLOWLIST[v.file]?.includes(v.rule) ?? false;

describe("motion policy", () => {
  const found = violations();

  it("has no violations outside the temporary allowlist", () => {
    expect(found.filter((v) => !allowed(v)).map((v) => `${v.file}: ${v.rule}`)).toEqual([]);
  });

  it("has no stale allowlist entries (delete them once fixed)", () => {
    const stale = Object.entries(ALLOWLIST).flatMap(([file, rules]) =>
      rules.filter((rule) => !found.some((v) => v.file === file && v.rule === rule)).map((rule) => `${file}: ${rule}`)
    );
    expect(stale).toEqual([]);
  });

  it("catches each rule (self-check of the scanner)", () => {
    const rulesOf = (src: string) => checkClasses("x.tsx", src).map((v) => v.rule).sort();
    expect(rulesOf(`<b className="hover:transition-all" />`)).toEqual(["transition-all"]);
    expect(rulesOf(`<b className="transition ease-in" />`)).toEqual(["bare-transition", "ease-in"]);
    expect(rulesOf(`<b className="ease-in-out duration-300 duration-[0.2s]" />`)).toEqual([]);
    expect(rulesOf(`<b className="duration-500" />`)).toEqual(["duration>300"]);
    expect(rulesOf(`<b className="data-open:animate-in fade-in-0 zoom-in-95" />`)).toEqual(["enter-exit-class"]);
    expect(rulesOf(`<b className="animate-bounce cursor-zoom-in animate-spin" />`)).toEqual(["animate:animate-bounce"]);
    expect(rulesOf(`<b className="transition-[opacity,scale] duration-(--dur-pop)" />`)).toEqual([
      "transform-without-motion-reduce",
    ]);
    expect(rulesOf(`<b className="transition-transform motion-reduce:transition-none" />`)).toEqual([]);
    expect(rulesOf(`// a transition-all in a comment\nconst a = 1;`)).toEqual([]);
    const cssRules = (src: string) => checkCss("x.css", src).map((v) => v.rule).sort();
    expect(cssRules(`a { transition: all 400ms ease-in; } @keyframes wiggle {}`)).toEqual([
      "duration>300",
      "ease-in",
      "keyframes:wiggle",
      "transition-all",
    ]);
    expect(cssRules(`a { transition: transform 200ms var(--ease-out); }`)).toEqual(["transform-without-motion-reduce"]);
  });
});
