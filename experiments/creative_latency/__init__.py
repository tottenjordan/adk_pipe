"""External, offline-first harness for measuring creative_agent run latency.

This package is deliberately NOT imported by any agent, so it is never bundled
into an Agent Engine deployment (``deploy_agent.py``'s ``AGENT_EXTRA_PACKAGES``
is derived from the agent import graph). Keep it out of that graph.
"""
