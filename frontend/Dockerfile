# syntax=docker/dockerfile:1
# BuildKit syntax (cache mounts). Built by the repo-root `cloudbuild.yaml` (buildx +
# registry layer cache); `gcloud run deploy --source ./frontend` still works.

# ---- builder ----
# npm ci and next build share one stage, so node_modules is never copied between
# stages (that COPY alone took ~39 s). The `npm ci` layer only changes with
# package.json / package-lock.json, so the registry cache reuses it on source-only
# changes. `npm run build` keeps Next's TypeScript check.
FROM node:22-slim AS builder
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY . .
RUN npm run build

# ---- runner ----
FROM node:22-slim AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
RUN groupadd -r nodejs && useradd -r -g nodejs nextjs
# Standalone server + assets it does not bundle itself.
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs
ENV PORT=8080 HOSTNAME=0.0.0.0
EXPOSE 8080
CMD ["node", "server.js"]
