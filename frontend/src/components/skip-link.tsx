/**
 * Keyboard skip link (WCAG 2.4.1): the first focusable element on every page,
 * hidden until focused, jumping past the header to `<main id="main">`.
 */
export function SkipLink() {
  return (
    <a
      href="#main"
      className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-[var(--radius-control)] focus:border focus:border-border focus:bg-card focus:px-3 focus:py-2 focus:text-sm focus:font-medium focus:text-primary"
    >
      Skip to content
    </a>
  );
}
