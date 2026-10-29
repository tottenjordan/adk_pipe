"""Register, list, update, or delete Trend Trawler agents in Gemini Enterprise.

Supports both Discovery Engine ``v1alpha`` registration modes without requiring a
hardcoded ``app_id``:

1. ``--mode reasoning-engine``: binds a deployed Vertex AI Agent Engine
   (``adkAgentDefinition.provisionedReasoningEngine``).
2. ``--mode a2a``: binds a Cloud Run A2A endpoint mounted by
   ``runserver.a2a_mount`` (``a2aAgentDefinition.jsonAgentCard``).

Use ``--dry-run`` to inspect the exact REST method, URL, and JSON payload offline
with zero GCP credentials or network requests.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import dotenv

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from runserver.a2a_mount import DEFAULT_A2A_AGENTS, agent_card_dict, build_agent_card

ENV_FILE_PATH = os.path.join(project_root, ".env")
dotenv.load_dotenv(dotenv_path=ENV_FILE_PATH, override=False)

AGENT_CHOICES: tuple[str, ...] = (*DEFAULT_A2A_AGENTS, "all")
MODE_CHOICES: tuple[str, ...] = ("reasoning-engine", "a2a")

_AGENT_DEFAULTS: dict[str, dict[str, str]] = {
    "trend_scout": {
        "display_name": "Trend Trawler — Trend Scout",
        "description": (
            "Discovers daily Google Search trends from BigQuery, researches cultural "
            "context, and selects the top 3 campaign-relevant trends."
        ),
        "tool_description": (
            "Invoke when the user wants to discover rising Google Search trends and "
            "evaluate cultural relevance for a brand, product, and target audience."
        ),
        "engine_env": "SCOUT_AGENT_ENGINE_ID",
    },
    "creative_agent": {
        "display_name": "Trend Trawler — Creative Agent",
        "description": (
            "Runs parallel web and enterprise research, writes a cited strategic "
            "brief, generates gated ad copy and visuals, and scores creatives with "
            "a 12-dimension LLM judge."
        ),
        "tool_description": (
            "Invoke when the user wants end-to-end ad copy, rendered ad visuals, and "
            "a 12-dimension creative evaluation for a search trend and campaign."
        ),
        "engine_env": "CREATIVE_AGENT_ENGINE_ID",
    },
    "interactive_creative": {
        "display_name": "Trend Trawler — Interactive Creative",
        "description": (
            "Human-in-the-loop creative pipeline with three review checkpoints "
            "(creative brief, ad copy, visual concepts) and Studio handoff."
        ),
        "tool_description": (
            "Invoke when the user wants an interactive creative campaign run with "
            "human review checkpoints before image rendering and final evaluation."
        ),
        "engine_env": "INTERACTIVE_AGENT_ENGINE_ID",
    },
}

HttpTransport = Callable[
    [str, str, Mapping[str, str], dict[str, Any] | None], dict[str, Any]
]
TokenProvider = Callable[[], str]


@dataclass(frozen=True)
class PlannedRequest:
    """One Discovery Engine ``v1alpha`` HTTP operation."""

    action: str
    agent: str | None
    method: str
    url: str
    payload: dict[str, Any] | None


_LOCATION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def validate_location(raw: str, *, field_name: str = "location") -> str:
    """Validate a GCP location slug so malformed input cannot hijack URL authority."""
    loc = raw.strip()
    if not loc:
        raise ValueError(f"{field_name} must be non-empty")
    if not _LOCATION_RE.fullmatch(loc):
        raise ValueError(
            f"Invalid {field_name} {raw!r}; expected lowercase letters, digits, and hyphens"
        )
    return loc


def validate_a2a_base_url(raw: str | None) -> str:
    """Validate an A2A public base URL (http/https only, non-empty host, no credentials)."""
    cleaned = (raw or "").strip()
    if not cleaned:
        raise ValueError(
            "--a2a-base-url (or A2A_PUBLIC_BASE_URL) is required for --mode a2a"
        )
    parsed = urllib.parse.urlsplit(cleaned)
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.netloc
        or not parsed.hostname
    ):
        raise ValueError(
            f"Invalid --a2a-base-url {raw!r}; expected an http:// or https:// URL with a host"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(
            f"Invalid --a2a-base-url {raw!r}; embedded credentials are not allowed"
        )
    if parsed.query or parsed.fragment:
        raise ValueError(
            f"Invalid --a2a-base-url {raw!r}; query strings and fragments are not allowed"
        )
    return cleaned.rstrip("/")


def build_discovery_base_url(*, project: str, location: str) -> str:
    """Return the Discovery Engine ``v1alpha`` ``default_collection`` base URL."""
    proj = project.strip()
    if not proj:
        raise ValueError(
            "project must be non-empty (--project or GOOGLE_CLOUD_PROJECT)"
        )
    loc = validate_location(location, field_name="location")
    host = (
        "https://discoveryengine.googleapis.com"
        if loc == "global"
        else f"https://{loc}-discoveryengine.googleapis.com"
    )
    return (
        f"{host}/v1alpha/projects/{urllib.parse.quote(proj, safe='')}"
        f"/locations/{urllib.parse.quote(loc, safe='')}"
        "/collections/default_collection"
    )


def build_agents_url(
    *,
    project: str,
    location: str,
    app_id: str,
    agent_resource_id: str | None = None,
    update_mask: Sequence[str] | None = None,
) -> str:
    """Return the Discovery Engine ``v1alpha`` assistant agents collection/item URL."""
    app = app_id.strip()
    if not app:
        raise ValueError(
            "app_id must be non-empty (--app-id or GEMINI_ENTERPRISE_APP_ID)"
        )
    base = build_discovery_base_url(project=project, location=location)
    url = (
        f"{base}/engines/{urllib.parse.quote(app, safe='')}"
        "/assistants/default_assistant/agents"
    )
    if agent_resource_id:
        rid = agent_resource_id.strip().rsplit("/", 1)[-1]
        if not rid:
            raise ValueError("agent_resource_id must be non-empty")
        url = f"{url}/{urllib.parse.quote(rid, safe='')}"
    if update_mask:
        query = urllib.parse.urlencode({"updateMask": ",".join(update_mask)})
        url = f"{url}?{query}"
    return url


def normalize_reasoning_engine_name(
    raw_id: str, *, project: str, engine_location: str
) -> str:
    """Accept either a bare numeric ID or a full ``projects/.../reasoningEngines/...`` path."""
    cleaned = raw_id.strip()
    if not cleaned:
        raise ValueError("reasoning_engine_id must be non-empty")
    if cleaned.startswith("projects/"):
        return cleaned
    proj = project.strip()
    if not proj:
        raise ValueError("project must be non-empty")
    loc = validate_location(
        engine_location.strip() or "us-central1", field_name="engine_location"
    )
    return f"projects/{proj}/locations/{loc}/reasoningEngines/{cleaned}"


def build_registration_payload(
    *,
    agent_name: str,
    mode: str,
    project: str,
    engine_location: str = "us-central1",
    reasoning_engine_id: str | None = None,
    a2a_base_url: str | None = None,
    card_override: Mapping[str, Any] | None = None,
    display_name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Construct the Discovery Engine ``v1alpha`` agent resource body."""
    if agent_name not in _AGENT_DEFAULTS:
        raise ValueError(
            f"Unsupported agent {agent_name!r}; expected one of {DEFAULT_A2A_AGENTS}"
        )
    defaults = _AGENT_DEFAULTS[agent_name]
    resolved_display = (display_name or "").strip() or defaults["display_name"]
    resolved_desc = (description or "").strip() or defaults["description"]

    if mode == "reasoning-engine":
        if not reasoning_engine_id or not reasoning_engine_id.strip():
            raise ValueError(
                f"--reasoning-engine-id (or {defaults['engine_env']}) is required "
                f"for --mode reasoning-engine ({agent_name})"
            )
        re_resource = normalize_reasoning_engine_name(
            reasoning_engine_id,
            project=project,
            engine_location=engine_location,
        )
        return {
            "displayName": resolved_display,
            "description": resolved_desc,
            "adkAgentDefinition": {
                "toolSettings": {"toolDescription": defaults["tool_description"]},
                "provisionedReasoningEngine": {"reasoningEngine": re_resource},
            },
        }

    if mode == "a2a":
        if card_override is not None:
            card_dict = dict(card_override)
        else:
            validated_base = validate_a2a_base_url(a2a_base_url)
            card = build_agent_card(agent_name, base_url=validated_base)
            card_dict = agent_card_dict(card)
        return {
            "displayName": resolved_display,
            "description": resolved_desc,
            "a2aAgentDefinition": {
                "jsonAgentCard": json.dumps(card_dict, sort_keys=True),
            },
        }

    raise ValueError(f"Unsupported mode {mode!r}; expected one of {MODE_CHOICES}")


def _default_token_provider() -> str:
    import google.auth
    from google.auth.transport.requests import Request

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    if not credentials.valid:
        credentials.refresh(Request())
    if not credentials.token:
        raise RuntimeError(
            "Unable to obtain an access token from Application Default Credentials"
        )
    return str(credentials.token)


def _default_http_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url=url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8").strip()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Discovery Engine {method} {url} failed ({exc.code}): {err_body}"
        ) from exc


def _fetch_remote_agent_card(base_url: str, agent_name: str) -> dict[str, Any]:
    validated_base = validate_a2a_base_url(base_url)
    card_url = f"{validated_base}/a2a/{agent_name}/.well-known/agent-card.json"
    req = urllib.request.Request(url=card_url, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        loaded = json.loads(resp.read().decode("utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected JSON object from {card_url}")
    return loaded


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Register or manage Trend Trawler agents on Gemini Enterprise (Discovery Engine v1alpha)."
    )
    parser.add_argument(
        "--project",
        default=None,
        help="GCP project ID (defaults to GOOGLE_CLOUD_PROJECT).",
    )
    parser.add_argument(
        "--location",
        default="global",
        help="Discovery Engine location (default: global).",
    )
    parser.add_argument(
        "--engine-location",
        default=None,
        help="Vertex AI Agent Engine region for bare --reasoning-engine-id values (default: GCP_REGION or us-central1).",
    )
    parser.add_argument(
        "--app-id",
        default=None,
        help="Gemini Enterprise engine/app ID (defaults to GEMINI_ENTERPRISE_APP_ID).",
    )
    parser.add_argument(
        "--agent",
        choices=AGENT_CHOICES,
        default="trend_scout",
        help="Trend Trawler agent to target, or 'all' (for --register).",
    )
    parser.add_argument(
        "--mode",
        choices=MODE_CHOICES,
        default="a2a",
        help="Registration mode: 'reasoning-engine' (Agent Engine) or 'a2a' (Cloud Run A2A).",
    )
    parser.add_argument(
        "--reasoning-engine-id",
        default=None,
        help="Reasoning Engine ID or full resource name for --mode reasoning-engine.",
    )
    parser.add_argument(
        "--a2a-base-url",
        default=None,
        help="Public base URL of the Cloud Run API service for --mode a2a (defaults to A2A_PUBLIC_BASE_URL).",
    )
    parser.add_argument(
        "--fetch-card",
        action="store_true",
        help="Fetch /.well-known/agent-card.json over HTTP instead of building locally (ignored in --dry-run).",
    )
    parser.add_argument(
        "--agent-resource-id",
        default=None,
        help="Discovery Engine agent ID (required for --update and --delete).",
    )
    parser.add_argument(
        "--display-name",
        default=None,
        help="Optional custom displayName override (single-agent --register / --update).",
    )
    parser.add_argument(
        "--description",
        default=None,
        help="Optional custom description override (single-agent --register / --update).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print exact HTTP method, URL, and JSON payload without credentials or network calls.",
    )

    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument(
        "--list",
        action="store_true",
        help="List registered agents on the Gemini Enterprise assistant.",
    )
    actions.add_argument(
        "--register",
        action="store_true",
        help="Register the selected agent(s) on the Gemini Enterprise assistant.",
    )
    actions.add_argument(
        "--update",
        action="store_true",
        help="Update an existing registered agent (--agent-resource-id required).",
    )
    actions.add_argument(
        "--delete",
        action="store_true",
        help="Delete a registered agent (--agent-resource-id required).",
    )
    return parser


def plan_requests(
    args: argparse.Namespace,
    *,
    env: Mapping[str, str] = os.environ,
    card_fetcher: Callable[[str, str], dict[str, Any]] = _fetch_remote_agent_card,
) -> tuple[str, list[PlannedRequest]]:
    """Validate CLI arguments and produce the ordered list of Discovery Engine requests."""
    project = (args.project or env.get("GOOGLE_CLOUD_PROJECT") or "").strip()
    if not project:
        raise ValueError("--project or GOOGLE_CLOUD_PROJECT is required")
    app_id = (args.app_id or env.get("GEMINI_ENTERPRISE_APP_ID") or "").strip()
    if not app_id:
        raise ValueError("--app-id or GEMINI_ENTERPRISE_APP_ID is required")
    location = (args.location or "global").strip() or "global"
    engine_location = (
        args.engine_location or env.get("GCP_REGION") or "us-central1"
    ).strip() or "us-central1"
    a2a_base_url = (
        args.a2a_base_url
        or env.get("A2A_PUBLIC_BASE_URL")
        or env.get("BACKEND_PUBLIC_URL")
        or ""
    ).strip()

    if args.list:
        url = build_agents_url(project=project, location=location, app_id=app_id)
        return project, [
            PlannedRequest(
                action="list", agent=None, method="GET", url=url, payload=None
            )
        ]

    if args.delete:
        if not args.agent_resource_id:
            raise ValueError("--agent-resource-id is required for --delete")
        url = build_agents_url(
            project=project,
            location=location,
            app_id=app_id,
            agent_resource_id=args.agent_resource_id,
        )
        return project, [
            PlannedRequest(
                action="delete",
                agent=None,
                method="DELETE",
                url=url,
                payload=None,
            )
        ]

    if args.update:
        if not args.agent_resource_id:
            raise ValueError("--agent-resource-id is required for --update")
        if args.agent == "all":
            raise ValueError("--update targets a single agent, not '--agent all'")

    target_agents = (
        list(DEFAULT_A2A_AGENTS) if args.agent == "all" else [str(args.agent)]
    )
    if len(target_agents) > 1 and args.reasoning_engine_id:
        raise ValueError(
            "--reasoning-engine-id applies to one agent; set SCOUT_AGENT_ENGINE_ID / "
            "CREATIVE_AGENT_ENGINE_ID / INTERACTIVE_AGENT_ENGINE_ID for '--agent all'"
        )

    planned: list[PlannedRequest] = []
    for agent_name in target_agents:
        defaults = _AGENT_DEFAULTS[agent_name]
        re_id = args.reasoning_engine_id or env.get(defaults["engine_env"])
        card_override = None
        if args.mode == "a2a" and args.fetch_card and not args.dry_run:
            validated_a2a_base = validate_a2a_base_url(a2a_base_url)
            card_override = card_fetcher(validated_a2a_base, agent_name)

        payload = build_registration_payload(
            agent_name=agent_name,
            mode=args.mode,
            project=project,
            engine_location=engine_location,
            reasoning_engine_id=re_id,
            a2a_base_url=a2a_base_url,
            card_override=card_override,
            display_name=args.display_name if len(target_agents) == 1 else None,
            description=args.description if len(target_agents) == 1 else None,
        )
        if args.update:
            def_field = (
                "adkAgentDefinition"
                if args.mode == "reasoning-engine"
                else "a2aAgentDefinition"
            )
            url = build_agents_url(
                project=project,
                location=location,
                app_id=app_id,
                agent_resource_id=args.agent_resource_id,
                update_mask=("displayName", "description", def_field),
            )
            planned.append(
                PlannedRequest(
                    action="update",
                    agent=agent_name,
                    method="PATCH",
                    url=url,
                    payload=payload,
                )
            )
        else:
            url = build_agents_url(project=project, location=location, app_id=app_id)
            planned.append(
                PlannedRequest(
                    action="register",
                    agent=agent_name,
                    method="POST",
                    url=url,
                    payload=payload,
                )
            )
    return project, planned


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] = os.environ,
    transport: HttpTransport = _default_http_transport,
    token_provider: TokenProvider = _default_token_provider,
    card_fetcher: Callable[[str, str], dict[str, Any]] = _fetch_remote_agent_card,
) -> list[dict[str, Any]]:
    """Parse ``argv``, plan requests, and either print dry-run specs or execute them."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    project, requests = plan_requests(args, env=env, card_fetcher=card_fetcher)

    if args.dry_run:
        dry_items: list[dict[str, Any]] = []
        for req in requests:
            item: dict[str, Any] = {
                "dry_run": True,
                "action": req.action,
                "agent": req.agent,
                "method": req.method,
                "url": req.url,
                "headers": {
                    "Authorization": "Bearer <REDACTED>",
                    "Content-Type": "application/json",
                    "X-Goog-User-Project": project,
                },
                "payload": req.payload,
            }
            dry_items.append(item)
        output = dry_items[0] if len(dry_items) == 1 else {"operations": dry_items}
        print(json.dumps(output, indent=2, sort_keys=True))
        return dry_items

    token = token_provider()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Goog-User-Project": project,
    }
    results: list[dict[str, Any]] = []
    for req in requests:
        resp = transport(req.method, req.url, headers, req.payload)
        results.append(resp)
    output = results[0] if len(results) == 1 else {"results": results}
    print(json.dumps(output, indent=2, sort_keys=True))
    return results


def main(argv: Sequence[str] | None = None) -> int:
    try:
        run_cli(argv)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
