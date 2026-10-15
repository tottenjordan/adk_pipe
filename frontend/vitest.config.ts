import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  test: {
    // Node by default: building a jsdom per file dominated the run. DOM tests
    // opt in with a `// @vitest-environment jsdom` docblock (README → Testing).
    environment: "node",
    globals: true,
    setupFiles: ["./src/__tests__/setup.ts"],
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
      // The `server-only` guard throws outside the react-server condition (Next aliases
      // it per bundle); tests import the server modules directly, so give them the no-op.
      "server-only": path.resolve(__dirname, "./node_modules/server-only/empty.js"),
    },
  },
});
