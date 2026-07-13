This is a [Next.js](https://nextjs.org) project bootstrapped with [`create-next-app`](https://nextjs.org/docs/app/api-reference/cli/create-next-app).

## Getting Started

First, run the development server:

```bash
npm run dev
# or
yarn dev
# or
pnpm dev
# or
bun dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.

You can start editing the page by modifying `app/page.tsx`. The page auto-updates as you edit the file.

This project uses [`next/font`](https://nextjs.org/docs/app/building-your-application/optimizing/fonts) to automatically optimize and load [Geist](https://vercel.com/font), a new font family for Vercel.

## Project Structure

```bash
frontend/
├── src/
│   ├── app/                                # Next.js App Router — routes + server-side API proxies
│   │   ├── layout.tsx                      # root layout: fonts (Sora + JetBrains Mono), glass header
│   │   ├── page.tsx                        # "/" campaign input form (brand, audience, product, agent selector)
│   │   ├── globals.css                     # Tailwind base + light-theme design tokens
│   │   ├── favicon.ico
│   │   ├── api/
│   │   │   ├── adk/[...path]/route.ts      # same-origin proxy to the ADK api_server (streams SSE through untouched)
│   │   │   └── gcs/route.ts                # authenticated GCS proxy for serving artifacts
│   │   ├── run/[sessionId]/page.tsx        # "/run/*" live SSE event stream, pipeline widgets, status tracking
│   │   └── results/[sessionId]/page.tsx    # "/results/*" artifacts gallery, research PDF, eval report, state inspector
│   ├── components/
│   │   ├── event-log.tsx                   # timeline of streamed agent events
│   │   ├── gallery-viewer.tsx              # image gallery for generated visual concepts
│   │   ├── gcs-widget.tsx                  # renders a gs:// URI as a Cloud Console link
│   │   ├── trend-cards.tsx                 # trend selection cards (parsed from agent output)
│   │   └── ui/                             # shadcn/ui primitives (self-contained, generated)
│   │       ├── badge.tsx
│   │       ├── button.tsx
│   │       ├── card.tsx
│   │       ├── collapsible.tsx
│   │       ├── dialog.tsx
│   │       ├── input.tsx
│   │       ├── label.tsx
│   │       ├── scroll-area.tsx
│   │       ├── select.tsx
│   │       ├── separator.tsx
│   │       ├── tabs.tsx
│   │       └── textarea.tsx
│   ├── lib/
│   │   ├── api.ts                          # API client: session CRUD, SSE streaming, artifact fetching
│   │   ├── presets.ts                      # preset dropdown values for the campaign form
│   │   ├── types.ts                        # shared TS types (ADK event Parts, agent events, …)
│   │   └── utils.ts                        # cn() class-merge helper + formatStateValue
│   └── __tests__/                          # Vitest + React Testing Library unit tests
│       ├── setup.ts                        # test bootstrap (jsdom, matchers)
│       ├── api-client.test.ts              # SSE parsing / API client
│       ├── extract-items.test.ts           # extractItems helper
│       ├── form-validation.test.ts         # campaign form validation
│       ├── gcs-uri.test.ts                 # gs:// URI building
│       ├── interactive-mode.test.ts        # interactive-mode pause/resume logic
│       ├── parse-trends.test.ts            # trend markdown parsing
│       └── widget-layouts.test.ts          # pipeline widget layouts
├── public/                                 # static assets (create-next-app svgs + trend_trawler_banner.png)
├── components.json                         # shadcn/ui config (aliases, style)
├── eslint.config.mjs                       # ESLint flat config
├── next.config.ts                          # Next.js config
├── postcss.config.mjs                      # PostCSS / Tailwind config
├── tsconfig.json                           # TypeScript config
├── vitest.config.ts                        # Vitest config
├── package.json
├── package-lock.json
├── AGENTS.md                               # ⚠️ modified Next.js — read node_modules/next/dist/docs before coding
├── CLAUDE.md                               # → @AGENTS.md
└── README.md                               # this file
```

## Learn More

To learn more about Next.js, take a look at the following resources:

- [Next.js Documentation](https://nextjs.org/docs) - learn about Next.js features and API.
- [Learn Next.js](https://nextjs.org/learn) - an interactive Next.js tutorial.

You can check out [the Next.js GitHub repository](https://github.com/vercel/next.js) - your feedback and contributions are welcome!

## Deploy on Vercel

The easiest way to deploy your Next.js app is to use the [Vercel Platform](https://vercel.com/new?utm_medium=default-template&filter=next.js&utm_source=create-next-app&utm_campaign=create-next-app-readme) from the creators of Next.js.

Check out our [Next.js deployment documentation](https://nextjs.org/docs/app/building-your-application/deploying) for more details.
