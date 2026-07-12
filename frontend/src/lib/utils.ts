import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

// Session-state values are not always strings. Some keys (e.g. `target_search_trends`)
// are stored as nested objects/arrays; rendering those raw crashes React with
// "Objects are not valid as a React child". Flatten any value to a display string.
export function formatStateValue(value: unknown): string {
  if (value == null) return ""
  if (typeof value === "string") return value
  if (Array.isArray(value)) return value.map(formatStateValue).filter(Boolean).join(", ")
  if (typeof value === "object") {
    return Object.values(value as Record<string, unknown>).map(formatStateValue).filter(Boolean).join(", ")
  }
  return String(value)
}
