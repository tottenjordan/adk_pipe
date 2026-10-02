"use client";

import React, { useMemo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { FieldLabel } from "@/components/field-label";
import { formatReportCitations, type ReportSources } from "@/lib/research-report";
import { cn } from "@/lib/utils";

// Minimal hast shapes (avoids a direct `hast` type dependency).
interface HastNode {
  type: string;
  tagName?: string;
  value?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
}

const isCitation = (n: HastNode | undefined) =>
  n?.type === "element" &&
  n.tagName === "a" &&
  n.children?.length === 1 &&
  n.children[0].type === "text" &&
  /^\d+$/.test(n.children[0].value ?? "");

/**
 * Rehype pass: mark a citation link that directly follows another citation
 * (no text between) so it renders with a separating comma: ¹,².
 */
function rehypeJoinCitations() {
  const walk = (node: HastNode) => {
    const kids = node.children ?? [];
    kids.forEach((child, i) => {
      if (i > 0 && isCitation(child) && isCitation(kids[i - 1])) {
        child.properties = { ...child.properties, dataCiteJoin: "true" };
      }
      walk(child);
    });
  };
  return (tree: HastNode) => walk(tree);
}

/** Plain text of a link's children when it is a single string. */
function textOf(children: React.ReactNode): string | null {
  if (typeof children === "string") return children;
  if (Array.isArray(children) && children.every((c) => typeof c === "string")) {
    return children.join("");
  }
  return null;
}

const BODY = "text-sm leading-relaxed text-foreground/85";

/** Element styles for the report markdown (the app has no typography plugin). */
const ELEMENTS: Components = {
  h1: ({ children }) => (
    <h1 className="mb-3 text-lg font-bold leading-snug text-foreground">{children}</h1>
  ),
  h2: ({ children }) => (
    <h2 className="mt-6 mb-2 text-base font-bold leading-snug text-foreground">{children}</h2>
  ),
  h3: ({ children }) => (
    <h3 className="mt-4 mb-1.5 text-sm font-bold text-foreground">{children}</h3>
  ),
  p: ({ children }) => <p className={cn("my-2.5", BODY)}>{children}</p>,
  ul: ({ children }) => (
    <ul className={cn("my-2.5 list-disc space-y-1.5 pl-5 marker:text-muted-foreground", BODY)}>
      {children}
    </ul>
  ),
  ol: ({ children }) => (
    <ol className={cn("my-2.5 list-decimal space-y-1.5 pl-5 marker:text-muted-foreground", BODY)}>
      {children}
    </ol>
  ),
  li: ({ children }) => <li className="pl-1 [&>ol]:my-1.5 [&>p]:my-0 [&>ul]:my-1.5">{children}</li>,
  strong: ({ children }) => <strong className="font-semibold text-foreground">{children}</strong>,
  blockquote: ({ children }) => (
    <blockquote className="my-3 border-l-2 border-primary/30 pl-3 text-muted-foreground">
      {children}
    </blockquote>
  ),
  code: ({ children }) => (
    <code className="rounded-sm bg-muted px-1 font-mono text-xs">{children}</code>
  ),
  hr: () => <hr className="my-5 border-border" />,
};

/**
 * A research report (raw `combined_final_cited_report` markdown with
 * `<cite source="src-N" />` tags), rendered with numbered superscript
 * citations and a numbered "Sources" list linking to each source.
 */
export function ResearchReport({
  markdown,
  sources,
  scroll = false,
  className,
}: {
  markdown: string;
  sources?: ReportSources;
  /** Cap the height and scroll long reports inside the panel. */
  scroll?: boolean;
  className?: string;
}) {
  const formatted = useMemo(
    () => formatReportCitations(markdown, sources),
    [markdown, sources]
  );

  const components = useMemo<Components>(() => {
    const labels = new Map(formatted.sources.map((s) => [String(s.n), s.label]));
    return {
      ...ELEMENTS,
      a: ({ href, children, node }) => {
        const text = textOf(children);
        if (text && /^\d+$/.test(text)) {
          const joined = node?.properties?.dataCiteJoin === "true";
          return (
            <sup className="ml-px text-[0.7em] leading-none font-medium tabular-nums">
              {joined && <span className="text-muted-foreground">,</span>}
              <a
                href={href}
                title={labels.get(text)}
                target="_blank"
                rel="noopener noreferrer"
                className="text-primary hover:underline"
              >
                {text}
              </a>
            </sup>
          );
        }
        return (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="text-primary hover:underline"
          >
            {children}
          </a>
        );
      },
    };
  }, [formatted.sources]);

  return (
    <div
      className={cn(
        "rounded-md border border-border bg-background p-5",
        scroll && "max-h-[28rem] overflow-y-auto",
        className
      )}
    >
      <div className="max-w-[75ch]">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          rehypePlugins={[rehypeJoinCitations]}
          components={components}
        >
          {formatted.markdown}
        </ReactMarkdown>
      </div>
      {formatted.sources.length > 0 && (
        <section aria-label="Sources" className="mt-6 border-t border-border pt-4">
          <FieldLabel as="h3" className="mb-2">
            Sources
            <span className="ml-1.5 font-normal tabular-nums">{formatted.sources.length}</span>
          </FieldLabel>
          <ol className="grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2 lg:grid-cols-3">
            {formatted.sources.map((s) => (
              <li key={s.id} className="flex min-w-0 items-baseline gap-2">
                <span className="w-5 shrink-0 text-right text-muted-foreground tabular-nums">
                  {s.n}
                </span>
                <a
                  href={s.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={s.label}
                  className="truncate text-foreground/85 hover:text-primary hover:underline"
                >
                  {s.label}
                </a>
              </li>
            ))}
          </ol>
        </section>
      )}
    </div>
  );
}
