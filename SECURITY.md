# Security policy

## Reporting a vulnerability

Report vulnerabilities privately through GitHub: open the repository's **Security** tab and
click **Report a vulnerability** (a private security advisory). Include what you found, how
to reproduce it, and the impact you expect.

Do not open public issues, pull requests or discussions for security reports.

## Scope

- **Public share viewer** (`trend-trawler-share`): the only unauthenticated surface.
- **Web app and API** (`trend-trawler-web`, `trend-trawler-api`): IAP-gated, with per-user
  authorization behind the IAP.
- **Agent Engine engines** (`trend_scout`, `creative_agent`, `interactive_creative`).
- **Batch pipeline**: the Pub/Sub orchestrator, Cloud Tasks queue and worker in
  `cloud_functions/`.
- The code in this repository that builds and deploys the above.

Issues in third-party dependencies are in scope only when this project's use of them makes
them exploitable; otherwise report them upstream.

## What to expect

We aim to acknowledge a report within 5 business days and to keep you updated until it is
resolved. Please give us a reasonable time to fix the issue before disclosing it publicly.
