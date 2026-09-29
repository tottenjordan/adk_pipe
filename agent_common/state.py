"""Shared session-state helpers used by every agent package.

- ``memorize`` -- the ADK tool the root orchestrators call to persist campaign
  metadata into session state. The ADK ``FunctionTool`` name comes from the
  function ``__name__``, so it must stay ``memorize`` (prompts reference it).
- ``seed_initial_state`` -- the one-time session-state seeding shared by the
  ``before_agent_callback`` state loaders. Per-agent differences (output dir,
  extra seeded keys, post-seed ``setdefault`` defaults) are supplied by the
  caller.
"""

import datetime
import logging
import uuid
from collections.abc import MutableMapping
from typing import Any

from google.adk.sessions.state import State
from google.adk.tools import ToolContext


def memorize(key: str, value: str, tool_context: ToolContext):
    """
    Memorize pieces of information, one key-value pair at a time.

    Args:
        key: the label indexing the memory to store the value.
        value: the information to be stored.
        tool_context: The ADK tool context.

    Returns:
        A status message.
    """
    mem_dict = tool_context.state
    mem_dict[key] = value
    return {"status": f'Stored "{key}": "{value}"'}


def seed_initial_state(
    source: dict[str, Any],
    target: State | MutableMapping[str, Any],
    *,
    state_init_key: str,
    gcs_bucket: str | None,
    agent_output_dir: str,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Seed session state once per session.

    If ``state_init_key`` is absent from ``target``, marks it initialized, sets
    ``gcs_bucket``, ``agent_output_dir`` and a unique timestamped ``gcs_folder``,
    then any ``extra`` keys, then applies ``source`` on top.

    Args:
        source: Default state values to apply (overwrite existing keys).
        target: The session state (an ADK ``State`` or a plain dict).
        state_init_key: The marker key that records the state was seeded.
        gcs_bucket: The ``gs://`` bucket URI stored under ``gcs_bucket``.
        agent_output_dir: Value stored under ``agent_output_dir``.
        extra: Additional per-agent keys, set before ``source`` is applied.

    Returns:
        True if the state was seeded by this call, False if it already was.
    """
    if state_init_key in target:
        return False

    unique_id = f"{str(uuid.uuid4())[:4]}"
    formatted_now = datetime.datetime.now(datetime.UTC).strftime("%Y_%m_%d_%H_%M")
    target[state_init_key] = True
    target["gcs_bucket"] = gcs_bucket
    target["agent_output_dir"] = agent_output_dir
    target["gcs_folder"] = f"{formatted_now}_{unique_id}"
    logging.info(f"gcs_folder: {target['gcs_folder']}")
    if extra:
        target.update(extra)

    target.update(source)
    return True
