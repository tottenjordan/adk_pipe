# syntax=docker/dockerfile:1
# BuildKit syntax (cache mounts). Built by the repo-root `cloudbuild.yaml` (buildx +
# registry layer cache); `gcloud run deploy --source ./frontend` still works.

# ---- base ----
# Node 24 LTS, pinned by exact tag + digest (reproducible builds). Written once,
# here, as a literal FROM: Dependabot's docker ecosystem (.github/dependabot.yml,
# /frontend) bumps tag and digest together, but skips `FROM node:${ARG}` lines, so
# both stages build FROM this one instead of sharing an ARG.
FROM node:24.22.1-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205 AS base
# Opt out of Next telemetry for `next build` (builder) and the server (runner).
ENV NEXT_TELEMETRY_DISABLED=1

# ---- builder ----
# npm ci and next build share one stage, so node_modules is never copied between
# stages (that COPY alone took ~39 s). The `npm ci` layer only changes with
# package.json / package-lock.json, so the registry cache reuses it on source-only
# changes. `npm run build` keeps Next's TypeScript check.
FROM base AS builder
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY . .
RUN npm run build

# ---- runner ----
FROM base AS runner
WORKDIR /app
ENV NODE_ENV=production
# Standalone server + assets it does not bundle itself. Runs as the image's
# built-in non-root `node` user, which owns .next (the server writes .next/cache).
COPY --from=builder /app/public ./public
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
USER node
ENV PORT=8080 HOSTNAME=0.0.0.0
EXPOSE 8080
CMD ["node", "server.js"]
