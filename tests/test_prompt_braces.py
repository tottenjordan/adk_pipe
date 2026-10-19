"""Global brace guard for every agent instruction (CA-13).

ADK fills ``{name}`` / ``{name?}`` instruction tokens from session state and
leaves any other brace text alone, so a stray JSON example or a typo'd token
would reach the model verbatim (or, for ``{name}``, fail the turn when the key
is missing). Rather than per-prompt lists, this test scans every instruction of
every ``LlmAgent`` defined in ``creative_agent``, ``interactive_creative`` and
``trend_scout`` (all submodules imported) plus every string constant of their
``prompts`` modules, and requires that:

* the only braces are ``{identifier}`` / ``{identifier?}`` tokens, and
* every token names a known state key: an agent ``output_key``, a key the
  state-init callbacks seed, or a key written at runtime by a tool, node or
  resume (``_RUNTIME_KEYS`` below, each with its writer).

Fill-in slots in prompt text use ``[square brackets]`` instead.
"""

import importlib
import pkgutil
import re
import sys

import pytest
from google.adk.agents import LlmAgent

_PACKAGES = ("creative_agent", "interactive_creative", "trend_scout")
_PROMPT_MODULES = tuple(f"{pkg}.prompts" for pkg in _PACKAGES)

# Keys written outside state init and output_key, with the code that writes them.
_RUNTIME_KEYS = {
    # creative_agent
    "sources": "collect_research_sources_callback (research searchers)",
    "trend_situation_sources": "collect_trend_situation_sources_callback",
    "creative_brief_md": "brief gate / brief reviser (brief_render)",
    "brief_issues": "brief gate (brief_check)",
    "ad_copy_issues": "copy gate (copy_gate)",
    "visual_concept_issues": "concept gate (concept_guard)",
    "brand_history": "load_brand_history",
    "rating_signals": "load_brand_history (rating learning)",
    "trend_situation_md": "validate_trend_situation",
    # interactive_creative resumes (runserver.async_runs)
    "research_feedback": "checkpoint-1 resume",
    "ad_copy_feedback": "checkpoint-2 resume",
    "visual_revision_notes": "checkpoint-3 resume",
    # trend_scout
    "raw_gtrends": "get_daily_gtrends",
    "research_gaps": "record_research_gaps",
    "gtrends_staleness_note": "get_daily_gtrends",
    "trend_pick_note": "review_trends resume",
    "interactive_trend_pick": "load_session_state (opt-in, seeded by the caller)",
}

# Prompts whose output-format section shows a multi-line JSON skeleton. ADK
# leaves such braces untouched (not an identifier), so they are allowed, but
# only as a lone ``{`` / ``}`` / ``},`` on its own line: inline JSON (a
# ``{"a": 1}`` example) still fails. Prefer prose or [brackets] for new prompts.
_JSON_SKELETON_PROMPTS = {
    "trend_scout.prompts.UNDERSTAND_TRENDS_SYNTHESIZER_INSTR",
    "understand_trends_synthesizer.instruction",
}
_SKELETON_LINE = re.compile(r"\s*[{}],?\s*")

_TOKEN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\??\}")


def _import_all() -> None:
    for pkg in _PACKAGES:
        module = importlib.import_module(pkg)
        for info in pkgutil.walk_packages(module.__path__, pkg + "."):
            importlib.import_module(info.name)


def _instructions() -> tuple[dict[str, str], set[str]]:
    _import_all()
    texts: dict[str, str] = {}
    output_keys: set[str] = set()
    for name, module in list(sys.modules.items()):
        if not name.startswith(_PACKAGES):
            continue
        for attr, value in vars(module).items():
            if isinstance(value, LlmAgent):
                if value.output_key:
                    output_keys.add(value.output_key)
                for field in ("instruction", "global_instruction"):
                    text = getattr(value, field, None)
                    if isinstance(text, str) and text:
                        texts[f"{value.name}.{field}"] = text
            elif (
                name in _PROMPT_MODULES
                and isinstance(value, str)
                and not attr.startswith("__")
            ):
                texts[f"{name}.{attr}"] = value
    return texts, output_keys


def _seeded_keys() -> set[str]:
    from creative_agent.callbacks import _set_initial_states as creative_init
    from trend_scout.callbacks import _set_initial_states as scout_init

    creative: dict = {}
    creative_init({}, creative)
    scout: dict = {}
    scout_init(
        {
            "brand": "",
            "target_product": "",
            "target_audience": "",
            "key_selling_points": "",
            "target_search_trends": {"target_search_trends": []},
        },
        scout,
    )
    return set(creative) | set(scout)


_TEXTS, _OUTPUT_KEYS = _instructions()
_KNOWN_KEYS = _OUTPUT_KEYS | _seeded_keys() | set(_RUNTIME_KEYS)


def test_the_scan_finds_the_instructions():
    # Guard the guard: a broken scan must not pass vacuously.
    assert len(_TEXTS) > 50
    for expected in (
        "creative_agent.prompts.AD_COPY_REVISER_INSTR",
        "interactive_creative.prompts.VISUAL_CONCEPT_REVISER_INSTR",
        "trend_scout.prompts.PICK_TRENDS_INSTR",
        "gs_web_planner.instruction",
        "campaign_web_synthesizer.instruction",
    ):
        assert expected in _TEXTS, expected


@pytest.mark.parametrize("name", sorted(_TEXTS))
def test_only_state_tokens_use_braces(name):
    leftover = _TOKEN.sub("", _TEXTS[name])
    if name in _JSON_SKELETON_PROMPTS:
        leftover = "\n".join(
            line for line in leftover.splitlines() if not _SKELETON_LINE.fullmatch(line)
        )
    assert "{" not in leftover and "}" not in leftover, (
        f"{name}: brace text other than {{identifier}} / {{identifier?}} tokens "
        f"(use [square brackets] for fill-in slots): "
        f"{re.findall(r'.{0,30}[{}].{0,30}', leftover)[:3]}"
    )


@pytest.mark.parametrize("name", sorted(_TEXTS))
def test_every_token_is_a_known_state_key(name):
    unknown = set(_TOKEN.findall(_TEXTS[name])) - _KNOWN_KEYS
    assert not unknown, f"{name}: unknown state keys {sorted(unknown)}"
