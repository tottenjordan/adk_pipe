import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * Normalize an ADK session-state value into a display string.
 * Most campaign fields are plain strings, but some (e.g. `target_search_trends`)
 * are stored nested as `{ target_search_trends: string[] }`. Flatten arrays and
 * unwrap such wrapper objects so the value can be rendered as a React child.
 */
export function formatStateValue(value: unknown): string {
  if (value == null) return ""
  if (typeof value === "string") return value
  if (Array.isArray(value)) return value.map(formatStateValue).filter(Boolean).join(", ")
  if (typeof value === "object") {
    return Object.values(value as Record<string, unknown>)
      .map(formatStateValue)
      .filter(Boolean)
      .join(", ")
  }
  return String(value)
}
